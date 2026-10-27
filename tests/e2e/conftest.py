"""Fixtures for the end-to-end tier: one server, one browser, one story, and a failure bundle.

Command-line options live in ``tests/conftest.py``, because pytest parses options before it loads a
conftest this deep. Everything that drives a test lives here.

A passing test leaves nothing behind. A failing test keeps a bundle under ``artifacts/e2e/`` with the
merged timeline, the server output, the port data logs, the config it used, and the browser traces.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import pytest

from .console import Browser, ConsoleSession, browser_available, login
from .harness import TEST_PASSWORD, Api, ConfigBuilder, E2EServer, ServerAddress, free_ports
from .story import Story, Timeline, artifact_dir_for, environment_flags
from .textclients import CliSession, TelnetSession, open_cli_client, open_telnet_client

#: Where a failure bundle is written, relative to the repository root.
ARTIFACTS_RELATIVE = Path("artifacts") / "e2e"

#: The users every default world gets. Names say what a handle may do, per the readability rules.
DEFAULT_USERS = (("admin", "admin"), ("rw", "read-write"), ("ro", "read-only"))

REPO_ROOT = Path(__file__).resolve().parents[2]


def repo_root() -> Path:
    """The repository root, from this file's location."""
    return REPO_ROOT


# --- options (registered in tests/conftest.py, read here) ------------------------------------


def _options(config: Any) -> Dict[str, Any]:
    """The e2e knobs: command-line options merged with their environment equivalents."""
    env = environment_flags()
    live = bool(config.getoption("--e2e-live")) or env["live"]
    reveal = bool(config.getoption("--e2e-reveal")) or env["reveal"]
    return {
        "live": live,
        "reveal": reveal,
        "headed": live or bool(config.getoption("--e2e-headed")) or env["live"],
        "slow_mo": int(config.getoption("--e2e-slowmo") or 0),
        # A revealed failure is useless if the bundle is deleted next second, so reveal implies keep.
        "keep": bool(config.getoption("--e2e-keep")) or reveal,
        "_revealed": False,
        "artifacts": Path(config.getoption("--e2e-artifacts") or (REPO_ROOT / ARTIFACTS_RELATIVE)),
    }


@pytest.fixture(scope="session")
def e2e_options(request: pytest.FixtureRequest) -> Dict[str, Any]:
    """The resolved options for the whole session."""
    return _options(request.config)


@pytest.fixture(scope="session")
def artifacts_root(e2e_options: Dict[str, Any]) -> Path:
    """The directory that holds failure bundles."""
    return Path(e2e_options["artifacts"])


# --- failure capture -------------------------------------------------------------------------


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: Any):
    """Remember that this test failed, so fixture teardown can keep the artifacts.

    The timeline gets the pointer line, because it is the file a reader opens first. A step already
    wrote the precise line when the assertion sat inside one, so the body phase adds nothing there. A
    setup or teardown failure has no step, and there the traceback is the only evidence.
    """
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        setattr(item, f"_e2e_failed_{report.when}", True)
        story = getattr(item, "_e2e_story", None) or getattr(item, "funcargs", {}).get("story")
        if story is not None:
            timeline = story.timeline
            if report.when != "call" or not timeline.recorded_failure:
                timeline.failure(f"{report.when} failed", _short_repr(report))


def _short_repr(report: Any) -> str:
    """The crash line of a report (``E AssertionError: ...``), else its whole text, as one line."""
    crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
    message = str(getattr(crash, "message", "") or "") if crash is not None else ""
    text = " ".join(message.split()) if message else " ".join(str(getattr(report, "longrepr", "")).split())
    return text[-900:] if text else "no assertion text reported"


def _failed(request: pytest.FixtureRequest) -> bool:
    """True when the current test failed in setup, the body, or teardown so far."""
    node = request.node
    return any(getattr(node, f"_e2e_failed_{phase}", False) for phase in ("setup", "call", "teardown"))


def _test_id(nodeid: str) -> str:
    """A readable directory name for one test id."""
    tail = nodeid.split("::")[-1]
    return tail


