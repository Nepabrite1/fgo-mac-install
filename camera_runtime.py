"""Core-owned RTSP/ONVIF interoperability for Tapo and compatible cameras.

The desktop client never receives camera credentials.  Core opens and maintains
the stream, then returns only a short-lived JPEG preview over authenticated IPC.
"""

from __future__ import annotations

import base64
import asyncio
import concurrent.futures
import select
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable
from urllib.parse import quote


def rtsp_candidates(camera: dict, password: str = "") -> list[str]:
    host = str(camera.get("host") or "").strip()
    port = int(camera.get("port") or 554)
    user = quote(str(camera.get("username") or ""), safe="")
    secret = quote(password, safe="")
    authority = f"{user}:{secret}@" if user or secret else ""
    stream = int(camera.get("stream", 2))
    paths = [f"/stream{stream}"]
    if str(camera.get("provider") or "").lower() == "tapo":
        paths.extend(path for path in ("/stream2", "/stream1") if path not in paths)
    custom = str((camera.get("capabilities") or {}).get("rtsp_path") or "").strip()
    if custom:
        paths.insert(0, custom if custom.startswith("/") else "/" + custom)
    return [f"rtsp://{authority}{host}:{port}{path}" for path in dict.fromkeys(paths)]


class _FFmpegCapture:
    """cv2.VideoCapture-compatible RTSP reader backed by an FFmpeg subprocess.

    Some OpenCV builds are compiled without FFmpeg or fail to open RTSP over the
    default UDP transport (e.g. certain Intel-macOS wheels / Tapo cameras). This
    decodes the RTSP stream to raw BGR frames with an external FFmpeg (forced TCP
    transport) and exposes ``isOpened()``/``read()`` so the existing capture loop
    is unchanged.
    """

    def __init__(self, url: str, width: int, height: int, timeout: float = 1.5):
        self._url = url
        self._w = max(160, int(width))
        self._h = max(120, int(height))
        self._frame_size = self._w * self._h * 3
        self._timeout = float(timeout)
        self._proc = None
        self._closed = False
        self.open()

    @staticmethod
    def _exe() -> str:
        try:
            import imageio_ffmpeg  # provides a bundled ffmpeg binary
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return "ffmpeg"

    def open(self) -> bool:
        cmd = [
            self._exe(), "-nostdin", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-i", self._url,
            "-an", "-sn", "-dn",
            "-vf", "scale=%d:%d" % (self._w, self._h),
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
            )
        except Exception:
            self._proc = None
            return False
        return True

    def isOpened(self) -> bool:
        if self._proc is None or self._closed:
            return False
        return self._proc.poll() is None and self._proc.stdout is not None

    def read(self):
        if not self.isOpened():
            return False, None
        import numpy as np
        frame = b""
        deadline = time.monotonic() + self._timeout
        while len(frame) < self._frame_size:
            if self._closed:
                return False, None
            fd = None
            try:
                fd = self._proc.stdout.fileno()
            except Exception:
                fd = None
            ready = True
            if fd is not None:
                try:
                    readable, _, _ = select.select([fd], [], [], 0.5)
                    ready = bool(readable)
                except Exception:
                    ready = True  # non-posix pipe fallback: block on read
            if not ready:
                if time.monotonic() >= deadline:
                    return False, None
                continue
            chunk = self._proc.stdout.read(self._frame_size - len(frame))
            if not chunk:
                return False, None
            frame += chunk
        return True, np.frombuffer(frame[: self._frame_size], dtype=np.uint8).reshape(
            (self._h, self._w, 3)
        )

    def close(self) -> None:
        self._closed = True
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except Exception:
                    self._proc.kill()
            except Exception:
                pass
            self._proc = None


def local_ipv4() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        probe.close()


