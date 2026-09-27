"""Console/service-child entry point used by the Windows service wrapper."""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import signal
import sys
import threading

from .contracts import request


def _acquire_instance_guard(role: str):
    """Return a process-held Windows mutex, or None when this role already runs."""
    if os.name != "nt":
        return True
    namespace = str(os.environ.get("FGO_IPC_TEST_PORT_BASE") or os.environ.get("FGO_IPC_PIPE_NAMESPACE") or "production")
    namespace = re.sub(r"[^A-Za-z0-9_.-]", "-", namespace)[:64]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, f"Local\\FirstGeneralOrder.{namespace}.{role}.v2")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="FirstGeneralOrderRuntime")
    parser.add_argument("--role", choices=("core", "ai", "map", "distance", "media", "connect", "supervisor"))
    parser.add_argument("--gateway", action="store_true",
                        help="run the remote client gateway (discovery on 15195 + rotating TCP port)")
    parser.add_argument("--health-smoke", action="store_true")
    parser.add_argument("--windows-service", action="store_true")
    return parser


def _run_gateway(options) -> int:
    from .netgate import DEFAULT_DISCOVERY_PORT, STREAM_SUPPLIER, NetworkGateway

    stop_once = threading.Event()

    def _stop(_signum, _frame):
        stop_once.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    gateway = NetworkGateway(
        discovery_port=int(os.environ.get("FGO_DISCOVERY_PORT", DEFAULT_DISCOVERY_PORT)),
        rotate_min_seconds=int(os.environ.get("FGO_GATEWAY_ROTATE_MIN", "45")),
        rotate_max_seconds=int(os.environ.get("FGO_GATEWAY_ROTATE_MAX", "120")),
        stream_supplier=STREAM_SUPPLIER,
    )
    gateway.start()
    try:
        stop_once.wait()
    finally:
        gateway.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    if options.gateway:
        return _run_gateway(options)
    if options.windows_service:
        from .services import create_service
        from .windows_service import run_windows_service
        return run_windows_service(options.role, create_service)
    guard = _acquire_instance_guard(options.role)
    if guard is None:
        return 3
    try:
        # The per-role guard must be acquired before importing every service
        # implementation. A duplicate process then exits immediately instead of
        # spending seconds loading AI/Qt/native dependencies before it is rejected.
        from .services import create_service
        service = create_service(options.role)
        if options.health_smoke:
            result = service.dispatch(request("health", source="FGO.BuildSmoke"))
            service.store.close()
            return 0 if result.get("payload", {}).get("ok") else 2
        stop_once = threading.Event()

        def stop_handler(_signum, _frame):
            if not stop_once.is_set():
                stop_once.set()
                service.stop()

        signal.signal(signal.SIGINT, stop_handler)
        signal.signal(signal.SIGTERM, stop_handler)
        service.run()
        return 0
    finally:
        if os.name == "nt" and guard not in (None, True):
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(guard)


if __name__ == "__main__":
    raise SystemExit(main())
