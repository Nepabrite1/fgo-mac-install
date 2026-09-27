"""Remote-client gateway: UDP discovery on 15195 + rotating TCP control port.

On the services host (Apple Intel / macOS), the ``core`` role owns a
:class:`NetworkGateway` that turns the machine-local services into a remotely
reachable control plane:

* It listens for control connections on a **randomly chosen TCP port**.
* It rebinds to a **new random port at a random interval**.
* It advertises the current ``(host, port)`` as a **PKI-signed UDP beacon** on
  port ``15195`` so any online client can (re)locate it.
* It verifies the connecting client's PKI identity, forwards the request to the
  target role's local Unix-socket service, and routes the signed reply back.

Clients on other machines use :class:`DiscoveryClient`, :func:`resolve_core` and
:class:`RemoteClient` to learn the current endpoint and speak the same PKI
envelope framing as the local IPC transport.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
import threading
import time
from dataclasses import dataclass

from multiprocessing.context import AuthenticationError
from multiprocessing.connection import Client, Listener

from .auth import issue_capability, load_key
from .ipc import IPCClient, ROLE_OFFSETS
from .pki import PKIError, PKI_IDENTITIES, sign_message, verify_message

DEFAULT_DISCOVERY_PORT = 15195
GATEWAY_IDENTITY = "core"
#: Unicast the beacon directly to known FGO clients as a reliable supplement to
#: broadcast, which can fail to leave the host interface on some networks.
CLIENT_UNICAST = os.environ.get("FGO_CLIENT_UNICAST", "10.0.0.109")
#: Shared helper for reading the streaming endpoint persisted by the server.
from .streamgate import read_stream_json as _read_stream_json  # noqa: E402
STREAM_SUPPLIER = lambda: _read_stream_json()  # noqa: E731
# Shared, non-empty connection authkey. The PKI-signed envelopes are the real
# authority; this only guards the transport framing and MUST match on both ends
# (an empty authkey makes multiprocessing fall back to a per-process default,
# which differs across machines).
REMOTE_AUTHKEY = b"FirstGeneralOrder-remote-v1"


@dataclass(frozen=True)
class Beacon:
    """Verified discovery advertisement of the current gateway endpoint."""

    host: str
    port: int
    role: str
    epoch: int
    timestamp: float
    identity: str
    # Realtime streaming endpoint (separate port; discovered alongside control).
    stream_port: int | None = None
    stream_epoch: int = 0


def _beacon_body(host: str, port: int, role: str, epoch: int) -> dict:
    return {
        "kind": "fgo.discovery.beacon",
        "host": host,
        "port": int(port),
        "role": role,
        "epoch": int(epoch),
        "timestamp": time.time(),
        "source": "FGO." + role.capitalize() + ".Gateway",
    }


class DiscoveryBroadcaster:
    """Broadcasts signed ``fgo.discovery.beacon`` frames on the discovery port.

    The beacon content is pulled from ``beacon_supplier`` on every send so the
    advertised port/epoch always reflect the gateway's *current* listener, even
    across rotations.
    """

    def __init__(
        self,
        beacon_supplier,
        *,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        role: str = GATEWAY_IDENTITY,
        interval_seconds: int = 3,
        stream_supplier=None,
    ):
        self._supplier = beacon_supplier
        self._stream_supplier = stream_supplier
        self.role = role
        self.discovery_port = discovery_port
        self.interval_seconds = max(1, int(interval_seconds))
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        threading.Thread(target=self._run, name="FGO-discovery-broadcast", daemon=True).start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            host, port, epoch = self._supplier()
            if port is None:
                continue
            body = _beacon_body(host, port, self.role, epoch)
            if self._stream_supplier is not None:
                try:
                    body.update(self._stream_supplier())
                except Exception:
                    pass
            beacon = sign_message(
                body,
                self.role,
            )
            payload = json.dumps(beacon, sort_keys=True, separators=(",", ":")).encode("utf-8")
            destinations = [
                ("<broadcast>", self.discovery_port),
                ("255.255.255.255", self.discovery_port),
                (_subnet_broadcast(), self.discovery_port),
                (
                    CLIENT_UNICAST if CLIENT_UNICAST else _subnet_broadcast(),
                    self.discovery_port,
                ),
                ("127.0.0.1", self.discovery_port),
            ]
            for address in destinations:
                try:
                    self._sock.sendto(payload, address)
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


class DiscoveryClient:
    """Listens for verified ``fgo.discovery.beacon`` frames on the discovery port."""

    def __init__(
        self,
        *,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        allowed_identities=(GATEWAY_IDENTITY,),
        bind_host: str = "0.0.0.0",
    ):
        self.discovery_port = discovery_port
        self.allowed_identities = tuple(allowed_identities)
        self.bind_host = bind_host
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: Beacon | None = None
        self._listener: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind_host, self.discovery_port))
        self._sock.settimeout(1.0)
        self._listener = threading.Thread(target=self._run, name="FGO-discovery-listen", daemon=True)
        self._listener.start()

    @property
    def latest(self) -> Beacon | None:
        with self._lock:
            return self._latest

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload, _address = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                message = json.loads(payload.decode("utf-8"))
                identity = verify_message(message, self.allowed_identities)
                body = dict(message)
                body.pop("pki", None)
                if body.get("kind") != "fgo.discovery.beacon":
                    continue
                beacon = Beacon(
                    host=str(body.get("host")),
                    port=int(body.get("port")),
                    role=str(body.get("role")),
                    epoch=int(body.get("epoch", 0)),
                    timestamp=float(body.get("timestamp", 0)),
                    identity=identity,
                    stream_port=int(body["stream_port"]) if body.get("stream_port") else None,
                    stream_epoch=int(body.get("stream_epoch", 0)),
                )
                with self._lock:
                    if self._latest is None or beacon.epoch > self._latest.epoch:
                        self._latest = beacon
            except (ValueError, KeyError, PKIError):
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


def resolve_stream(
    *,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
    timeout_seconds: float = 30.0,
    poll_interval: float = 1.0,
) -> tuple[str, int]:
    """Block until a verified beacon advertises a streaming port; return
    ``(host, stream_port)`` for the realtime media channel."""
    client = DiscoveryClient(discovery_port=discovery_port)
    client.start()
    try:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            latest = client.latest
            if latest is not None and latest.stream_port:
                return latest.host, latest.stream_port
            time.sleep(poll_interval)
        raise TimeoutError(f"no FGO stream port on discovery {discovery_port}")
    finally:
        client.stop()


def resolve_core(
    *,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
    timeout_seconds: float = 30.0,
    poll_interval: float = 1.0,
) -> tuple[str, int]:
    """Block until a valid core beacon is received; return ``(host, port)``."""
    client = DiscoveryClient(discovery_port=discovery_port)
    client.start()
    try:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            latest = client.latest
            if latest is not None:
                return latest.host, latest.port
            time.sleep(poll_interval)
        raise TimeoutError(f"no FGO core discovery beacon on port {discovery_port}")
    finally:
        client.stop()


class NetworkGateway:
    """Rotating TCP control listener that forwards to local Unix-socket services."""

    def __init__(
        self,
        *,
        services_host: str | None = None,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        bind_host: str = "0.0.0.0",
        rotate_min_seconds: int = 45,
        rotate_max_seconds: int = 120,
        broadcast_interval_seconds: int = 3,
        allowed_identities=("client",),
        stream_supplier=None,
    ):
        self.services_host = services_host or _default_lan_host()
        self.discovery_port = discovery_port
        self.bind_host = bind_host
        self.rotate_min = max(15, int(rotate_min_seconds))
        self.rotate_max = max(self.rotate_min, int(rotate_max_seconds))
        self.broadcast_interval = max(1, int(broadcast_interval_seconds))
        self.allowed_identities = tuple(allowed_identities)
        self.stream_supplier = stream_supplier
        self._stop = threading.Event()
        self._epoch = int(time.time() * 1000)
        self._listener: Listener | None = None
        self._current_port: int | None = None
        self._lock = threading.Lock()
        self._broadcaster: DiscoveryBroadcaster | None = None
        self._slots = threading.BoundedSemaphore(16)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._bind_new()
        self._rotator = threading.Thread(target=self._rotate_loop, name="FGO-gateway-rotate", daemon=True)
        self._rotator.start()
        self._broadcaster = DiscoveryBroadcaster(
            self._beacon,
            discovery_port=self.discovery_port, role=GATEWAY_IDENTITY,
            interval_seconds=self.broadcast_interval,
            stream_supplier=self.stream_supplier,
        )
        self._broadcaster.start()
        self._acceptor = threading.Thread(target=self._accept_loop, name="FGO-gateway-accept", daemon=True)
        self._acceptor.start()

    def _beacon(self) -> tuple[str, int, int]:
        with self._lock:
            return self.services_host, self._current_port, self._epoch

    def stop(self) -> None:
        self._stop.set()
        if self._broadcaster is not None:
            self._broadcaster.stop()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

    @property
    def port(self) -> int | None:
        with self._lock:
            return self._current_port

    # -- internals -----------------------------------------------------------
    def _bind_new(self) -> None:
        old = self._listener
        while True:
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            candidate.bind((self.bind_host, 0))
            port = candidate.getsockname()[1]
            candidate.close()
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
                target=self._handle, args=(connection,), name="FGO-gateway-handler", daemon=True
            ).start()

    def _handle(self, connection) -> None:
        try:
            # ``Listener.accept()`` (authkey=REMOTE_AUTHKEY) already performed the
            # transport handshake; the PKI-signed envelopes are the real authority.
            incoming = connection.recv()
            identity = verify_message(incoming, self.allowed_identities)
            target = str((incoming.get("payload") or {}).get("_remote_target") or "core").strip().lower()
            if target not in ROLE_OFFSETS:
                raise PKIError("invalid remote target role")
            payload = dict(incoming.get("payload") or {})
            remote_scopes = payload.pop("_remote_scopes", None)
            payload.pop("_remote_target", None)
            forwarded = dict(incoming)
            forwarded["payload"] = payload
            # The gateway is the trusted Mac-side intermediary. Issue a fresh
            # capability with the Mac's IPC key for the scopes the client
            # requested, so machine-local protected commands verify against the
            # Mac key without ever transferring it to the client.
            if remote_scopes:
                forwarded["authorization"] = issue_capability(
                    load_key(), subject=identity,
                    scopes=[str(s) for s in remote_scopes], lifetime_seconds=120,
                )
            else:
                forwarded["authorization"] = incoming.get("authorization", {})
            local = IPCClient(target, timeout_seconds=15.0, identity=GATEWAY_IDENTITY)
            reply = local.send(forwarded)
            connection.send(reply)
        except (PKIError, EOFError, BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass
            self._slots.release()


class RemoteClient:
    """Client side of the rotating control plane.

    Discovers the current gateway endpoint, connects over the advertised TCP
    port, and addresses a target service role via the gateway. Re-discoveries on
    rotation so a port change does not strand the client.
    """

    def __init__(
        self,
        *,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        identity: str = "client",
        allowed_response_identities=("core", "ai", "map", "distance", "media", "connect", "supervisor"),
        timeout_seconds: float = 10.0,
        discover_timeout_seconds: float = 30.0,
    ):
        self.discovery_port = discovery_port
        self.identity = identity
        self.allowed_response_identities = tuple(allowed_response_identities)
        self.timeout_seconds = timeout_seconds
        self.discover_timeout = discover_timeout_seconds
        self._endpoint: tuple[str, int] | None = None
        # Guards discovery (only one UDP binder at a time) and the cached endpoint
        # so the client is safe when several threads call send() concurrently
        # (e.g. a frame grabber + periodic state refresh). Each send still runs its
        # own TCP connection; only the shared discovery/endpoint state is serialized.
        self._lock = threading.RLock()

    def refresh_endpoint(self) -> tuple[str, int]:
        with self._lock:
            self._endpoint = resolve_core(
                discovery_port=self.discovery_port, timeout_seconds=self.discover_timeout
            )
            return self._endpoint

    def send(self, message: dict, *, target: str) -> dict:
        from multiprocessing.connection import Client

        message = dict(message)
        message.pop("pki", None)
        payload = dict(message.get("payload") or {})
        payload["_remote_target"] = str(target).strip().lower()
        message["payload"] = payload

        with self._lock:
            host_port = self._endpoint
            if host_port is None:
                host_port = self.refresh_endpoint()
        for attempt in (0, 1):
            try:
                encoded = sign_message(message, self.identity)
                box: dict = {}
                with Client(host_port, family="AF_INET", authkey=REMOTE_AUTHKEY) as connection:
                    connection.send(encoded)
                    worker = threading.Thread(
                        target=_timed_recv, args=(connection, box), name="FGO-remote-recv", daemon=True
                    )
                    worker.start()
                    worker.join(self.timeout_seconds)
                    if worker.is_alive():
                        raise TimeoutError("remote service response timed out")
                reply = box["response"]
                verify_message(reply, self.allowed_response_identities)
                return reply
            except (OSError, EOFError, ConnectionError, TimeoutError):
                if attempt == 0:
                    with self._lock:
                        refreshed = self.refresh_endpoint()
                    if refreshed != host_port:
                        host_port = refreshed
                        continue
                raise


def _timed_recv(connection, box: dict) -> None:
    box["response"] = connection.recv()


def _default_lan_host() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # does not send traffic; selects a route
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _subnet_broadcast() -> str:
    """Directed broadcast for the local /24, which macOS sends out the LAN
    interface reliably (the limited 255.255.255.255 can stay on loopback)."""
    try:
        addr = ipaddress.ip_address(_default_lan_host())
        net = ipaddress.ip_network(str(addr) + "/24", strict=False)
        return str(net.broadcast_address)
    except ValueError:
        return "255.255.255.255"
