"""Realtime media streaming channel, separate from the control gateway.

Design (per the architecture direction):

* The media/streaming server listens on its **own rotating TCP port**, distinct
  from the control gateway, so high-frequency live frames never contend with
  control commands (e.g. PTZ).
* A client authenticates **once via PKI** (``stream.hello``). The server issues a
  short-lived cryptographic **session token**. On later connections — including
  when the streaming port rotates — the client reconnects presenting the token
  (``stream.resume``) *without* another PKI handshake.
* Frames are **pushed** over a persistent connection (latest JPEG per camera),
  so there is no connect-per-frame churn and no rotate-triggered re-handshake.

The control gateway (``netgate``) continues to advertise this server's current
``stream_port``/``stream_epoch`` so clients discover it the same way they find
the control endpoint.
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from multiprocessing.context import AuthenticationError
from multiprocessing.connection import Client, Listener

from .pki import PKIError, sign_message, verify_message

DEFAULT_STREAM_PORT = 15196
# Framing authkey for the stream transport. The PKI handshake (one time) and the
# session token (on rotation) carry the real authority; this only guards framing
# and MUST match on both ends.
REMOTE_AUTHKEY = b"FirstGeneralOrder-stream-v1"
SESSION_TTL_SECONDS = 3600.0
DEFAULT_PUSH_INTERVAL = 0.25  # up to ~4fps, matching the camera snapshot rate


def _stream_json_path() -> str:
    from .paths import runtime_dir
    return str(runtime_dir(create=True) / "stream.json")


def write_stream_json(port: int | None, epoch: int) -> None:
    """Persist the current streaming endpoint so the control gateway can merge it
    into its discovery beacon without sharing a process (separate services read it
    from the shared machine runtime dir)."""
    import json, os
    path = _stream_json_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"stream_port": port, "stream_epoch": int(epoch)}, handle)
        os.replace(tmp, path)
    except OSError:
        pass


def read_stream_json() -> dict:
    import json
    path = _stream_json_path()
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


class StreamSessionStore:
    """In-memory, one-time-PKI-issuable streaming session tokens."""

    def __init__(self, ttl: float = SESSION_TTL_SECONDS):
        self._ttl = float(ttl)
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, identity: str, cameras: list[str]) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {
                "identity": identity,
                "cameras": [str(c) for c in cameras],
                "expires": time.time() + self._ttl,
            }
        return token

    def valid(self, token: str) -> bool:
        with self._lock:
            session = self._sessions.get(token)
            if not session:
                return False
            if time.time() > session["expires"]:
                self._sessions.pop(token, None)
                return False
            return True

    def cameras_for(self, token: str) -> list[str]:
        with self._lock:
            session = self._sessions.get(token)
            return list(session["cameras"]) if session else []

    def identity_for(self, token: str) -> str | None:
        with self._lock:
            session = self._sessions.get(token)
            return session.get("identity") if session else None


class StreamServer:
    """Pushes the latest cached frame per camera over persistent connections.

    ``frame_source`` must be a callable ``(camera_id) -> dict`` that returns a
    frame payload (e.g. from ``CameraRuntime.frame``) or ``None`` when there is
    no frame yet.
    """

    def __init__(
        self,
        frame_source,
        *,
        bind_host: str = "0.0.0.0",
        rotate_min_seconds: int = 120,
        rotate_max_seconds: int = 300,
        push_interval: float = DEFAULT_PUSH_INTERVAL,
        allowed_identities=("client",),
    ):
        self.frame_source = frame_source
        self.bind_host = bind_host
        self.rotate_min = max(15, int(rotate_min_seconds))
        self.rotate_max = max(self.rotate_min, int(rotate_max_seconds))
        self.push_interval = max(0.05, float(push_interval))
        self.allowed_identities = tuple(allowed_identities)
        self.sessions = StreamSessionStore()
        self._listener: Listener | None = None
        self._current_port: int | None = None
        self._epoch = int(time.time() * 1000)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(32)

    def start(self) -> None:
        self._bind_new()
        self._rotator = threading.Thread(target=self._rotate_loop, name="FGO-stream-rotate", daemon=True)
        self._rotator.start()
        self._acceptor = threading.Thread(target=self._accept_loop, name="FGO-stream-accept", daemon=True)
        self._acceptor.start()

    @property
    def port(self) -> int | None:
        with self._lock:
            return self._current_port

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def beacon(self) -> dict:
        with self._lock:
            return {"stream_port": self._current_port, "stream_epoch": self._epoch}

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

    # -- internals -----------------------------------------------------------
    def _bind_new(self) -> None:
        old = self._listener
        while True:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind((self.bind_host, 0))
            port = probe.getsockname()[1]
            probe.close()
            try:
                new_listener = Listener((self.bind_host, port), family="AF_INET", authkey=REMOTE_AUTHKEY)
                break
            except OSError:
                continue
        with self._lock:
            self._listener = new_listener
            self._current_port = port
            self._epoch += 1
        if old is not None:
            try:
                old.close()
            except OSError:
                pass

    def _rotate_loop(self) -> None:
        while not self._stop.is_set():
            interval = secrets.randbelow(self.rotate_max - self.rotate_min + 1) + self.rotate_min
            if self._stop.wait(interval):
                break
            self._bind_new()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                connection = self._listener.accept()
            except AuthenticationError:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            if not self._slots.acquire(blocking=False):
                try:
                    connection.close()
                except OSError:
                    pass
                continue
            threading.Thread(
                target=self._handle, args=(connection,), name="FGO-stream-handler", daemon=True
            ).start()

    def _handle(self, connection) -> None:
        try:
            message = connection.recv()
            kind = str(message.get("kind") or "")
            cameras: list[str] = []
            if kind == "stream.hello":
                verify_message(message, self.allowed_identities)  # the ONE PKI handshake
                cameras = [str(c) for c in (message.get("cameras") or [])]
                token = self.sessions.create("client", cameras)
                connection.send({"kind": "stream.welcome", "token": token,
                                 "epoch": self.epoch, "identity": "client"})
                self._pump(connection, token, cameras)
            elif kind == "stream.resume":
                token = str(message.get("token") or "")
                if not token or not self.sessions.valid(token):
                    connection.send({"kind": "stream.error", "error": "invalid or expired token"})
                    return
                cameras = self.sessions.cameras_for(token)
                connection.send({"kind": "stream.resumed", "epoch": self.epoch})
                self._pump(connection, token, cameras)
        except (PKIError, EOFError, BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass
            self._slots.release()

    def _pump(self, connection, token: str, cameras: list[str]) -> None:
        while not self._stop.is_set():
            sent_any = False
            for camera_id in cameras:
                try:
                    frame = self.frame_source(camera_id)
                except Exception:
                    frame = None
                if not frame or not frame.get("jpeg_base64"):
                    continue
                payload = {
                    "kind": "stream.frame",
                    "camera_id": camera_id,
                    "jpeg_base64": frame.get("jpeg_base64"),
                    "frame_id": frame.get("frame_id") or "",
                    "captured_at": frame.get("captured_at") or 0.0,
                }
                try:
                    connection.send(payload)
                    sent_any = True
                except (BrokenPipeError, ConnectionResetError, EOFError, OSError):
                    return
            if not sent_any:
                try:
                    connection.send({"kind": "stream.heartbeat", "epoch": self.epoch})
                except OSError:
                    return
            if self._stop.wait(self.push_interval):
                return


class StreamClient:
    """Client reader for the streaming channel.

    Establishes the ONE PKI handshake, remembers the server-issued token, and
    reconnects across port rotations presenting the token (no re-handshake).
    Callers run ``open_stream`` on a dedicated thread.
    """

    def __init__(self, *, identity: str = "client", push_interval: float = DEFAULT_PUSH_INTERVAL):
        self.identity = identity
        self.token: str | None = None

    def open_stream(self, host: str, port: int, cameras: list[str], callback) -> None:
        """Connect (PKI hello or token resume) and read frames until the socket
        closes. ``callback`` receives each ``stream.frame`` dict on this thread.
        """
        with Client((host, port), family="AF_INET", authkey=REMOTE_AUTHKEY) as connection:
            if self.token:
                connection.send({"kind": "stream.resume", "token": self.token, "cameras": cameras})
            else:
                connection.send(sign_message({"kind": "stream.hello", "cameras": cameras}, self.identity))
            while True:
                message = connection.recv()
                kind = str(message.get("kind") or "")
                if kind == "stream.welcome":
                    self.token = str(message.get("token") or self.token)
                elif kind == "stream.frame":
                    callback(message)
                elif kind == "stream.error":
                    # Token rejected (e.g. server restarted): drop it so the next
                    # connect falls back to a fresh PKI handshake.
                    self.token = None
                    return
