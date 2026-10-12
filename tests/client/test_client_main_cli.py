"""Tests for the OpenMux client CLI entry point (``openmux.client.main.main``).

``main()`` was never covered before this file (all 128 body lines missing).
These tests pin every dispatch branch on the OLD code so the bracket-C901
split of ``main()`` is provably behavior-preserving:

- by-name connect (username vs websocket ws_user/ws_password selection)
- ``--list`` (resolved vs no-server SystemExit(1); the list task runs real
  with an async fake client: config-credential fallback, config pubkey
  fallback, the ``if connection:`` True and False paths)
- batch ``port_name command`` (no-server SystemExit(1), websocket
  SystemExit(2), tcp run)
- port-only console (no-server SystemExit(1), websocket closure with a
  connect that returns None, tcp run)
- server console (no-server SystemExit(1), websocket discovery closure
  running real: empty-port-list path and full path incl. auto-selecting the
  first port and the second connect, tcp run)
- the KeyboardInterrupt -> client.shutdown() path
- the verbose level selection (-v/-vv/default) and the logging-config merge
  into a dict ``client.config``

Mechanics: ``sys.argv``, ``OpenMuxClient``, ``ClientLoggingManager`` are
monkeypatched in the module namespace. Where a branch must NOT spin up a
real event loop, ``asyncio.run`` is patched to record + close the coroutine;
where the coroutine body itself is under test (list task, websocket
console), the real ``asyncio.run`` is used and the fake client provides
plain ``async def`` methods. A synchronous ``find_and_connect_by_name`` that
raises drives the KeyboardInterrupt path (an async stub would only create a
coroutine and never raise at call time).
"""

import logging
import sys

import pytest

import openmux.client.main as cli_main


class _FakeConn:
    def __init__(self, name="c", is_connected=False, is_authenticated=False):
        self.name = name
        self.is_connected = is_connected
        self.is_authenticated = is_authenticated
        self.closed = 0

    async def close(self):
        self.closed += 1


class _FakeClient:
    def __init__(
        self,
        config=None,
        resolved=True,
        connect_rets=(),
        list_ports_ret=None,
        creds=("u", "p", "k"),
        pubkey=("pk", "pkid"),
    ):
        self.config = config
        self.logging_manager = None
        self._resolved = ("resolved-host", 9999) if resolved else None
        self._connect_rets = list(connect_rets)
        self._list_ports_ret = list_ports_ret
        self._creds = creds
        self._pubkey = pubkey
        self.find_args = None
        self.list_calls = []
        self.shutdown_called = False

    def resolve_default_server(self, server, port):
        return (server, 8023) if server else self._resolved

    def get_credentials_for(self, host):
        return self._creds

    def get_pubkey_for(self, host):
        return self._pubkey

    def find_and_connect_by_name(self, *args):
        # Record at CALL time (the coroutine body only runs when awaited,
        # and the recorded-then-closed fake asyncio.run never awaits it).
        self.find_args = args

        async def _run():
            return None

        return _run()

    async def list_ports(self, connection):
        self.list_calls.append(connection)
        return self._list_ports_ret

    async def connect_to_server(self, *args, **kwargs):
        assert self._connect_rets, "connect_to_server called with no queued fake connection"
        return self._connect_rets.pop(0)

    async def run_batch_command(self, *args):
        return None

    async def run_console_on_port(self, *args):
        return None

    async def run_console(self, *args):
        return None

    async def shutdown(self):
        self.shutdown_called = True


class _FakeLogMgr:
    def __init__(self, cfg):
        self.cfg = cfg


def _main(monkeypatch, argv, client, run_real=False):
    """Drive main() with a fixed argv and fake client; return the asyncio.run
    coroutines recorded when ``run_real`` is False."""
    monkeypatch.setattr(sys, "argv", ["openmux_client"] + argv)
    monkeypatch.setattr(cli_main, "OpenMuxClient", lambda cfg: client)
    monkeypatch.setattr(cli_main, "ClientLoggingManager", _FakeLogMgr)
    record = []

    def fake_run(coro, *a, **k):
        record.append(coro)
        coro.close()

    import asyncio as _asyncio

    def real_run(coro, *a, **k):
        # Drive the coroutine on a throwaway loop. The real asyncio.run() calls
        # set_event_loop(None) on exit, which latches the thread's event-loop
        # policy so that later tests constructing asyncio.Event() outside a
        # running loop (muxcon adapters) raise RuntimeError. An independent
        # new/close loop never touches that policy.
        loop = _asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(cli_main.asyncio, "run", real_run if run_real else fake_run)
    cli_main.main()
    return record