def discover_local_cameras(timeout: float = 0.12) -> list[dict]:
    """Find likely RTSP/ONVIF devices on the local /24 without credentials."""
    address = local_ipv4()
    parts = address.split(".")
    if len(parts) != 4 or address.startswith("127."):
        return []
    prefix = ".".join(parts[:3])

    def probe(index: int) -> dict | None:
        host = f"{prefix}.{index}"
        ports = []
        for port in (554, 2020):
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    ports.append(port)
            except OSError:
                pass
        if not ports:
            return None
        return {"host": host, "rtsp": 554 in ports, "onvif": 2020 in ports, "ports": ports}

    with concurrent.futures.ThreadPoolExecutor(max_workers=48, thread_name_prefix="FGO-Camera-Discovery") as pool:
        found = [item for item in pool.map(probe, range(1, 255)) if item]
    return sorted(found, key=lambda item: tuple(int(part) for part in item["host"].split(".")))


@dataclass
class _Stream:
    camera: dict
    password: str
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    frame: bytes | None = None
    connected: bool = False
    error: str = "waiting for first frame"
    url_index: int = 0
    updated_at: float = 0.0
    updated_monotonic: float = 0.0
    frame_id: str = ""
    opened_at: float = 0.0
    reconnects: int = 0
    decoded_frames: int = 0
    dropped_frames: int = 0
    skipped_frames: int = 0
    active_candidate: str = ""
    thread: threading.Thread | None = None
    analytics_thread: threading.Thread | None = None
    analytics_connected: bool = False
    analytics_error: str = ""
    analytics_events: int = 0
    last_analytics_signal: str = ""
    last_analytics_at: float = 0.0