def _reveal(options: Dict[str, Any], lines: Sequence[str]) -> None:
    """Hold a failing test open so a person can look at the live page and server.

    Runs once per test, in the first teardown reached, which is the page registry: pytest finalizes
    fixtures in reverse order of creation, so pages close while the server still runs. Without a
    terminal (``pytest -s`` off, or CI) it prints and returns, so a batch run never blocks.
    """
    if not options.get("reveal") or options.get("_revealed"):
        return
    options["_revealed"] = True
    print("\n--e2e-reveal: this test failed and is being held open", file=sys.stderr)
    for line in lines:
        print(f"  {line}", file=sys.stderr)
    if sys.stdin is None or not sys.stdin.isatty():
        print("  no terminal attached (run with -s), so nothing is held open", file=sys.stderr)
        return
    try:
        input("  press Enter to close the pages and stop the server...")
    except EOFError:
        pass  # justification: a closed stdin means nobody can answer, so reveal must not block forever


# --- story and timeline ----------------------------------------------------------------------


@pytest.fixture
def timeline(request: pytest.FixtureRequest, e2e_options: Dict[str, Any], artifacts_root: Path) -> Timeline:
    """The merged timeline for this test, written into its artifact directory."""
    path = artifact_dir_for(artifacts_root, request.node.nodeid) / "timeline.log"
    return Timeline(path=path, echo=e2e_options["live"])


@pytest.fixture
def story(request: pytest.FixtureRequest, timeline: Timeline, e2e_options: Dict[str, Any]) -> Story:
    """The narrative handle: numbered steps, the cast list, and the timeline behind them.

    On a pass, the artifact directory is removed, so a green run leaves the tree clean. On a
    failure, it stays and the timeline inside it is the first file to read.
    """
    title = (request.node.function.__doc__ or "").strip().splitlines()
    instance = Story(timeline, test_id=_test_id(request.node.nodeid), title=title[0] if title else request.node.name)
    instance.begin()
    # Reachable from the report hook even when this test's setup fails and pytest drops funcargs.
    request.node._e2e_story = instance
    try:
        yield instance
    finally:
        instance.end()
        keep = _failed(request) or e2e_options["keep"]
        bundle = timeline.path.parent if timeline.path else None
        if bundle is not None and not keep:
            shutil.rmtree(bundle, ignore_errors=True)
            _remove_if_empty(bundle.parent)


@pytest.fixture(autouse=True)
def _story_first(story: Story) -> Story:
    """Build the story before any other fixture of this test runs.

    pytest asks an autouse fixture before the ones a test names, so a failure in some other fixture's
    setup still has a timeline to write into and a bundle directory to keep.
    """
    return story


def _remove_if_empty(path: Optional[Path]) -> None:
    """Delete a directory only when it is empty, so a sibling test's artifacts survive."""
    if path is None or not path.exists():
        return
    try:
        next(path.iterdir())
    except StopIteration:
        path.rmdir()
    except OSError:
        pass  # justification: a leftover artifact directory is never a test failure


# --- servers ---------------------------------------------------------------------------------


