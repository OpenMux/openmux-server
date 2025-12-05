#!/usr/bin/env python3
"""
OpenMux end-to-end smoke workflow

- Starts the OpenMux server in a background subprocess with a dedicated config
- Uses TcpClientAdapter to connect, auth, list ports, and exercise:
  * loopback: send/receive roundtrip
  * command (/bin/cat): echo roundtrip
  * serial (optional): only if /tmp/vserial1 exists; does simple echo via peer

Exit codes:
  0 on success, non-zero on failure. Prints concise diagnostics.

Usage:
  python3 scripts/smoke_workflow.py [--server-config config/integration_test.yaml]
  Optional: run setup_virtual_serial.sh in another terminal to enable serial test.
"""
import argparse
import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

# Allow running from repo root
THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from openmux.client.adapters.tcp_adapter import TcpClientAdapter  # noqa: E402


def _log(msg: str) -> None:
    print(msg, flush=True)


def _which_python() -> str:
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


async def wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    import socket
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except Exception:
            # transient connection attempt failure
            await asyncio.sleep(0.1)
    return False


async def run_loopback_case() -> None:
    _log("[loopback] starting")
    adapter = TcpClientAdapter("127.0.0.1", 8123)
    if not await adapter.connect() or not await adapter.authenticate_with_password("admin", "password"):
        raise RuntimeError("Loopback session connect/auth failed")
    ports = await adapter.list_ports()
    if "loop1" not in ports:
        raise RuntimeError(f"loop1 not in server ports: {ports}")
    if not await adapter.connect_to_port("loop1"):
        raise RuntimeError("Failed to connect to loop1")
    payload = b"hello-loop\n"
    if not await adapter.send_data(payload):
        raise RuntimeError("Failed to send to loop1")
    buf = await read_until_contains(adapter, payload.strip(), timeout=2.0)
    if payload.strip() not in buf:
        raise RuntimeError(f"Unexpected loopback response: {buf!r}")
    await adapter.close()
    _log("[loopback] ok")


async def run_command_case() -> None:
    _log("[command] starting")
    adapter = TcpClientAdapter("127.0.0.1", 8123)
    if not await adapter.connect() or not await adapter.authenticate_with_password("admin", "password"):
        raise RuntimeError("Command session connect/auth failed")
    ports = await adapter.list_ports()
    if "cat" not in ports:
        raise RuntimeError(f"cat not in server ports: {ports}")
    if not await adapter.connect_to_port("cat"):
        raise RuntimeError("Failed to connect to cat")
    payload = b"hello-cat\n"
    if not await adapter.send_data(payload):
        raise RuntimeError("Failed to send to cat")
    buf = await read_until_contains(adapter, b"hello-cat", timeout=2.0)
    if b"hello-cat" not in buf:
        raise RuntimeError(f"Unexpected command response: {buf!r}")
    await adapter.close()
    _log("[command] ok")


async def run_serial_case() -> None:
    # Only run if device exists
    if not Path("/tmp/vserial1").exists():
        _log("[serial] skipping (no /tmp/vserial1)")
        return
    _log("[serial] starting")
    adapter = TcpClientAdapter("127.0.0.1", 8123)
    if not await adapter.connect() or not await adapter.authenticate_with_password("admin", "password"):
        raise RuntimeError("Serial session connect/auth failed")
    ports = await adapter.list_ports()
    if "vserial1" not in ports:
        raise RuntimeError(f"vserial1 not in server ports: {ports}")
    if not await adapter.connect_to_port("vserial1"):
        raise RuntimeError("Failed to connect to vserial1")
    payload = b"hello-serial\n"
    if not await adapter.send_data(payload):
        raise RuntimeError("Failed to send to serial")
    buf = await read_until_contains(adapter, b"hello-serial", timeout=2.0)
    if b"hello-serial" not in buf:
        raise RuntimeError(f"Unexpected serial response (ensure peer side echoes): {buf!r}")
    await adapter.close()
    _log("[serial] ok")


async def read_until_contains(adapter: TcpClientAdapter, needle: bytes, timeout: float = 2.0) -> bytes:
    import time

    end = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < end:
        chunk = await adapter.read_data(timeout=0.2)
        if isinstance(chunk, (bytes, bytearray)) and chunk:
            buf += chunk
            if needle in buf:
                break
    return bytes(buf)


async def main_async(args) -> int:
    # Start server process
    python = _which_python()
    cfg = args.server_config
    env = os.environ.copy()
    cmd = [python, "-m", "openmux.server.main", "-c", cfg]
    _log(f"[server] starting: {cmd}")
    server = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        # Wait until port is open
        if not await wait_for_port("127.0.0.1", 8123, timeout=8.0):
            _log("[server] did not open port 8123 within 8s")
            # Try to terminate and then drain any available output safely
            try:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        server.kill()
                if server.stdout:
                    try:
                        out = server.stdout.read()  # drain remaining buffered output
                    except Exception:
                        out = ""
                else:
                    out = ""
                if out:
                    print("[server] output:\n" + out)
            except Exception as e:
                print(f"[server] error draining output after failure: {e}", flush=True)
            return 2
        _log("[server] ready on 127.0.0.1:8123")

        # Exercise cases (fresh session per case to avoid character mode state)
        await run_loopback_case()
        await run_command_case()
        try:
            await run_serial_case()
        except Exception as e:
            # Make serial optional: warn but do not fail the whole smoke run
            print(f"[WARN] Serial case skipped/failed: {e}", flush=True)
        _log("[smoke] SUCCESS: loopback + command (serial optional)")
        return 0

    except Exception as e:
        print(f"[ERROR] Smoke test failed: {e}", flush=True)
        return 1
    finally:
        try:
            server.send_signal(signal.SIGINT)
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        except Exception as e:
            print(f"[server] error during shutdown: {e}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--server-config",
        default=str(REPO_ROOT / "config" / "integration_test.yaml"),
        help="Path to server YAML config",
    )
    args = parser.parse_args()
    rc = asyncio.run(main_async(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