class CameraRuntime:
    """Maintains one resilient, service-owned capture worker per enabled camera."""

    def __init__(self, analytics_callback: Callable[[str, str, dict], None] | None = None):
        self._streams: dict[str, _Stream] = {}
        self._lock = threading.RLock()
        self._analytics_callback = analytics_callback

    def sync(self, cameras: list[tuple[dict, str]]) -> None:
        wanted = {str(camera["camera_id"]): (camera, password) for camera, password in cameras if camera.get("enabled")}
        with self._lock:
            for camera_id in set(self._streams) - set(wanted):
                self._stop_locked(camera_id)
            for camera_id, (camera, password) in wanted.items():
                existing = self._streams.get(camera_id)
                signature = (
                    camera.get("host"), camera.get("port"), camera.get("username"), camera.get("stream"),
                    camera.get("buffer_width"), camera.get("buffer_fps"), repr(camera.get("capabilities") or {}), password,
                )
                old_signature = None if not existing else (
                    existing.camera.get("host"), existing.camera.get("port"), existing.camera.get("username"), existing.camera.get("stream"),
                    existing.camera.get("buffer_width"), existing.camera.get("buffer_fps"), repr(existing.camera.get("capabilities") or {}), existing.password,
                )
                if existing and signature == old_signature:
                    continue
                if existing:
                    self._stop_locked(camera_id)
                stream = _Stream(dict(camera), password)
                stream.thread = threading.Thread(target=self._capture, args=(stream,), name=f"FGO-Camera-{camera_id[:8]}", daemon=True)
                self._streams[camera_id] = stream
                stream.thread.start()
                if self._analytics_callback and str(camera.get("provider") or "").lower() in {"tapo", "onvif"} and (camera.get("capabilities") or {}).get("analytics_events", True):
                    stream.analytics_thread = threading.Thread(
                        target=self._analytics,
                        args=(stream, self._analytics_callback),
                        name=f"FGO-Analytics-{camera_id[:8]}",
                        daemon=True,
                    )
                    stream.analytics_thread.start()

    def _stop_locked(self, camera_id: str) -> None:
        stream = self._streams.pop(camera_id, None)
        if stream:
            stream.stop.set()

    @staticmethod
    def _open_capture(cv2, url: str):
        """Open RTSP using both modern and proven v1.3 OpenCV sequences.

        Some Windows OpenCV/FFmpeg builds accept timeout properties only in the
        ``open`` parameter vector.  Others reject that vector and require the v1.3
        property-before-open sequence.  Treating a false return as final made valid
        Tapo streams remain disconnected even though the same camera worked in 1.3.
        """
        parameters = []
        if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
            parameters.extend([cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 4000])
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            parameters.extend([cv2.CAP_PROP_READ_TIMEOUT_MSEC, 4000])

        def configured_capture():
            candidate = cv2.VideoCapture()
            for property_name, value in (
                ("CAP_PROP_OPEN_TIMEOUT_MSEC", 4000),
                ("CAP_PROP_READ_TIMEOUT_MSEC", 4000),
                ("CAP_PROP_BUFFERSIZE", 1),
            ):
                if hasattr(cv2, property_name):
                    try:
                        candidate.set(getattr(cv2, property_name), value)
                    except Exception:
                        pass
            return candidate

        capture = configured_capture()
        try:
            opened = bool(capture.open(url, cv2.CAP_FFMPEG, parameters)) if parameters else False
        except (TypeError, ValueError, RuntimeError):
            opened = False
        if not opened or not bool(getattr(capture, "isOpened", lambda: opened)()):
            capture.release()
            capture = configured_capture()
            try:
                opened = bool(capture.open(url, cv2.CAP_FFMPEG))
            except Exception:
                opened = False
        if not opened or not bool(getattr(capture, "isOpened", lambda: opened)()):
            capture.release()
            capture = configured_capture()
            try:
                capture.open(url)
            except Exception:
                pass
        return capture

    @staticmethod
    def _capture(stream: _Stream) -> None:
        try:
            import cv2
        except Exception as exc:
            with stream.lock:
                stream.connected = False
                stream.error = f"camera runtime is unavailable: {exc}"
            return
        urls = rtsp_candidates(stream.camera, stream.password)
        while not stream.stop.is_set():
            url = urls[stream.url_index % len(urls)]
            if url.lower().startswith("rtsp://"):
                width = int(stream.camera.get("buffer_width") or 1280)
                height = max(1, round(width * 9 / 16))
                capture = _FFmpegCapture(url, width, height)
            else:
                capture = CameraRuntime._open_capture(cv2, url)
            try:
                if not capture.isOpened():
                    raise RuntimeError("RTSP session could not be opened")
                with stream.lock:
                    stream.opened_at = time.time()
                    stream.active_candidate = f"candidate-{stream.url_index % len(urls) + 1}"
                failures = 0
                last_encoded = 0.0
                snapshot_fps = max(1.0, min(float(stream.camera.get("buffer_fps") or 4), 4.0))
                snapshot_interval = 1.0 / snapshot_fps
                while not stream.stop.is_set() and capture.isOpened():
                    ok, frame = capture.read()
                    if not ok:
                        failures += 1
                        with stream.lock:
                            stream.dropped_frames += 1
                        if failures >= 3:
                            break
                        time.sleep(0.05)
                        continue
                    failures = 0
                    captured_monotonic = time.monotonic()
                    if last_encoded and captured_monotonic - last_encoded < snapshot_interval:
                        with stream.lock:
                            stream.skipped_frames += 1
                        continue
                    width = int(stream.camera.get("buffer_width") or 1280)
                    if frame.shape[1] > width:
                        height = max(1, round(frame.shape[0] * width / frame.shape[1]))
                        frame = cv2.resize(frame, (width, height))
                    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
                    if ok:
                        with stream.lock:
                            stream.frame = encoded.tobytes()
                            stream.connected = True
                            stream.error = ""
                            stream.updated_at = time.time()
                            stream.updated_monotonic = captured_monotonic
                            stream.frame_id = str(uuid.uuid4())
                            stream.decoded_frames += 1
                        last_encoded = captured_monotonic
                if not stream.stop.is_set():
                    with stream.lock:
                        stream.connected = False
                        stream.error = "Camera stopped delivering frames; Core is reconnecting automatically"
            except Exception as exc:
                with stream.lock:
                    stream.connected = False
                    stream.error = f"{str(exc)[:150]}; Core is trying the next Tapo stream"
            finally:
                closer = getattr(capture, "close", None) or getattr(capture, "release", None)
                if closer is not None:
                    try:
                        closer()
                    except Exception:
                        pass
            stream.url_index = (stream.url_index + 1) % len(urls)
            with stream.lock:
                stream.reconnects += 1
            stream.stop.wait(1.5)

    @staticmethod
    def _event_text(value, *, depth: int = 0) -> list[str]:
        """Flatten a bounded ONVIF/Zeep notification without retaining secrets."""
        if value is None or depth > 7:
            return []
        if isinstance(value, (str, int, float, bool)):
            return [str(value)]
        if isinstance(value, dict):
            values: list[str] = []
            for key, item in list(value.items())[:80]:
                values.append(str(key))
                values.extend(CameraRuntime._event_text(item, depth=depth + 1))
            return values
        if isinstance(value, (list, tuple)):
            values = []
            for item in value[:80]:
                values.extend(CameraRuntime._event_text(item, depth=depth + 1))
            return values
        try:
            from zeep.helpers import serialize_object
            serialized = serialize_object(value)
            if serialized is not value:
                return CameraRuntime._event_text(serialized, depth=depth + 1)
        except Exception:
            pass
        return [str(value)[:500]]

    @staticmethod
    def _analytics(stream: _Stream, callback: Callable[[str, str, dict], None]) -> None:
        """Subscribe to ONVIF + Tapo person-detection events."""
        provider = str(stream.camera.get("provider") or "").lower()
        onvif_thread = threading.Thread(
            target=lambda: asyncio.run(CameraRuntime._analytics_async(stream, callback)),
            name=f"FGO-ONVIF-Analytics-{stream.camera.get('camera_id','')}",
            daemon=True,
        )
        onvif_thread.start()
        if provider == "tapo":
            tapo_thread = threading.Thread(
                target=lambda: asyncio.run(CameraRuntime._tapo_analytics_async(stream, callback)),
                name=f"FGO-Tapo-Analytics-{stream.camera.get('camera_id','')}",
                daemon=True,
            )
            tapo_thread.start()
            onvif_thread.join()
            tapo_thread.join()
        else:
            onvif_thread.join()

    @staticmethod
    async def _analytics_async(stream: _Stream, callback: Callable[[str, str, dict], None]) -> None:
        camera_id = str(stream.camera.get("camera_id") or "")
        host = str(stream.camera.get("host") or "")
        username = str(stream.camera.get("username") or "")
        capabilities = stream.camera.get("capabilities") or {}
        port = int(capabilities.get("onvif_port") or 2020)
        failures = 0
        while not stream.stop.is_set():
            camera = None
            manager = None
            try:
                from onvif import ONVIFCamera
                camera = ONVIFCamera(host, port, username, stream.password)
                manager = await camera.create_pullpoint_manager(
                    timedelta(minutes=10), lambda: None,
                )
                pullpoint = manager.get_service()
                request_body = pullpoint.create_type("PullMessages")
                request_body.Timeout = timedelta(seconds=8)
                request_body.MessageLimit = 32
                with stream.lock:
                    stream.analytics_connected = True
                    stream.analytics_error = ""
                failures = 0
                while not stream.stop.is_set():
                    try:
                        response = await pullpoint.PullMessages(request_body)
                    except Exception as exc:
                        if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                            continue
                        raise
                    messages = getattr(response, "NotificationMessage", None) or []
                    if not isinstance(messages, (list, tuple)):
                        messages = [messages]
                    for message in messages:
                        flattened = " ".join(CameraRuntime._event_text(message)).lower()
                        active = not any(token in flattened for token in ("value false", "value 0", "state false", "state 0"))
                        if not active:
                            continue
                        if any(token in flattened for token in ("person", "people", "human", "pedestrian")):
                            signal = "person_detected"
                        elif any(token in flattened for token in ("motion", "cellmotion", "is_motion")):
                            signal = "motion_detected"
                        else:
                            continue
                        now = time.time()
                        with stream.lock:
                            quiet_seconds = 10.0 if signal == "motion_detected" else 3.0
                            if signal == stream.last_analytics_signal and now - stream.last_analytics_at < quiet_seconds:
                                continue
                            stream.last_analytics_signal = signal
                            stream.last_analytics_at = now
                            stream.analytics_events += 1
                        callback(camera_id, signal, {"source": "onvif_pullpoint", "received_at": now})
            except Exception as exc:
                failures += 1
                with stream.lock:
                    stream.analytics_connected = False
                    stream.analytics_error = str(exc)[:180]
            finally:
                if manager is not None:
                    try:
                        await manager.shutdown()
                    except Exception:
                        pass
                if camera is not None:
                    try:
                        await camera.close()
                    except Exception:
                        pass
            if not stream.stop.is_set():
                await asyncio.sleep(min(60.0, 5.0 * (2 ** min(failures, 4))))

    @staticmethod
    async def _tapo_analytics_async(stream: _Stream, callback: Callable[[str, str, dict], None]) -> None:
        """Poll Tapo cameras for person-detection events via pytapo library."""
        camera_id = str(stream.camera.get("camera_id") or "")
        host = str(stream.camera.get("host") or "")
        username = str(stream.camera.get("username") or "")
        if not host or not username or not stream.password:
            return
        failures = 0
        last_event_id = None
        while not stream.stop.is_set():
            try:
                from pytapo import Tapo
                device = Tapo(host, username, stream.password)
                # Use getEvents for real-time person-detection events (more reliable than polling config)
                try:
                    events = device.getEvents()
                    if events:
                        for evt in (events if isinstance(events, list) else [events]):
                            eid = str(evt.get("event_id", evt.get("id", "")))
                            etype = str(evt.get("type", evt.get("event_type", ""))).lower()
                            # Person-related events
                            if "person" in etype or "people" in etype or "human" in etype:
                                if eid != last_event_id:
                                    last_event_id = eid
                                    now = time.time()
                                    with stream.lock:
                                        stream.last_analytics_signal = "person_detected"
                                        stream.last_analytics_at = now
                                        stream.analytics_events += 1
                                    callback(camera_id, "person_detected", {"source": "tapo_pytapo_events", "received_at": now, "event_type": etype})
                except Exception:
                    # Fallback: poll person detection config state
                    person_cfg = device.getPersonDetection()
                    if person_cfg and person_cfg not in (False, "false", "0", 0):
                        now = time.time()
                        with stream.lock:
                            if stream.last_analytics_signal != "person_detected" or now - stream.last_analytics_at > 3.0:
                                stream.last_analytics_signal = "person_detected"
                                stream.last_analytics_at = now
                                stream.analytics_events += 1
                        callback(camera_id, "person_detected", {"source": "tapo_pytapo_config", "received_at": now})
                failures = 0
            except ImportError:
                with stream.lock:
                    stream.analytics_error = "pytapo library not installed"
                break
            except Exception as exc:
                failures += 1
                err_msg = str(exc)[:180]
                with stream.lock:
                    stream.analytics_error = err_msg
                if "auth" in err_msg.lower() or "credential" in err_msg.lower():
                    break
            await asyncio.sleep(1.5 + min(15.0, 2.0 * failures))


    def status(self, camera_id: str) -> dict:
        with self._lock:
            stream = self._streams.get(camera_id)
        if not stream:
            return {"connected": False, "error": "camera is disabled"}
        with stream.lock:
            age = None if not stream.updated_at else max(0.0, time.time() - stream.updated_at)
            return {
                "connected": stream.connected, "error": stream.error,
                "updated_at": stream.updated_at, "frame_age_seconds": age,
                "opened_at": stream.opened_at, "reconnects": stream.reconnects,
                "decoded_frames": stream.decoded_frames, "dropped_frames": stream.dropped_frames,
                "skipped_frames": stream.skipped_frames,
                "active_candidate": stream.active_candidate,
                "analytics_connected": stream.analytics_connected,
                "analytics_error": stream.analytics_error,
                "analytics_events": stream.analytics_events,
                "last_analytics_signal": stream.last_analytics_signal,
                "last_analytics_at": stream.last_analytics_at,
            }

    def frame(self, camera_id: str) -> dict:
        with self._lock:
            stream = self._streams.get(camera_id)
        if not stream:
            return {"available": False, "connected": False, "error": "camera is disabled"}
        with stream.lock:
            if not stream.frame:
                return {"available": False, "connected": stream.connected, "error": stream.error}
            return {
                "available": True, "connected": stream.connected, "mime": "image/jpeg",
                "jpeg_base64": base64.b64encode(stream.frame).decode("ascii"),
                "frame_id": stream.frame_id,
                "captured_at": stream.updated_at,
                "captured_utc": stream.updated_at,
                "captured_monotonic": stream.updated_monotonic,
                "orientation": dict(stream.camera.get("orientation") or {}),
                "source": {"type": "camera", "camera_id": camera_id},
                "error": stream.error,
            }

    def close(self) -> None:
        with self._lock:
            for camera_id in list(self._streams):
                self._stop_locked(camera_id)


