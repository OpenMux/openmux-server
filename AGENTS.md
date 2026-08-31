# OpenMux Agent Guidelines

OpenMux is a serial/console "terminal server": a Python asyncio server (`openmux/server/`) that
exposes serial, loopback, command, and TCP-initiator ports through pluggable adapters, plus a
CLI/WebSocket client (`openmux/client/`) and an HTML5 web console (`templates/web_console/`).

## Architecture
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full runtime flow, adapter/plugin
model, and extension points. Key points:
- Adapters subclass `BaseGenericAdapter` and are registered per config section (`loopback_ports`,
  `serial_ports`, `tcp_initiator_ports`, `muxcon`, `web_console`, etc.) via `PluginRegistry`.
- Config is split across three files: `config/server.yaml` (adapters), `config/authentication.yaml`
  (users/API keys), `config/security.yaml` (allow-lists, Config Editor writable sections).
- Hot-reload is signal-driven (SIGHUP soft reload, SIGUSR1 full reload) — no HTTP reload endpoint
  outside the Config Editor plugin's own reload actions.

## Build and Test
Use the existing `.venv` (`.venv/bin/python`, `.venv/bin/pytest`) rather than system Python.
- Run full test suite: `make test` (or `pytest -v`)
- Run a focused test file: `pytest -q tests/test_<name>.py`
- Lint: `make lint` (flake8, syntax-error-only checks are hard failures, style is not)
- Format: `make format` (black + isort, line-length 127)
- Start server for manual/browser testing: use the "Run OpenMux Server" task, or
  `.venv/bin/python -m openmux.server.main -c config/server.yaml`
- After any code change, run the relevant tests (`pytest -q tests/test_<name>.py` for the
  affected area, or `make test` for the full suite) and confirm they pass before considering
  the change done. Treat a newly failing or newly skipped test as a regression to fix, not
  to ignore.

## Python Coding Style
- Formatting is enforced by `make format` (black + isort, line-length 127) — match this rather
  than PEP8's default 79/88 when wrapping lines.
- Use type hints on function signatures (`Optional`, `Dict`, `List`, `Set`, `Tuple` from `typing`);
  the codebase is not fully typed but new/edited code should be.
- Keep cyclomatic complexity low (`flake8 --max-complexity=10` is enforced); extract helpers
  rather than deeply nesting conditionals.
- Async code lives under `openmux/server/` and `openmux/client/` — never block the event loop
  (no bare `time.sleep`, blocking I/O, or sync subprocess calls in async paths).
- Docstrings are short and practical (purpose + resolution order/edge cases), not full
  Sphinx-style docs — see `_find_config_manager` in
  [openmux/server/web_plugins/config_editor.py](openmux/server/web_plugins/config_editor.py) for the norm.

## Documentation Style
Write all docs (`docs/**/*.md`, `README.md`, `CONTRIBUTING.md`, docstrings, code comments) in
Simplified Technical English (ASD-STE100):
- Use short sentences (under ~20 words) and short paragraphs.
- Use active voice: "The server loads the config", not "The config is loaded by the server".
- Use one term for one concept; do not use synonyms for the same thing (e.g. always "port",
  never mixing in "channel" or "line" for the same concept). See
  [docs/GLOSSARY.md](docs/GLOSSARY.md) for the approved word list; add new terms there.
- Use approved vocabulary and simple verb tenses; avoid "-ing" nouns (gerunds) where a plain
  verb works, e.g. "to reload" not "for reloading".
- Write instructions as imperative steps ("Run `make test`."), not narrative descriptions.
- Avoid slang, idioms, and unnecessary jargon; spell out abbreviations on first use.
- After any code change, check affected docs (`docs/**/*.md`, `README.md`, `CONTRIBUTING.md`,
  docstrings, config schema comments) and update anything that no longer matches the new
  behavior. Do not consider a change done while docs describe the old behavior.

## Changelog
[CHANGELOG.md](CHANGELOG.md) records what a user must know before upgrading: config key
changes (renames, new required keys, behavior changes for existing keys), behavior changes
visible in access control, federation, or client/server protocol, and new user-facing
features. Update it as part of the same change:
- After committing a user-visible change, add one short entry to the `Unreleased` section of
  `CHANGELOG.md` and commit it in the same commit (or immediately after).
- Classify the entry: "Config changes to check before upgrading" only when the user might
  need to edit a config file or accept changed behavior; otherwise "Behavior changes" or
  "Web console and observability".
- Keep entries in the same documentation style as the rest of the docs. Name the exact
  config key and file (`security.yaml`, `server.yaml`). Do not paste commit messages.
- At release, rename `Unreleased` to the version number (e.g. `1.0.3`), keep the "since
  v1.0.2" line, and start a new `Unreleased` section for the next release.

## Conventions
- Exception handling & logging policy is strict — read
  [CONTRIBUTING.md](CONTRIBUTING.md) before touching `except Exception` blocks. Silent
  swallows require an inline `# justification: ...` comment.
- The Config Editor web console (`templates/web_console/config_editor.html.j2`) embeds a large
  inline `<script>` as a single Jinja2 template. There is no JS linter/build step for it, so a
  syntax error (e.g. a stray trailing comma) silently breaks the *entire* script with no visible
  error other than sub-views failing to switch. After editing this file, sanity-check the JS by
  eye or via a browser console before considering the change done.
- Config Editor sub-pages are plain query-param views (`?view=ports|listeners|muxcon|auth|server|reload`)
  switched client-side by `updateView()` — not separate server routes.

