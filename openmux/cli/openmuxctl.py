#!/usr/bin/env python3
"""
openmuxctl - Local control tool for OpenMux via Unix domain socket.

This module is installed as a console script when the package is installed.

Usage examples:
  openmuxctl status
  openmuxctl reload --soft
  openmuxctl reload --full
  openmuxctl --socket /path/to/openmux.sock status

Socket resolution precedence:
  1) --socket path
  2) env OPENMUX_CTL_SOCK
  3) logs/openmux.sock (default)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Optional, List


async def send_command(sock_path: str, payload: dict) -> int:
    try:
        reader, writer = await asyncio.open_unix_connection(sock_path)
    except FileNotFoundError:
        print(f"Control socket not found: {sock_path}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Failed to connect to control socket {sock_path}: {e}", file=sys.stderr)
        return 2

    try:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not line:
            print("No response from server", file=sys.stderr)
            return 2
        resp = json.loads(line.decode("utf-8"))
        if not isinstance(resp, dict) or not resp.get("ok"):
            print(json.dumps(resp, indent=2, sort_keys=True))
            return 1
        # Pretty print result
        print(json.dumps(resp.get("result", {}), indent=2, sort_keys=True))
        return 0
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def resolve_socket_path(cli_sock: Optional[str]) -> str:
    if cli_sock:
        return cli_sock
    env = os.environ.get("OPENMUX_CTL_SOCK")
    if env:
        return env
    return os.path.join("logs", "openmux.sock")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(description="OpenMux local control (Unix domain socket)")
    p.add_argument("command", choices=["status", "reload"], help="Command to execute")
    p.add_argument("--socket", dest="socket", help="Path to the OpenMux control socket")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--soft", action="store_true", help="Soft reload (auth+ports)")
    g.add_argument("--full", action="store_true", help="Full reload (stop/recreate/start)")

    args = p.parse_args(argv)

    sock_path = resolve_socket_path(args.socket)

    if args.command == "status":
        payload = {"action": "status"}
    elif args.command == "reload":
        scope = "full" if args.full else "soft"
        payload = {"action": "reload", "scope": scope}
    else:
        p.error("Unknown command")
        return 2

    return asyncio.run(send_command(sock_path, payload))


if __name__ == "__main__":
    raise SystemExit(main())
