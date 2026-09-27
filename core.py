"""Authoritative camera, PTZ, Patrol, Rule, event, and alarm service."""

from __future__ import annotations

import json
import time
import uuid

from ..auth import issue_capability
from ..contracts import request
from ..ipc import IPCClient
from ..camera_runtime import CameraRuntime, discover_local_cameras, onvif_nudge, rtsp_candidates, tapo_nudge
from ..machine_security import protect_text, unprotect_text
from .base import BaseService, ServiceCommandError

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras(
    camera_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'tapo',
    host TEXT NOT NULL DEFAULT '',
    port INTEGER NOT NULL DEFAULT 554,
    username TEXT NOT NULL DEFAULT '',
    credential_blob BLOB,
    stream INTEGER NOT NULL DEFAULT 2,
    buffer_width INTEGER NOT NULL DEFAULT 1280,
    buffer_fps INTEGER NOT NULL DEFAULT 8,
    enabled INTEGER NOT NULL DEFAULT 1,
    connected INTEGER NOT NULL DEFAULT 0,
    patrol_enabled INTEGER NOT NULL DEFAULT 0,
    patrol_suspended INTEGER NOT NULL DEFAULT 0,
    ptz_owner TEXT NOT NULL DEFAULT 'IDLE',
    floor_id TEXT,
    orientation_json TEXT NOT NULL DEFAULT '{}',
    capability_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS patrol_waypoints(
    waypoint_id TEXT PRIMARY KEY,
    camera_id TEXT NOT NULL REFERENCES cameras(camera_id) ON DELETE CASCADE,
    pan REAL,
    tilt REAL,
    zoom REAL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    priority INTEGER NOT NULL,
    dwell_seconds REAL NOT NULL,
    enabled INTEGER NOT NULL,
    sequence_index INTEGER NOT NULL,
    source TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rules(
    rule_id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    definition_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alarm_state(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    state TEXT NOT NULL,
    emergency_type TEXT,
    event_id TEXT,
    updated_at REAL NOT NULL
);
INSERT OR IGNORE INTO alarm_state(singleton,state,updated_at) VALUES(1,'SAFE',0);
"""

PTZ_PRIORITY = {"IDLE": 0, "PATROL": 1, "SURVEY": 2, "EMERGENCY/RULE TRACKING": 3, "USER": 4}
GUIDED_RULE_TRIGGERS = {"is detected", "enters an area", "leaves an area", "remains in an area", "is not recognized", "an object moves", "a door or window opens"}
GUIDED_RULE_SCHEDULES = {"at any time", "at night", "while the household is away", "on weekdays", "on weekends"}
GUIDED_RULE_RESPONSES = {"record an event and notify me", "start evidence recording and notify me", "sound a local alert", "raise a security alarm", "track with the camera and record"}
GUIDED_RULE_EXCEPTIONS = {"", "an authorized caregiver is present", "a household member is present", "the person is accompanied by an adult", "Privacy mode is active"}


class CoreService(BaseService):
    protected_scopes = {
        "camera.upsert": "core.control",
        "camera.remove": "core.control",
        "camera.discover": "core.control",
        "camera.test": "core.control",
        "camera.frame": "core.control",
        "camera.set_enabled": "core.control",
        "patrol.set": "core.control",
        "ptz.claim": "core.control",
        "ptz.release": "core.control",
        "ptz.nudge": "core.control",
        "rule.save": "rules.write",
        "rule.delete": "rules.write",
        "alarm.trigger": "alarm.control",
        "alarm.clear": "alarm.control",
        "perimeter.set": "core.control",
        "protection.stop": "protection.stop",
        "provider.analytics_event": "provider.ingest",
    }
    public_commands = BaseService.public_commands | {"camera.list", "rule.list", "alarm.get", "perimeter.get"}

    def __init__(self):
        super().__init__("core", [("100-core", CORE_SCHEMA)])
        self.camera_runtime = CameraRuntime(self._native_analytics_event)
        for name in (
            "camera.upsert", "camera.remove", "camera.discover", "camera.test", "camera.frame",
            "camera.set_enabled", "patrol.set", "ptz.claim", "ptz.release", "ptz.nudge",
            "rule.save", "rule.delete", "alarm.trigger", "alarm.clear", "protection.stop",
            "camera.list", "rule.list", "alarm.get", "perimeter.get", "perimeter.set", "provider.analytics_event",
        ):
            self.register(name, getattr(self, "_" + name.replace(".", "_")))
        self._sync_camera_runtime()
        self._start_stream_server()

    def _start_stream_server(self) -> None:
        """Start the realtime streaming channel (separate port from the control
        gateway) so live frames are pushed rather than polled, and announce the
        current streaming endpoint via the shared machine runtime file that the
        control gateway merges into its discovery beacon."""
        import os
        import threading
        import time

        try:
            from ..streamgate import StreamServer, write_stream_json
            self._stream_server = StreamServer(
                lambda camera_id: self.camera_runtime.frame(camera_id),
                rotate_min_seconds=int(os.environ.get("FGO_STREAM_ROTATE_MIN", "120")),
                rotate_max_seconds=int(os.environ.get("FGO_STREAM_ROTATE_MAX", "300")),
            )
            self._stream_server.start()
            threading.Thread(
                target=self._announce_stream, daemon=True, name="FGO-stream-announce"
            ).start()
        except Exception as exc:  # noqa: BLE001 - streaming is additive
            self.logger.warning("ai.stream_unavailable", error=str(exc))

    def _announce_stream(self) -> None:
        import time
        from ..streamgate import write_stream_json
        while True:
            try:
                write_stream_json(self._stream_server.port, self._stream_server.epoch)
            except Exception:
                pass
            time.sleep(2.0)

    @staticmethod
    def _camera(row) -> dict:
        return {
            "camera_id": row["camera_id"], "name": row["name"], "enabled": bool(row["enabled"]),
            "provider": row["provider"], "host": row["host"], "port": int(row["port"]),
            "username": row["username"],
            "stream": int(row["stream"] or 2), "buffer_width": int(row["buffer_width"] or 1280), "buffer_fps": int(row["buffer_fps"] or 8),
            "connected": bool(row["connected"]), "patrol_enabled": bool(row["patrol_enabled"]),
            "patrol_suspended": bool(row["patrol_suspended"]), "ptz_owner": row["ptz_owner"],
            "floor_id": row["floor_id"], "orientation": json.loads(row["orientation_json"]),
            "capabilities": json.loads(row["capability_json"]), "updated_at": row["updated_at"],
        }

    def _camera_rows(self) -> list[dict]:
        rows = self.store.conn.execute("SELECT * FROM cameras ORDER BY name COLLATE NOCASE,camera_id").fetchall()
        cameras = [self._camera(row) for row in rows]
        for camera in cameras:
            status = self.camera_runtime.status(camera["camera_id"])
            camera["connected"] = bool(status.get("connected"))
            camera["connection_error"] = status.get("error") or ""
            camera["last_frame_at"] = status.get("updated_at")
        return cameras

    def _camera_secret(self, camera_id: str) -> str:
        row = self.store.conn.execute("SELECT credential_blob FROM cameras WHERE camera_id=?", (camera_id,)).fetchone()
        blob = row["credential_blob"] if row else None
        return unprotect_text(bytes(blob)) if blob else ""

    def _sync_camera_runtime(self) -> None:
        rows = self.store.conn.execute("SELECT * FROM cameras ORDER BY camera_id").fetchall()
        pairs = []
        for row in rows:
            camera = self._camera(row)
            pairs.append((camera, self._camera_secret(camera["camera_id"])))
        self.camera_runtime.sync(pairs)

    def _rule_rows(self) -> list[dict]:
        rows = self.store.conn.execute("SELECT * FROM rules ORDER BY name COLLATE NOCASE,rule_id").fetchall()
        return [{
            "rule_id": row["rule_id"], "family": row["family"], "name": row["name"],
            "summary": row["summary"], "enabled": bool(row["enabled"]),
            "definition": json.loads(row["definition_json"]), "updated_at": row["updated_at"],
        } for row in rows]

    def _alarm(self) -> dict:
        row = self.store.conn.execute("SELECT * FROM alarm_state WHERE singleton=1").fetchone()
        return dict(row) if row else {"state": "UNKNOWN"}

    def snapshot(self) -> dict:
        return {
            "cameras": self._camera_rows(), "rules": self._rule_rows(), "alarm": self._alarm(),
            "perimeter": self.store.get_state("protection", {"enabled": False, "source": "default"}),
        }

    def _camera_list(self, _incoming: dict) -> dict:
        return {"cameras": self._camera_rows()}

    def _rule_list(self, _incoming: dict) -> dict:
        return {"rules": self._rule_rows()}

    def _alarm_get(self, _incoming: dict) -> dict:
        return {"alarm": self._alarm()}

    def _perimeter_get(self, _incoming: dict) -> dict:
        return {"perimeter": self.store.get_state("protection", {"enabled": False, "source": "default"})}

    def _perimeter_set(self, incoming: dict) -> dict:
        enabled = bool(incoming["payload"].get("enabled"))
        state = {"enabled": enabled, "source": str(incoming["payload"].get("source") or "operator"), "updated_at": time.time()}
        self.store.set_state("protection", state)
        self.record_event("perimeter.armed" if enabled else "perimeter.disarmed", state)
        return {"perimeter": state}

    def _camera_upsert(self, incoming: dict) -> dict:
        body = incoming["payload"]
        camera_id = str(body.get("camera_id") or uuid.uuid4())
        name = str(body.get("name") or "Camera").strip()
        if not name:
            raise ServiceCommandError("camera name is required")
        orientation = body.get("orientation") or {"north_degrees": 0.0, "native_inverted": False}
        capabilities = body.get("capabilities") or {}
        credential_blob = body.get("credential_blob")
        if "password" in body:
            credential_blob = protect_text(str(body.get("password") or ""))
        now = time.time()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO cameras(camera_id,name,provider,host,port,username,credential_blob,stream,buffer_width,buffer_fps,enabled,connected,patrol_enabled,patrol_suspended,ptz_owner,floor_id,orientation_json,capability_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET name=excluded.name,provider=excluded.provider,host=excluded.host,port=excluded.port,username=excluded.username,credential_blob=COALESCE(excluded.credential_blob,cameras.credential_blob),stream=excluded.stream,buffer_width=excluded.buffer_width,buffer_fps=excluded.buffer_fps,floor_id=excluded.floor_id,"
                "orientation_json=excluded.orientation_json,capability_json=excluded.capability_json,updated_at=excluded.updated_at",
                (camera_id, name, str(body.get("provider") or "tapo"), str(body.get("host") or ""), int(body.get("port", 554)), str(body.get("username") or ""), credential_blob, int(body.get("stream", 2)), int(body.get("buffer_width", 1280)), int(body.get("buffer_fps", 8)), int(bool(body.get("enabled", True))), 0, 0, 0, "IDLE", body.get("floor_id"),
                 json.dumps(orientation, sort_keys=True), json.dumps(capabilities, sort_keys=True), now),
            )
        self.record_event("camera.changed", {"camera_id": camera_id, "name": name}, {"camera_id": camera_id})
        self._sync_camera_runtime()
        return {"camera_id": camera_id}

    def _camera_remove(self, incoming: dict) -> dict:
        camera_id = str(incoming["entity_ids"].get("camera_id") or incoming["payload"].get("camera_id") or "")
        with self.store.transaction() as conn:
            removed = conn.execute("DELETE FROM cameras WHERE camera_id=?", (camera_id,)).rowcount
        if not removed:
            raise ServiceCommandError("camera was not found")
        self._sync_camera_runtime()
        self.record_event("camera.removed", {"camera_id": camera_id}, {"camera_id": camera_id})
        return {"camera_id": camera_id, "deleted": True}

    def _camera_discover(self, _incoming: dict) -> dict:
        return {"cameras": discover_local_cameras()}

    def _camera_test(self, incoming: dict) -> dict:
        camera_id = str(incoming["entity_ids"].get("camera_id") or incoming["payload"].get("camera_id") or "")
        row = self.store.conn.execute("SELECT * FROM cameras WHERE camera_id=?", (camera_id,)).fetchone()
        if not row:
            raise ServiceCommandError("camera was not found")
        camera = self._camera(row)
        status = self.camera_runtime.status(camera_id)
        return {"camera_id": camera_id, **status, "candidate_count": len(rtsp_candidates(camera, self._camera_secret(camera_id)))}

    def diagnostics(self) -> dict:
        return {
            "cameras": {
                str(camera["camera_id"]): self.camera_runtime.status(str(camera["camera_id"]))
                for camera in self.store.conn.execute("SELECT camera_id FROM cameras WHERE enabled=1 ORDER BY camera_id")
            }
        }

    def _camera_frame(self, incoming: dict) -> dict:
        camera_id = str(incoming["entity_ids"].get("camera_id") or incoming["payload"].get("camera_id") or "")
        if not self.store.conn.execute("SELECT 1 FROM cameras WHERE camera_id=?", (camera_id,)).fetchone():
            raise ServiceCommandError("camera was not found")
        return self.camera_runtime.frame(camera_id)

    def _camera_set_enabled(self, incoming: dict) -> dict:
        camera_id = str(incoming["entity_ids"].get("camera_id") or incoming["payload"].get("camera_id") or "")
        enabled = bool(incoming["payload"].get("enabled"))
        with self.store.transaction() as conn:
            row = conn.execute("SELECT camera_id FROM cameras WHERE camera_id=?", (camera_id,)).fetchone()
            if not row:
                raise ServiceCommandError("camera was not found")
            conn.execute("UPDATE cameras SET enabled=?,updated_at=? WHERE camera_id=?", (int(enabled), time.time(), camera_id))
        self.record_event("camera.enabled", {"camera_id": camera_id, "enabled": enabled}, {"camera_id": camera_id})
        self._sync_camera_runtime()
        return {"camera_id": camera_id, "enabled": enabled}

    def _ptz_nudge(self, incoming: dict) -> dict:
        camera_id = str(incoming["entity_ids"].get("camera_id") or "")
        row = self.store.conn.execute("SELECT * FROM cameras WHERE camera_id=?", (camera_id,)).fetchone()
        if not row:
            raise ServiceCommandError("camera was not found")
        camera = self._camera(row)
        pan = float(incoming["payload"].get("pan", 0.0))
        tilt = float(incoming["payload"].get("tilt", 0.0))
        zoom = float(incoming["payload"].get("zoom", 0.0))
        orientation = camera.get("orientation") or {}
        # Either correction changes the visual frame presented to the user. Apply
        # one PTZ inversion when any source says the image is flipped; do not require
        # both flags and do not cancel them when both happen to be set.
        controls_inverted = any(bool(orientation.get(key, False)) for key in (
            "software_flip_active", "tapo_flip_active", "app_flip_active", "native_inverted",
        ))
        if controls_inverted:
            pan, tilt = -pan, -tilt
        # Prefer pytapo's native PTZ control for Tapo cameras; fall back to ONVIF
        # for non-Tapo providers. This avoids the ONVIF WSDL dependency for the
        # Tapo fleet.
        provider = str(camera.get("provider") or "").lower()
        if provider == "tapo":
            result = tapo_nudge(
                camera, self._camera_secret(camera_id),
                pan, tilt, zoom,
            )
        else:
            result = onvif_nudge(
                camera, self._camera_secret(camera_id),
                pan, tilt, zoom,
            )
        result["controls_inverted"] = controls_inverted
        self.record_event("ptz.moved", {"camera_id": camera_id, **result}, {"camera_id": camera_id})
        return {"camera_id": camera_id, **result}

    def stop(self) -> None:
        self.camera_runtime.close()
        super().stop()

    def _provider_analytics_event(self, incoming: dict) -> dict:
        body = incoming["payload"]
        camera_id = str(incoming["entity_ids"].get("camera_id") or body.get("camera_id") or "")
        return self._dispatch_provider_analytics(
            camera_id,
            str(body.get("signal") or ""),
            provider=str(body.get("provider") or ""),
            provenance=dict(body.get("provenance") or {}),
            face_candidates=list(body.get("face_candidates") or []),
            object_detections=list(body.get("object_detections") or []),
        )

    def _native_analytics_event(self, camera_id: str, signal: str, details: dict) -> None:
        """Receive a trusted event directly from Core's camera-owned subscriber."""
        try:
            # Some Tapo firmware exposes the vendor's human alert as a generic
            # ONVIF motion topic. Treat it as a human *candidate*: AI still has to
            # locate a person before any identity or Unknown event is produced.
            effective_signal = "human_candidate" if signal == "motion_detected" else signal
            self._dispatch_provider_analytics(
                camera_id, effective_signal, provider="onvif",
                provenance={"source_type": "camera_event", "native_signal": signal, **dict(details or {})},
            )
        except Exception as exc:
            self.logger.warning("provider.analytics_handoff_failed", camera_id=camera_id, signal=signal, error=str(exc))

    def _dispatch_provider_analytics(
        self, camera_id: str, signal: str, *, provider: str = "", provenance: dict | None = None,
        face_candidates: list | None = None, object_detections: list | None = None,
    ) -> dict:
        camera = self.store.conn.execute(
            "SELECT camera_id,provider,enabled FROM cameras WHERE camera_id=?", (camera_id,)
        ).fetchone()
        if not camera or not bool(camera["enabled"]):
            raise ServiceCommandError("provider event camera is unavailable or disabled")
        signal = str(signal or "").strip().lower().replace("-", "_").replace(" ", "_")
        human_equivalents = {"human", "human_detected", "human_candidate", "person", "person_detected", "people", "people_detected"}
        human_equivalent = signal in human_equivalents or any(signal.endswith("_" + value) for value in human_equivalents)
        self.record_event("provider.analytics", {
            "camera_id": camera_id, "provider": str(provider or camera["provider"]),
            "signal": signal, "human_equivalent": human_equivalent,
        }, {"camera_id": camera_id})
        if not human_equivalent:
            return {"camera_id": camera_id, "signal": signal, "recognition_triggered": False}
        provenance = dict(provenance or {})
        current_frame = self.camera_runtime.frame(camera_id)
        if not current_frame.get("available"):
            raise ServiceCommandError(str(current_frame.get("error") or "camera has not supplied a frame for provider-triggered recognition"))
        provenance.update({
            "source_type": "camera_event", "camera_id": camera_id,
            "frame_id": current_frame.get("frame_id"), "captured_utc": current_frame.get("captured_utc"),
            "captured_monotonic": current_frame.get("captured_monotonic"),
        })
        capability = issue_capability(self.authkey, subject=self.component, scopes=["ai.submit"])
        result = IPCClient("ai", self.authkey, identity="core").send(request(
            "provider.human_signal", source=self.component,
            payload={
                "signal": signal, "provider": str(provider or camera["provider"]),
                "provenance": provenance, "face_candidates": list(face_candidates or []),
                "object_detections": list(object_detections or []),
            }, entity_ids={"camera_id": camera_id}, authorization=capability,
        ))
        response = result.get("payload") or {}
        if not response.get("ok"):
            raise ServiceCommandError(str(response.get("error") or "AI service rejected the human signal"))
        return {
            "camera_id": camera_id, "signal": signal, "recognition_triggered": True,
            "job_id": response.get("job_id"), "recognition_policy": response.get("recognition_policy"),
        }

    def _patrol_set(self, incoming: dict) -> dict:
        camera_id = str(incoming["entity_ids"].get("camera_id") or incoming["payload"].get("camera_id") or "")
        enabled = bool(incoming["payload"].get("enabled"))
        with self.store.transaction() as conn:
            row = conn.execute("SELECT ptz_owner FROM cameras WHERE camera_id=?", (camera_id,)).fetchone()
            if not row:
                raise ServiceCommandError("camera was not found")
            suspended = enabled and PTZ_PRIORITY.get(row["ptz_owner"], 0) > PTZ_PRIORITY["PATROL"]
            conn.execute(
                "UPDATE cameras SET patrol_enabled=?,patrol_suspended=?,updated_at=? WHERE camera_id=?",
                (int(enabled), int(suspended), time.time(), camera_id),
            )
        self.record_event("patrol.changed", {"camera_id": camera_id, "enabled": enabled, "suspended": suspended}, {"camera_id": camera_id})
        return {"camera_id": camera_id, "enabled": enabled, "suspended": suspended}

    def _ptz_claim(self, incoming: dict) -> dict:
        camera_id = str(incoming["entity_ids"].get("camera_id") or "")
        owner = str(incoming["payload"].get("owner") or "").upper()
        if owner not in PTZ_PRIORITY:
            raise ServiceCommandError("invalid PTZ owner")
        with self.store.transaction() as conn:
            row = conn.execute("SELECT ptz_owner,patrol_enabled FROM cameras WHERE camera_id=?", (camera_id,)).fetchone()
            if not row:
                raise ServiceCommandError("camera was not found")
            current = row["ptz_owner"]
            if PTZ_PRIORITY[owner] < PTZ_PRIORITY.get(current, 0):
                return {"camera_id": camera_id, "granted": False, "owner": current}
            suspended = bool(row["patrol_enabled"]) and owner != "PATROL"
            conn.execute("UPDATE cameras SET ptz_owner=?,patrol_suspended=?,updated_at=? WHERE camera_id=?", (owner, int(suspended), time.time(), camera_id))
        self.record_event("ptz.owner", {"camera_id": camera_id, "owner": owner, "patrol_suspended": suspended}, {"camera_id": camera_id})
        return {"camera_id": camera_id, "granted": True, "owner": owner, "patrol_suspended": suspended}

    def _ptz_release(self, incoming: dict) -> dict:
        camera_id = str(incoming["entity_ids"].get("camera_id") or "")
        owner = str(incoming["payload"].get("owner") or "").upper()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT ptz_owner,patrol_enabled FROM cameras WHERE camera_id=?", (camera_id,)).fetchone()
            if not row:
                raise ServiceCommandError("camera was not found")
            if row["ptz_owner"] != owner:
                return {"camera_id": camera_id, "released": False, "owner": row["ptz_owner"]}
            next_owner = "PATROL" if bool(row["patrol_enabled"]) else "IDLE"
            conn.execute("UPDATE cameras SET ptz_owner=?,patrol_suspended=0,updated_at=? WHERE camera_id=?", (next_owner, time.time(), camera_id))
        self.record_event("ptz.owner", {"camera_id": camera_id, "owner": next_owner}, {"camera_id": camera_id})
        return {"camera_id": camera_id, "released": True, "owner": next_owner}

    def _rule_save(self, incoming: dict) -> dict:
        body = incoming["payload"]
        rule_id = str(body.get("rule_id") or uuid.uuid4())
        name = str(body.get("name") or "Rule").strip()
        family = str(body.get("family") or "household").lower()
        if family not in {"household", "property"}:
            raise ServiceCommandError("Rule family must be household or property")
        definition = body.get("definition") or {}
        if int(definition.get("wizard_version") or 0) >= 2:
            subject = str(definition.get("subject_id") or "")
            location_id = str(definition.get("location_id") or "")
            if not subject or not location_id:
                raise ServiceCommandError("guided Rule subject and location selections are required")
            if str(definition.get("trigger") or "") not in GUIDED_RULE_TRIGGERS:
                raise ServiceCommandError("guided Rule trigger is not supported")
            if str(definition.get("schedule") or "") not in GUIDED_RULE_SCHEDULES:
                raise ServiceCommandError("guided Rule schedule is not supported")
            if str(definition.get("response") or "") not in GUIDED_RULE_RESPONSES:
                raise ServiceCommandError("guided Rule response is not supported")
            if str(definition.get("exception") or "") not in GUIDED_RULE_EXCEPTIONS:
                raise ServiceCommandError("guided Rule exception is not supported")
        summary = str(body.get("summary") or "").strip()
        if not summary:
            raise ServiceCommandError("plain-language Rule summary is required")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO rules(rule_id,family,name,summary,enabled,definition_json,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(rule_id) DO UPDATE SET family=excluded.family,name=excluded.name,summary=excluded.summary,"
                "enabled=excluded.enabled,definition_json=excluded.definition_json,updated_at=excluded.updated_at",
                (rule_id, family, name, summary, int(bool(body.get("enabled", True))), json.dumps(definition, sort_keys=True), time.time()),
            )
        self.record_event("rule.changed", {"rule_id": rule_id, "summary": summary}, {"rule_id": rule_id})
        return {"rule_id": rule_id}

    def _rule_delete(self, incoming: dict) -> dict:
        rule_id = str(incoming["entity_ids"].get("rule_id") or "")
        with self.store.transaction() as conn:
            removed = conn.execute("DELETE FROM rules WHERE rule_id=?", (rule_id,)).rowcount
        if not removed:
            raise ServiceCommandError("Rule was not found")
        self.record_event("rule.deleted", {"rule_id": rule_id}, {"rule_id": rule_id})
        return {"rule_id": rule_id, "deleted": True}

    def _alarm_trigger(self, incoming: dict) -> dict:
        emergency = str(incoming["payload"].get("emergency_type") or "SECURITY").upper()
        if emergency not in {"SECURITY", "FIRE", "POLICE", "MEDICAL"}:
            raise ServiceCommandError("invalid emergency type")
        event_id = str(uuid.uuid4())
        with self.store.transaction() as conn:
            conn.execute("UPDATE alarm_state SET state='ALARM',emergency_type=?,event_id=?,updated_at=? WHERE singleton=1", (emergency, event_id, time.time()))
        self.record_event("alarm.triggered", {"event_id": event_id, "emergency_type": emergency}, {"event_id": event_id})
        return {"state": "ALARM", "event_id": event_id, "emergency_type": emergency, "dispatch_claimed": False}

    def _alarm_clear(self, incoming: dict) -> dict:
        with self.store.transaction() as conn:
            conn.execute("UPDATE alarm_state SET state='SAFE',emergency_type=NULL,event_id=NULL,updated_at=? WHERE singleton=1", (time.time(),))
        self.record_event("alarm.cleared", {"reason": str(incoming["payload"].get("reason") or "operator")})
        return {"state": "SAFE"}

    def _protection_stop(self, incoming: dict) -> dict:
        reason = str(incoming["payload"].get("reason") or "authorized operator")
        self.store.set_state("protection", {"enabled": False, "reason": reason, "updated_at": time.time()})
        self.record_event("protection.stopped", {"reason": reason})
        return {"protection_enabled": False}