class ServerRegistry:
    """Every server one test started, so teardown can stop them all and keep their artifacts."""

    def __init__(
        self, request: pytest.FixtureRequest, tmp_path: Path, e2e_options: Dict[str, Any], artifacts_root: Path, story: Story
    ):
        self.request = request
        self.tmp_path = tmp_path
        self.options = e2e_options
        self.artifacts_root = artifacts_root
        self.story = story
        self.bundle = artifact_dir_for(artifacts_root, request.node.nodeid)
        self.servers: List[E2EServer] = []

    # --- factory ---------------------------------------------------------------

    def __call__(self, **kwargs: Any) -> E2EServer:
        """Shorthand for ``make``, so a test body reads ``servers(start=True)``."""
        return self.make(**kwargs)

    def make(
        self,
        *,
        name: str = "e2e",
        users: Sequence[Sequence[str]] = DEFAULT_USERS,
        ports: Optional[Dict[str, int]] = None,
        configure: Optional[Callable[[ConfigBuilder], ConfigBuilder]] = None,
        start: bool = False,
        loopback: Sequence[str] = ("loop1",),
        command_ports: Sequence[str] = (),
        raw_server_yaml: Optional[str] = None,
        telnet: bool = True,
        web: bool = True,
        timeline: Optional[Timeline] = None,
        timeout: float = 45.0,
    ) -> E2EServer:
        """Build (and optionally start) one server with its own ports, config dir, and state dirs.

        Args:
            name: Label used in log lines and failure messages.
            users: ``(username, permissions)`` pairs to create.
            ports: Which listeners to bind: keys ``web``, ``cli``, ``telnet``. Defaults follow
                ``web`` and ``telnet``.
            configure: Hook that receives the builder and returns it, for a scenario's own world.
            start: Start the process now and wait until every listener answers.
            loopback: Loopback port names to create before ``configure`` runs.
            command_ports: Command port names, each running ``/bin/cat``.
            raw_server_yaml: Write this text over the generated ``server.yaml``, for a scenario that
                proves the server rejects a bad file.
            telnet: Bind a Telnet listener (port menu).
            web: Bind the web console.
            timeline: Attach the timeline so server output lands on it.
            timeout: Seconds to wait for the server to be ready.
        """
        want = {"cli": True, "web": web, "telnet": telnet}
        names = [key for key in ("web", "cli", "telnet") if want.get(key)]
        allocated: Dict[str, int] = {key: int(value) for key, value in (ports or {}).items()}
        missing = [key for key in names if key not in allocated]
        for key, port in zip(missing, free_ports(len(missing))):
            allocated[key] = port
        addresses = [ServerAddress(name=key, port=allocated[key], kind="http" if key == "web" else "tcp") for key in names]

        workdir = self.tmp_path / name
        config_dir = workdir / "config"
        builder = ConfigBuilder(config_dir, server_id=f"e2e-{_test_id(self.request.node.nodeid)}-{name}")
        for username, permission in users:
            builder.with_user(username, permission)
        for port_name in loopback:
            builder.with_loopback(port_name)
        for port_name in command_ports:
            builder.with_command_port(port_name)
        if want["web"]:
            builder.with_web(int(allocated["web"]))
        builder.with_client_listener(int(allocated["cli"]))
        if want["telnet"]:
            builder.with_telnet_listener(int(allocated["telnet"]))
        if configure is not None:
            builder = configure(builder) or builder
        builder.with_log_dir(workdir / "logs")
        builder.write()
        if raw_server_yaml is not None:
            (config_dir / "server.yaml").write_text(raw_server_yaml, encoding="utf-8")

        server = E2EServer(workdir=workdir, config_dir=config_dir, addresses=addresses, name=name)
        server.recorder = timeline if timeline is not None else self.story.timeline
        server.live_output = self.options["live"]
        self.servers.append(server)
        self.story.cast(f"{name}      server  {name} on {[a.name for a in addresses]}")
        if start:
            server.start(timeout=timeout)
        return server

    # --- teardown --------------------------------------------------------------

    def teardown(self, failed: bool, keep: bool) -> List[Dict[str, Any]]:
        """Stop every server, and keep the artifacts a failure needs. Returns per-server metadata."""
        metas: List[Dict[str, Any]] = []
        if failed:
            _reveal(
                self.options,
                [
                    f"failure bundle: {self.bundle}",
                    *[f"server {s.name}: {s.web_url}  pid {s.proc.pid if s.proc else 'stopped'}" for s in self.servers],
                ],
            )
        for server in self.servers:
            if failed:
                _copy_tree(server.config_dir, self.bundle / "config")
                process_log = server.log_dir / "process.log"
                if process_log.exists():
                    shutil.copy2(process_log, self.bundle / f"server-{server.name}.log")
                ports_dir = server.log_dir / "ports"
                for port_log in sorted(ports_dir.glob("*.log")) if ports_dir.exists() else []:
                    shutil.copy2(port_log, self.bundle / f"portlog-{port_log.stem}.log")
            if failed or keep:
                metas.append(server.meta())
            if failed:
                server.stop()
            else:
                server.stop_clean()
                server.cleanup_run_dir()
        return metas


@pytest.fixture
def servers(
    request: pytest.FixtureRequest, tmp_path: Path, e2e_options: Dict[str, Any], artifacts_root: Path, story: Story
) -> Any:
    """The server factory for this test: ``servers(start=True)`` returns one started ``E2EServer``.

    Teardown stops every server it handed out. On a pass the exit code must be 0 and no child may
    survive, which is where a shutdown crash or a command-port orphan shows up.
    """
    registry = ServerRegistry(request, tmp_path, e2e_options, artifacts_root, story)
    try:
        yield registry
    finally:
        metas = registry.teardown(failed=_failed(request), keep=e2e_options["keep"])
        if metas:
            registry.bundle.mkdir(parents=True, exist_ok=True)
            (registry.bundle / "meta.json").write_text(json.dumps({"servers": metas}, indent=2), encoding="utf-8")