def onvif_nudge(camera: dict, password: str, pan: float, tilt: float, zoom: float = 0.0) -> dict:
    """Perform a bounded absolute nudge using the proven v1.3 ONVIF adapter."""
    from homesentinel.survey import OnvifPTZController
    controller = OnvifPTZController(
        str(camera.get("host") or ""), str(camera.get("username") or ""), password,
        port=int((camera.get("capabilities") or {}).get("onvif_port") or 2020),
    )
    state = controller.snapshot_state()
    ranges = state.get("ranges") or {}
    pan_range = ranges.get("pan") or [-1.0, 1.0]
    tilt_range = ranges.get("tilt") or [-1.0, 1.0]
    zoom_range = ranges.get("zoom") or [0.0, 1.0]
    target_pan = max(float(pan_range[0]), min(float(pan_range[1]), float(state.get("pan") or 0.0) + float(pan)))
    target_tilt = max(float(tilt_range[0]), min(float(tilt_range[1]), float(state.get("tilt") or 0.0) + float(tilt)))
    target_zoom = max(float(zoom_range[0]), min(float(zoom_range[1]), float(state.get("zoom") or 0.0) + float(zoom)))
    controller.move_absolute(target_pan, target_tilt, target_zoom)
    return {"pan": target_pan, "tilt": target_tilt, "zoom": target_zoom}