def test_main_name_branch_uses_username(monkeypatch):
    c = _FakeClient(config={})
    record = _main(monkeypatch, ["-n", "C", "-s", "h", "-u", "cli-u", "-w", "cli-w", "-k", "cli-k"], c)
    assert len(record) == 1
    args = c.find_args
    # name, explicit credentials (not ws_user on tcp), then the [0:2] pubkey pair
    assert args[0] == "C" and args[1] == "cli-u" and args[2] == "cli-w" and args[3] == "cli-k"
    assert args[4] == "pk" and args[5] == "pkid"


def test_main_name_branch_websocket_ws_user(monkeypatch):
    c = _FakeClient(config={})
    record = _main(monkeypatch, ["-n", "C", "-s", "h", "--adapter", "websocket", "--ws-user", "wu", "--ws-password", "wp"], c)
    assert len(record) == 1
    args = c.find_args
    # websocket + ws creds provided -> they override -u/-w
    assert args[0] == "C" and args[1] == "wu" and args[2] == "wp"


def test_main_name_branch_explicit_pubkey_wins(monkeypatch):
    c = _FakeClient(config={})
    _main(monkeypatch, ["-n", "C", "-s", "h", "--pubkey", "k.pem", "--pubkey-id", "pid"], c)
    args = c.find_args
    # explicit --pubkey/--pubkey-id skip the config fallback tuple
    assert args[4] == "k.pem" and args[5] == "pid"


def test_main_config_not_dict_skips_logging_merge(monkeypatch):
    c = _FakeClient(config=None)
    _main(monkeypatch, ["-s", "h"], c)
    # client.config is not a dict -> no logging_manager, config untouched
    assert c.logging_manager is None and c.config is None


def test_main_list_resolved_runs_task(monkeypatch):
    conn = _FakeConn("conn1")
    c = _FakeClient(config=None, connect_rets=[conn], list_ports_ret=[{"name": "p1"}])
    _main(monkeypatch, ["-l", "-s", "h"], c, run_real=True)
    # no -u/-w/-k -> config credentials fell in; no --pubkey -> config pubkey fell in
    assert c.list_calls == [conn]
    assert conn.closed == 1


def test_main_list_resolved_explicit_credentials(monkeypatch):
    conn = _FakeConn("conn2")
    c = _FakeClient(config=None, connect_rets=[conn])
    _main(monkeypatch, ["-l", "-s", "h", "-u", "cli-u"], c, run_real=True)
    # CLI username present -> the all-None credential fallback block is skipped
    assert c.list_calls == [conn]


def test_main_list_resolved_no_connection(monkeypatch):
    c = _FakeClient(config=None, connect_rets=[None])
    # connect returned None -> `if connection:` False; no list/close, no crash
    _main(monkeypatch, ["-l", "-s", "h"], c, run_real=True)
    assert c.list_calls == []


def test_main_list_no_server_exits(monkeypatch, caplog):
    c = _FakeClient(config=None, resolved=False)
    # conftest pins root logging to CRITICAL; drop it for the call so the
    # logging.error(...) notice reaches caplog
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as ei:
        _main(monkeypatch, ["-l"], c)
    assert ei.value.code == 1
    # logging.error is captured by pytest's logging plugin, not stderr
    assert any("No server specified" in r.getMessage() for r in caplog.records)


def test_main_batch_no_server_exits(monkeypatch):
    c = _FakeClient(config=None, resolved=False)
    with pytest.raises(SystemExit) as ei:
        _main(monkeypatch, ["P", "CMD"], c)
    assert ei.value.code == 1


def test_main_batch_websocket_exits(monkeypatch, caplog):
    c = _FakeClient(config=None)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as ei:
        _main(monkeypatch, ["P", "CMD", "-s", "h", "--adapter", "websocket"], c)
    assert ei.value.code == 2
    assert any("not yet supported" in r.getMessage() for r in caplog.records)


def test_main_batch_tcp_runs(monkeypatch):
    c = _FakeClient(config=None)
    record = _main(monkeypatch, ["P", "CMD", "-s", "h"], c)
    assert len(record) == 1 and record[0].__name__ == "run_batch_command"


def test_main_port_no_server_exits(monkeypatch):
    c = _FakeClient(config=None, resolved=False)
    with pytest.raises(SystemExit) as ei:
        _main(monkeypatch, ["P"], c)
    assert ei.value.code == 1


def test_main_port_tcp_runs(monkeypatch):
    c = _FakeClient(config=None)
    record = _main(monkeypatch, ["P", "-s", "h"], c)
    assert len(record) == 1 and record[0].__name__ == "run_console_on_port"


def test_main_port_websocket_no_connection(monkeypatch):
    c = _FakeClient(config=None, connect_rets=[None])
    # ws console closure: connect returned None -> `if not conn: return`
    _main(monkeypatch, ["P", "-s", "h", "--adapter", "websocket"], c, run_real=True)
    assert c.list_calls == []