@pytest.fixture
def server(servers: Any, story: Story, api_for_server: Callable[[E2EServer], Api]) -> E2EServer:
    """One started server with the default world.

    The world is: users ``admin`` (admin), ``rw`` (read-write), ``ro`` (read-only); ports ``loop1``
    (Loopback, write slot capacity ``one``) and ``cat1`` (command port running ``/bin/cat``); and
    the web console, the client listener, and a Telnet listener. ``cat1`` is here because its child
    process must die with the server, which every teardown then proves.
    """
    started = servers(start=True, name="e2e", command_ports=("cat1",))
    story.header(
        server=[f"{started.web_url}  pid {started.proc.pid if started.proc else '?'}  config {started.config_dir}"],
        ports=[f"{name}  (from /api/status)" for name in api_for_server(started).port_names()],
    )
    return started


# --- HTTP ------------------------------------------------------------------------------------


@pytest.fixture
def api_for(server: E2EServer) -> Callable[..., Api]:
    """Build an ``Api`` for the running server, for a chosen user. Basic auth only."""

    def build(username: str = "admin", password: str = TEST_PASSWORD) -> Api:
        return Api(server.web_url, username=username, password=password)

    return build


@pytest.fixture
def api_for_server() -> Callable[[E2EServer], Api]:
    """Build an ``Api`` for any server, used by the header of a server that is not the default one."""

    def build(server: E2EServer, username: str = "admin") -> Api:
        return Api(server.web_url, username=username, password=TEST_PASSWORD)

    return build


@pytest.fixture
def api(api_for: Callable[..., Api]) -> Api:
    """The admin ``Api`` for the running server: setup and cross-checks, never a UI substitute."""
    return api_for("admin")


# --- browser ---------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser(e2e_options: Dict[str, Any]) -> Any:
    """One Chromium instance for the session. Skips the test when no browser is usable."""
    reason = browser_available()
    if reason:
        pytest.skip(reason)
    instance = Browser(headless=not e2e_options["headed"], slow_mo_ms=e2e_options["slow_mo"])
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def console_pages(request: pytest.FixtureRequest, e2e_options: Dict[str, Any], story: Story) -> Any:
    """Track every page and context this test opened, so teardown closes them and keeps traces.

    Yields a registry with ``.user(surface, server, user=...)`` returning a signed-in page and
    ``.console(surface, server, port)`` returning an open console page.
    """
    registry = _PageRegistry(request, e2e_options, story)
    try:
        yield registry
    finally:
        registry.teardown(failed=_failed(request), keep=e2e_options["keep"])


class _PageRegistry:
    """Owns one browser context per actor, keyed by its surface name, so cookies never cross a permission boundary."""

    def __init__(self, request: pytest.FixtureRequest, options: Dict[str, Any], story: Story):
        self.request = request
        self.options = options
        self.story = story
        self._browser: Optional[Browser] = None
        self._contexts: Dict[str, Any] = {}
        self.pages: List[ConsoleSession] = []
        self.bundle = artifact_dir_for(options["artifacts"], request.node.nodeid)

    @property
    def browser(self) -> Browser:
        """The session browser, created on first use, so a socket-only test never launches one."""
        if self._browser is None:
            reason = browser_available()
            if reason:
                pytest.skip(reason)
            self._browser = Browser(headless=not self.options["headed"], slow_mo_ms=self.options["slow_mo"])
        return self._browser

    def context(self, surface: str) -> Any:
        """The context for one surface, created once per test."""
        if surface not in self._contexts:
            self._contexts[surface] = self.browser.context(tracing=True)
        return self._contexts[surface]

    def user(self, surface: str, server: E2EServer, *, user: str, password: str = TEST_PASSWORD) -> ConsoleSession:
        """Sign one user in and register the page under ``surface``, the name the timeline and the artifacts use."""
        page = login(self.context(surface), server.web_url, user, password)
        session = ConsoleSession(page, server.web_url, surface=surface, story=self.story)
        session.user = user
        self.story.cast(f"{surface:<11} user {user!r}  browser tab {len(self.pages) + 1}")
        self.pages.append(session)
        return session

    def console(self, surface: str, server: E2EServer, port: str, *, user: str = "rw") -> ConsoleSession:
        """Sign one user in and open the console page for one port, with the test hook on."""
        session = self.user(surface, server, user=user)
        session.open(port=port, e2e=True)
        return session

    def teardown(self, failed: bool, keep: bool) -> None:
        """Close every page and context. A failure keeps the trace and a screenshot per surface."""
        if failed:
            _reveal(
                self.options,
                [
                    f"failure bundle: {self.bundle}",
                    *[f"page {p.surface}: {p.page.url}" for p in self.pages],
                    "the server is still running while you look at the page",
                ],
            )
        for session in self.pages:
            if failed:
                try:
                    session.screenshot(self.bundle / f"{session.surface.replace(':', '-')}.failure.png")
                except Exception:
                    pass  # justification: a missing screenshot must not hide the assertion that failed
            session.close()
        for surface, context in self._contexts.items():
            if failed:
                try:
                    self.bundle.mkdir(parents=True, exist_ok=True)
                    context.tracing.stop(path=str(self.bundle / f"{surface.replace(':', '-')}.trace.zip"))
                except Exception:
                    pass  # justification: tracing is a debugging aid, its failure is never the test's
            try:
                context.close()
            except Exception:
                pass  # justification: a context may already be closed when the browser died
        if self._browser is not None:
            self._browser.close()
            self._browser = None