def _tapo_float(value, default: float = 0.0) -> float:
    """Coerce a Tapo API value to a float, returning ``default`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tapo_find(node, key: str):
    """Recursively search a nested Tapo response for ``key``."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _tapo_find(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _tapo_find(value, key)
            if found is not None:
                return found
    return None


def tapo_nudge(camera: dict, password: str, pan: float, tilt: float, zoom: float = 0.0) -> dict:
    """Move a Tapo camera using the mihai-dinculescu/tapo library's PTZ control.

    This replaces the pytapo integration, which did not reliably move the camera.
    The ``tapo`` library (``pip install tapo``) exposes
    ``CameraPtzHandler.pan_tilt(pan, tilt)`` for relative pan/tilt movement using
    integer steps (``10`` is documented as a good value for small nudges).

    FGO's ``pan``/``tilt`` are normalized relative increments in ``[-1, 1]``, so
    they are scaled so that a ``0.10`` nudge maps to a step of ``10``. Zoom is not
    exposed through this relative API on most Tapo models, so it is recorded but
    not applied.
    """
    import asyncio
    from tapo import ApiClient
    host = str(camera.get("host") or "")
    username = str(camera.get("username") or "")
    if not host or not username:
        raise ValueError("Tapo camera is missing host or username")
    # Scale normalized FGO deltas to the tapo pan_tilt integer scale. The tapo
    # library documents "10 for both pan and tilt are good values for small
    # nudges", so a 0.10 normalized nudge maps to a step of 10.
    tapo_pan = int(round(_tapo_float(pan) * 100))
    tapo_tilt = int(round(_tapo_float(tilt) * 100))

    async def _move() -> None:
        # The tapo library's authentication can fail transiently (e.g. "missing
        # field stok" / "Invalid authentication data" on some firmware), so retry
        # a few times with a fresh client and short backoff before giving up.
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                client = ApiClient(username, password)
                # All supported Tapo PTZ models (C210/C220/C225/C325WB/C520WS/TC40/TC70)
                # share the same CameraPtzHandler; c210 is used as the generic handler.
                device = await client.c210(host)
                if tapo_pan or tapo_tilt:
                    await device.pan_tilt(tapo_pan, tapo_tilt)
                return
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.4 * (attempt + 1))
        if last_error is not None:
            raise last_error

    asyncio.run(_move())
    return {
        "pan": _tapo_float(pan),
        "tilt": _tapo_float(tilt),
        "zoom": _tapo_float(zoom),
        "provider": "tapo",
        "zoom_applied": False,
        "tapo_pan": tapo_pan,
        "tapo_tilt": tapo_tilt,
        "pan_range": 100,
        "tilt_range": 100,
    }