def test_main_port_websocket_real_console_run(monkeypatch):
    conn = _FakeConn("port", is_connected=False, is_authenticated=False)
    c = _FakeClient(config=None, connect_rets=[conn])
    # connect succeeded -> a REAL ConsoleUI is built from the conn and .run()
    # is awaited; not-connected -> run() logs + returns False cleanly
    _main(monkeypatch, ["P", "-s", "h", "--adapter", "websocket"], c, run_real=True)
    assert c.list_calls == []


def test_main_console_websocket_discovery_no_connection(monkeypatch):
    c = _FakeClient(config=None, connect_rets=[None])
    # discovery connect returned None -> `if not conn: return` (before list_ports)
    _main(monkeypatch, ["-s", "h", "--adapter", "websocket"], c, run_real=True)
    assert c.list_calls == []


def test_main_console_no_server_exits(monkeypatch, caplog):
    c = _FakeClient(config=None, resolved=False)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as ei:
        _main(monkeypatch, [], c)
    assert ei.value.code == 1
    assert any("or console name" in r.getMessage() for r in caplog.records)


def test_main_console_tcp_runs(monkeypatch):
    c = _FakeClient(config=None)
    record = _main(monkeypatch, ["-s", "h"], c)
    assert len(record) == 1 and record[0].__name__ == "run_console"


def test_main_console_websocket_success_flow(monkeypatch):
    conn1 = _FakeConn("disc", is_connected=False)
    conn2 = _FakeConn("port", is_connected=False)
    c = _FakeClient(config=None, connect_rets=[conn1, conn2], list_ports_ret=[{"name": "auto1"}])
    # discovery connect -> one port -> close disc -> second connect (port) ->
    # real ConsoleUI on a not-connected conn -> run() returns False
    _main(monkeypatch, ["-s", "h", "--adapter", "websocket"], c, run_real=True)
    assert c.list_calls == [conn1]
    assert conn1.closed == 1
    assert c._connect_rets == []


def test_main_console_websocket_empty_ports(monkeypatch, caplog):
    conn1 = _FakeConn("disc")
    c = _FakeClient(config=None, connect_rets=[conn1], list_ports_ret=[])
    with caplog.at_level(logging.ERROR):
        _main(monkeypatch, ["-s", "h", "--adapter", "websocket"], c, run_real=True)
    # `if not ports:` -> error + close + return (no second connect)
    assert conn1.closed == 1
    assert any("No ports available" in r.getMessage() for r in caplog.records)


def test_main_console_websocket_missing_second_connection(monkeypatch):
    conn1 = _FakeConn("disc")
    c = _FakeClient(config=None, connect_rets=[conn1, None], list_ports_ret=[{"name": "p"}])
    # second connect -> None -> `if not conn2: return`
    _main(monkeypatch, ["-s", "h", "--adapter", "websocket"], c, run_real=True)
    assert conn1.closed == 1


def test_main_keyboard_interrupt_shuts_down(monkeypatch):
    c = _FakeClient(config=None)

    def ki(*a):
        raise KeyboardInterrupt()

    c.find_and_connect_by_name = ki  # sync so it raises at call time in main()
    record = _main(monkeypatch, ["-n", "C", "-s", "h"], c)
    assert len(record) == 1 and record[0].__name__ == "shutdown"


def test_main_log_level_debug(monkeypatch):
    c = _FakeClient(config={})
    _main(monkeypatch, ["-vv", "-s", "h"], c)
    assert c.logging_manager.cfg["log_level"] == "DEBUG"
    assert c.config["logging"]["log_level"] == "DEBUG"


def test_main_log_level_info(monkeypatch):
    c = _FakeClient(config={})
    _main(monkeypatch, ["-v", "-s", "h"], c)
    assert c.logging_manager.cfg["log_level"] == "INFO"


def test_main_log_level_default_warning_and_defaults(monkeypatch):
    c = _FakeClient(config={})
    _main(monkeypatch, ["-s", "h"], c)
    cfg = c.logging_manager.cfg
    assert cfg["log_level"] == "WARNING"
    # defaults: no --log-to-file/-q/--log-dir -> file_only False, logging enabled False
    assert cfg["file_only"] is False and cfg["file_logging_enabled"] is False
    assert cfg["log_dir"] == "logs" and cfg["log_file"] == "openmux_client.log"
    assert cfg["log_max_size_mb"] == 10 and cfg["log_backups"] == 5


def test_main_log_config_file_only(monkeypatch):
    c = _FakeClient(config={})
    _main(monkeypatch, ["-s", "h", "--log-to-file"], c)
    # --log-to-file without -q/--log-dir/--log-file -> file_only True, enabled True
    assert c.logging_manager.cfg["file_only"] is True
    assert c.logging_manager.cfg["file_logging_enabled"] is True
