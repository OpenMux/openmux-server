#!/usr/bin/env python3
"""Run a single Port Action against a configured port (Phase 1, CLI-only).

See docs/design/port_actions.md ("Rollout phases", phase 1). Boots the full
adapter set from the given server config file (same adapters the real server
would start), runs one action script against a named port, prints the
resulting run status, then stops the adapters again. There is no web UI
trigger yet for this feature; this script is the phase-1 entry point.

Usage:
  .venv/bin/python scripts/run_action.py \\
      -c config/loopback_test.yaml \\
      --port <port_name> \\
      --script openmux/server/actions/examples/echo_probe.py \\
      --param text=hello
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from openmux.server.actions.errors import ActionError  # noqa: E402
from openmux.server.actions.registry import load_action_from_file  # noqa: E402
from openmux.server.actions.runner import ActionRunner  # noqa: E402
from openmux.server.data_logger import DataLogger  # noqa: E402
from openmux.server.main import OpenMuxServer  # noqa: E402


def _parse_params(raw: List[str]) -> Dict[str, str]:
    params = {}
    for item in raw or []:
        if "=" not in item:
            raise SystemExit(f"Invalid --param {item!r}; expected name=value")
        name, value = item.split("=", 1)
        params[name] = value
    return params


async def _run(args: argparse.Namespace) -> int:
    server = OpenMuxServer(
        args.config,
        auth_config_path=args.auth_config,
        security_config_path=args.security_config,
    )
    await server._initialize_unified_adapters()
    try:
        action = load_action_from_file(args.script)
        runner = ActionRunner(server.port_manager)
        params = _parse_params(args.param)
        run = await runner.start_run(action, args.port, params, username=args.username)
        print(
            json.dumps(
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "error": run.error,
                    "log_path": str(DataLogger.get().get_log_path(run.log_port_name)),
                },
                indent=2,
            )
        )
        return 0 if run.status == "success" else 1
    except ActionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        for adapter in list(getattr(server, "unified_adapters", [])):
            try:
                await adapter.stop()
            except Exception:
                # justification: best-effort shutdown after a one-shot CLI run; not user-actionable
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", required=True, help="Path to the server config YAML")
    parser.add_argument("--auth-config", default=None, help="Path to authentication.yaml (defaults per config dir)")
    parser.add_argument("--security-config", default=None, help="Path to security.yaml (defaults per config dir)")
    parser.add_argument("--port", required=True, help="Target port name")
    parser.add_argument("--script", required=True, help="Path to the action script file")
    parser.add_argument("--param", action="append", default=[], help="name=value (repeatable)")
    parser.add_argument("--username", default="cli", help="Identity to attribute the run to")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