@pytest.fixture
def console(console_pages: Any) -> Any:
    """The page registry: ``console.console(\"web:rw\", server, \"loop1\", user=\"rw\")``."""
    return console_pages


# --- text clients ---------------------------------------------------------------------------


@pytest.fixture
def clients(request: pytest.FixtureRequest, server: E2EServer, story: Story) -> Any:
    """Open and track CLI and Telnet sessions for this test, closing every one on teardown."""
    registry = _ClientRegistry(server, story)
    try:
        yield registry
    finally:
        registry.close_all()


class _ClientRegistry:
    """Opens CLI and Telnet sessions and guarantees each one is closed, reader thread included."""

    def __init__(self, server: E2EServer, story: Story):
        self.server = server
        self.story = story
        self.timeline = story.timeline
        self.sessions: List[Any] = []

    def cli(
        self,
        *,
        user: str = "admin",
        surface: Optional[str] = None,
        attach_to: Optional[str] = None,
        password: str = TEST_PASSWORD,
    ) -> CliSession:
        """Open a CLI session, authenticated, and optionally attached to one port."""
        session = open_cli_client(
            self.server,
            user=user,
            password=password,
            surface=surface or f"cli:{user}",
            attach_to=attach_to,
            timeline=self.timeline,
        )
        self.story.cast(f"{session.surface:<11} user {user!r}  {session.mode() or 'unattached'}  CLI over the client listener")
        self.sessions.append(session)
        return session

    def telnet(
        self, *, user: str = "admin", surface: str = "telnet:viewer", require_auth: bool = False, password: str = TEST_PASSWORD
    ) -> TelnetSession:
        """Open a Telnet session on the server's Telnet listener."""
        session = open_telnet_client(
            self.server,
            user=user,
            password=password,
            surface=surface,
            require_auth=require_auth,
            timeline=self.timeline,
        )
        self.story.cast(f"{surface:<11} user {user!r}  Telnet listener")
        self.sessions.append(session)
        return session

    def close_all(self) -> None:
        """Close every session, keeping the first failure so a real cause is not swallowed."""
        problem: Optional[BaseException] = None
        for session in reversed(self.sessions):
            try:
                session.close()
            except Exception as exc:  # justification: reported after the rest are closed
                problem = problem or exc
        if problem is not None:
            raise problem


# --- collection rules -----------------------------------------------------------------------


def pytest_collection_modifyitems(config: Any, items: List[Any]) -> None:
    """Skip browser scenarios that this machine cannot run, with the install command in the reason."""
    if not config.getoption("--run-e2e"):
        return  # the deselect that keeps ``make test`` untouched lives in tests/conftest.py
    reason = None
    if any(item.get_closest_marker("e2e_browser") for item in items):
        reason = browser_available()
    if not reason:
        return
    for item in items:
        if item.get_closest_marker("e2e_browser"):
            item.add_marker(pytest.mark.skip(reason=reason))


def _copy_tree(source: Path, target: Path) -> None:
    """Copy a config directory into an artifact bundle, so a failure is reproducible by hand."""
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(Path(source).iterdir()):
        if entry.is_file():
            shutil.copy2(entry, target / entry.name)
