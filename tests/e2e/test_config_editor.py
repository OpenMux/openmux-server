"""CFG: what the Config Editor changes, when it changes it, and who may ask (CFG-01 to CFG-17).

The editor is one page over four owners, and each layer can be right while the page is wrong, so every scenario here
states the pair a person would otherwise reconcile by hand:

* access and CSRF come from ``web_plugins/config_editor.py``, which asks the web console for the role and the token;
* what a save writes, and what it refuses to write, come from that plugin's ``/apply`` and ``/validate``;
* what a save *starts* comes from ``main.py``: ``reload_adapters_soft()`` reconciles ports, PDUs and listeners in
  place, while ``reload_adapters_full()`` stops and recreates every adapter and restarts the web console after the
  reply it is owed;
* what an open page learns without a reload comes from ``layout.js`` (``pdus_changed``) and ``console.js``
  (``reload_applied``), both fed by the socket that page already holds open.

Two facts about these worlds are simulations that matter. The ``dummy`` PDU driver answers instantly and needs no
device, so a PDU that appears in the file always starts. And the one adapter failure this tier can produce on demand
is a Serial port whose device path no host has, which the server starts offline rather than refusing.

Unlike the plan for this suite, no scenario depends on another: each builds its server, makes its edit, and proves
its own save-then-reload-then-see order in one run. That order is a product rule, so it belongs inside the scenario
that claims it rather than in the order of the file.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Dict, List, Sequence, Set

import pytest
import yaml

from .harness import HttpSession
from .story import step, surfaces

pytestmark = [pytest.mark.e2e, pytest.mark.feature]

TEST_PASSWORD = "passwd"
EDITOR = "/config-editor"
#: What ``config_editor.py`` answers for a role it does not grant. The plugin renders that status line itself.
FORBIDDEN = "403: Forbidden"
#: The sentinel ``/data`` sends instead of a stored secret. Nothing holding it may reach a config file.
MASK = "********"
SECURITY_BLOCKED = "Changes to {section} are blocked by the security policy"
#: The note an editor section carries when ``security.yaml`` closed it (``config_editor.js``).
READ_ONLY_UI = "Read-only (security policy)"
#: The inline answer when a new row is saved with a required field left empty (``config_editor.js``).
MISSING_DEVICE = "Missing required: device"
#: The confirm the Reload view raises before a full reload (``config_editor.js``).
FULL_RELOAD_CONFIRM = "Full reload will stop and recreate adapters, briefly interrupting connections. Continue?"
#: A device path no host has, for the one adapter this tier can force to come up with nothing behind it.
ABSENT_TTY = "/dev/openmux-e2e-absent-serial"
#: The reason the status gives for that port, and the line the server logs once for it.
NO_SUCH_DEVICE = "does not exist"


# --- editor requests -----------------------------------------------------------


def _data(api: Any) -> Dict[str, Any]:
    """The whole ``/config-editor/data`` answer: the running config plus what the policy allows."""
    body = api.json(f"{EDITOR}/data")
    assert isinstance(body, dict), f"data is not an object: {str(body)[:200]}"
    return body


def _config(api: Any) -> Dict[str, Any]:
    """The config as the browser sees it: masked secrets, every section the files hold in one mapping."""
    config = _data(api)["config"]
    assert isinstance(config, dict), f"config is not an object: {str(config)[:200]}"
    return config


def _payload(api: Any, *, keep_auth: bool = False) -> Dict[str, Any]:
    """A save body shaped like the page's, without ``authentication`` unless a scenario needs its accounts.

    ``authentication`` lives in its own file, so a ports save does not carry it. The masked hash comes back unchanged
    either way, which is what the preservation scenario needs.
    """
    body = json.loads(json.dumps(_config(api)))
    if not keep_auth:
        body.pop("authentication", None)
    return body


def _apply(api: Any, payload: Dict[str, Any], expect: int = 200) -> Dict[str, Any]:
    """Save one config and return the answer, asserting only the status the caller named."""
    status, _, text = api.post(f"{EDITOR}/apply", json_body=payload, expect=(expect,))
    assert status == expect, f"save answered {status}: {text[:300]}"
    try:
        body = json.loads(text)
    except ValueError:
        raise AssertionError(f"save answer is not JSON: {text[:300]}")
    assert isinstance(body, dict), f"save answer is not an object: {text[:300]}"
    return body


def _reload(api: Any, kind: str, expect: int = 200) -> Dict[str, Any]:
    """Ask for one reload kind and return its answer, which is the object the page shows."""
    status, _, text = api.post(f"{EDITOR}/reload/{kind}", expect=(expect,))
    assert status == expect, f"{kind} reload answered {status}: {text[:300]}"
    body = json.loads(text)
    assert isinstance(body, dict), f"{kind} reload answer is not an object: {str(body)[:300]}"
    return body


def _summary(answer: Dict[str, Any]) -> Dict[str, Any]:
    """The summary of one reload answer, with a failure that names what came back instead."""
    assert answer.get("ok") is True, f"reload did not report ok: {json.dumps(answer)[:400]}"
    summary = answer.get("summary")
    assert isinstance(summary, dict), f"reload carried no summary: {json.dumps(answer)[:400]}"
    return summary


def _reconciled(summary: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """What a soft reload did to one adapter kind: which names it added, updated, removed, or left alone."""
    adapters = summary.get("adapters")
    assert isinstance(adapters, dict), f"soft summary has no adapters: {json.dumps(summary)[:400]}"
    result = adapters.get(kind)
    assert isinstance(result, dict), f"soft summary has no {kind}: {json.dumps(adapters)[:400]}"
    return result


def _running(api: Any) -> Set[str]:
    """The port names the running server lists."""
    return set(api.port_names())


# --- page requests -------------------------------------------------------------


def _open_editor(viewer: Any, query: str = "") -> Any:
    """Open the editor page and wait until the region for that view holds content.

    This page carries no ``?e2e=1`` hook, so it is read at page level: each region appears as its script fills it,
    which is what Playwright's own wait is for. The region is named because the page keeps the other regions in the
    document, hidden.
    """
    viewer.goto(f"{EDITOR}{query}")
    viewer.wait_for_selector(f"#{_VIEW_REGION.get(_view_name(query), 'view-setup')}", state="visible")
    return viewer


#: Which region each ``?view=`` value shows, as ``updateView()`` in ``config_editor.js`` does it.
_VIEW_REGION = {
    "ports": "view-ports",
    "power": "view-power",
    "listeners": "view-listeners",
    "muxcon": "view-muxcon",
    "actions": "view-actions",
    "auth": "view-auth",
    "reload": "view-reload",
    "setup": "view-setup",
}


def _view_name(query: str) -> str:
    """The view one editor query asks for, empty being Ports and anything else the Server page."""
    if not query:
        return "ports"
    name = query.split("view=", 1)[-1].split("&", 1)[0]
    return name if name in _VIEW_REGION else "setup"


def _ports_view_text(viewer: Any) -> str:
    """The whole Ports view, which is where a saved port appears once the page has read ``/data``."""
    return viewer.page_text("#view-ports")


def _click_id(viewer: Any, element_id: str) -> None:
    """Press one button by id on a page with no test hook."""
    pressed = viewer.page.evaluate(
        "(id) => { const el = document.getElementById(id); if (!el) return false; el.click(); return true; }", element_id
    )
    assert pressed, f"the page has no element with id {element_id!r}"


def _click_add_item(viewer: Any) -> None:
    """Open the editor's inline row, by pressing the same Add item button a person presses."""
    headers = viewer.page.evaluate(
        """() => {
            for (const table of document.querySelectorAll('#view-ports table')) {
                const heads = [...table.querySelectorAll('thead th')].map(t=>(t.textContent||'').trim());
                if (!heads.some(h=>/^name/i.test(h))) continue;
                const add = [...table.parentElement.querySelectorAll('button')].find(b=>/add item/i.test(b.textContent||''));
                if (add) { add.click(); return heads.join(','); }
            }
            return null;
        }"""
    )
    assert headers, "the Ports view offered no table with a Name column and an Add item button"
    viewer.wait_for_selector(".inline-editor-panel", state="visible")


def _panel_text(viewer: Any) -> str:
    """The text of the inline row the editor opened, including any reason it refused to save."""
    text = viewer.page.evaluate(
        "() => { const p = document.querySelector('.inline-editor-panel'); return p ? (p.innerText||'') : ''; }"
    )
    return str(text)


def _fill_panel_field(viewer: Any, label: str, value: str) -> None:
    """Type into one field of the inline row, found by the label a person reads."""
    filled = viewer.page.evaluate(
        """([label, value]) => {
            const panel = document.querySelector('.inline-editor-panel');
            if (!panel) return false;
            for (const field of panel.querySelectorAll('.field')) {
                const name = ((field.querySelector('label,span')||field).textContent||'').trim();
                if (!name.toLowerCase().startsWith(label.toLowerCase())) continue;
                const input = field.querySelector('input,select,textarea');
                if (!input) continue;
                input.value = value;
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            return false;
        }""",
        [label, value],
    )
    assert filled, f"the inline row has no field labelled {label!r}: {_panel_text(viewer)[:300]}"


def _click_panel_button(viewer: Any, caption: str) -> None:
    """Press one button of the inline row, found by its caption."""
    pressed = viewer.page.evaluate(
        """(caption) => {
            const panel = document.querySelector('.inline-editor-panel');
            if (!panel) return false;
            const button = [...panel.querySelectorAll('button')]
              .find(b=>(b.textContent||'').trim().toLowerCase().startsWith(caption.toLowerCase()));
            if (!button) return false;
            button.click();
            return true;
        }""",
        caption,
    )
    assert pressed, f"the inline row has no {caption!r} button: {_panel_text(viewer)[:300]}"


def _reload_status(viewer: Any) -> str:
    """What the Reload view reports after one of its buttons was pressed."""
    return viewer.text_of("#reloadStatus")


def _listener_answers(port: int) -> bool:
    """Whether a listener port accepts a connection, for a listener this harness did not allocate."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1.0):
            return True
    except OSError:
        return False


def _health_answers(world: Any) -> int:
    """What the health route answers, which needs no session: proof that the port serves again."""
    return HttpSession(world.web_url, surface="http").get("/healthz", expect=(200,))[0]


def _pdu_names(api: Any) -> List[str]:
    """The PDU names ``/api/power`` reports, which is what the Power menu is built from."""
    pdus = api.power().get("pdus") or []
    if isinstance(pdus, dict):
        return sorted(str(name) for name in pdus)
    return sorted(str(entry.get("name")) for entry in pdus)


def _feed_refs(api: Any, port_name: str) -> List[str]:
    """The outlet refs ``/api/power`` maps to one port, which is where a console's feeds show once wired."""
    refs: List[str] = []
    for pdu in api.power().get("pdus") or []:
        for outlet in pdu.get("outlets") or []:
            if port_name in (outlet.get("mapped_ports") or []):
                refs.append(f"{pdu.get('name')}.{outlet.get('id')}")
    return sorted(refs)


def _free_port() -> int:
    """One free TCP port, for a listener this harness does not allocate."""
    from .harness import free_ports

    return int(free_ports(1)[0])


def stored_hash_for(text: str, username: str) -> str:
    """The hash ``authentication.yaml`` holds for one account, which the editor must never overwrite.

    The harness stores a salted SHA-256 and the editor serves a mask, so the test compares the stored value with the
    mask instead of with a format: a hash is what a login has to work with.
    """
    stored = yaml.safe_load(text) or {}
    for entry in stored.get("users") or []:
        if isinstance(entry, dict) and entry.get("username") == username:
            value = str(entry.get("password_hash") or "")
            assert value and value != MASK and len(value) >= 32, f"{username} holds {value!r}"
            return value
    raise AssertionError(f"authentication.yaml lists no account {username!r}: {text[:400]}")


# --- config edits --------------------------------------------------------------


def _add_loopback(config: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Add one Loopback port to a save body, in the shape a saved port has."""
    ports = config.setdefault("loopback_ports", [])
    ports.append({"name": name, "description": f"e2e added by the editor: {name}"})
    return config


def _add_pdu(config: Dict[str, Any], name: str, outlets: Sequence[str]) -> Dict[str, Any]:
    """Add one ``dummy`` PDU to a save body, which needs no device behind it."""
    power = config.setdefault("power", {"enabled": True, "pdus": []})
    power["enabled"] = True
    power.setdefault("pdus", []).append({"name": name, "driver": "dummy", "outlets": [{"id": ref} for ref in outlets]})
    return config


def _port_feeds(config: Dict[str, Any], port_name: str, feeds: Sequence[str]) -> Dict[str, Any]:
    """Give one running port its power feeds, in the shape a console declares them."""
    for entry in config.get("loopback_ports") or []:
        if entry.get("name") == port_name:
            entry["power"] = list(feeds)
            return config
    raise AssertionError(f"the config holds no loopback port {port_name!r}")


def _add_telnet_listener(config: Dict[str, Any], name: str, port: int) -> Dict[str, Any]:
    """Add a second Telnet listener to a save body, on a port nobody holds.

    The new entry copies the running one, as the page's own row editor does, and keeps ``require_auth`` on: a listener
    that asks for no password cannot grant a Write slot, so a scenario that drives a port through it needs an account.
    """
    listeners = config.setdefault("telnet_listener", [])
    if not isinstance(listeners, list):
        listeners = config["telnet_listener"] = [listeners]
    assert listeners, "the world has no Telnet listener to copy"
    entry: Dict[str, Any] = json.loads(json.dumps(listeners[0]))
    entry.update({"name": name, "bind_port": int(port), "require_auth": True})
    listeners.append(entry)
    return config


def _with_absent_serial(builder: Any, name: str) -> Any:
    """Configure a Serial port whose device no host has, which is what forces an adapter to start offline."""
    return builder.with_serial_port(name, ABSENT_TTY)


# ===============================================================================
# Who may open the editor, and what it shows them
# ===============================================================================


@surfaces("http", "web:admin", "web:rw", "web:ro")
def test_cfg01_only_an_administrator_may_open_the_editor(servers, api_for, api_for_server, console):
    """Every editor route is for an administrator, and the entry is not in anyone else's menu.

    Reading is gated as strictly as writing, so a read-only account cannot read the file either. A browser with no
    session at all is sent to the login page instead of being refused in place.
    """
    world = servers(name="gate", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    step("an administrator gets the page and the data route")
    admin = api_for_server(world)
    assert admin.get(f"{EDITOR}/data")[0] == 200, "an administrator was refused the data route"
    assert admin.request("GET", EDITOR, headers={"Accept": "text/html"}, expect=(200,))[0] == 200
    step("and a signed-in browser is served the Editor, from its own menu")
    administrator = console.user("web:admin", world, user="admin")
    assert "Config Editor" in administrator.nav_labels(), administrator.nav_labels()
    _open_editor(administrator)
    assert "Config Editor" in administrator.page_text(".page"), administrator.page_text(".page")
    step("a read-write and a read-only account are refused in place")
    for username in ("rw", "ro"):
        session = api_for_server(world, username)
        blocked, _, body = session.request("GET", EDITOR, expect=(403,))
        assert blocked == 403, f"{username} was not refused the page: {blocked}"
        assert FORBIDDEN in body, f"{username} saw a page instead of a refusal: {body[:200]}"
        data_status, _, data_body = session.request("GET", f"{EDITOR}/data", expect=(403,))
        assert data_status == 403, f"{username} read the config: {data_body[:200]}"
        for path in ("/config-editor/apply", "/config-editor/reload/soft", "/config-editor/reload/full"):
            refused = session.request("POST", path, json_body={}, expect=(403,))
            assert FORBIDDEN in refused[2], f"{username} was allowed {path}"
    step("the editor is absent from an operator's menu and from a reader's")
    for username in ("rw", "ro"):
        reader = console.user(f"web:{username}", world, user=username)
        assert "Config Editor" not in reader.nav_labels(), f"{username} was offered the editor: {reader.nav_labels()}"
    step("a browser with no session is sent to the login page")
    anon = HttpSession(world.web_url, surface="http")
    redirected, headers, _ = anon.get(EDITOR, expect=(302,))
    assert "/login" in (anon.location or ""), f"a sessionless request was not redirected: {redirected} {headers}"


@surfaces("web:admin")
def test_cfg02_every_sub_page_opens_and_an_unknown_one_shows_the_server_page(servers, console):
    """Each Config Editor sub-page is a view of one page, and anything unrecognised shows the Server page.

    The view is picked in the page, so a mistyped name does not fail: it falls back to a section an administrator can
    use rather than a blank screen.
    """
    world = servers(name="views", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    viewer = console.user("web:admin", world, user="admin")
    wanted = {
        "ports": "Config Editor - Ports",
        "power": "Config Editor - Power",
        "listeners": "Config Editor - Listeners",
        "muxcon": "Config Editor - Muxcon",
        "actions": "Config Editor - Actions",
        "auth": "Config Editor - Authentication",
        "reload": "Config Editor - Reload",
        "setup": "Config Editor - Server",
    }
    for query, title in wanted.items():
        step(f"open the {query} view")
        _open_editor(viewer, f"?view={query}")
        assert viewer.page_text(".page") == title, f"?view={query} headed itself {viewer.page_text('.page')!r}"
    step("the page opens on Ports, which is where its own menu starts")
    viewer.goto(EDITOR)
    viewer.wait_for_selector("#view-ports", state="visible")
    assert viewer.page_text(".page") == "Config Editor - Ports", viewer.page_text(".page")
    step("an unknown view, and a view named with the wrong case, both show the Server page")
    for query in ("?view=nosuchview", "?view=PORTS"):
        _open_editor(viewer, query)
        assert viewer.page_text(".page") == "Config Editor - Server", f"{query} headed itself {viewer.page_text('.page')!r}"


@surfaces("http", "web:admin")
def test_cfg03_the_editor_shows_the_running_config_with_every_secret_masked(servers, api_for, api_for_server, console):
    """The editor reads the config the process runs on, and never sends a stored secret to the browser.

    The mask is the plugin's sentinel and the file keeps the real value, so a save cannot be blamed for losing a
    password. What the security policy closes is reported to the page, which is how the page can mark a section
    before anyone edits it.
    """
    world = servers(name="masked", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    api = api_for_server(world)
    step("the served config is the one the process runs on")
    config = _config(api)
    assert [entry["name"] for entry in config["loopback_ports"]] == ["keep"], config["loopback_ports"]
    assert config["client_listener"]["port"] == world.port_of("cli"), config["client_listener"]
    assert config["web_console"]["port"] == world.port_of("web"), config["web_console"]
    step("no stored secret is in it")
    served = json.dumps(config)
    assert MASK in served, "the accounts were served unmasked"
    assert TEST_PASSWORD not in served, "a password reached the browser"
    users = config["authentication"]["users"]
    assert MASK in json.dumps(users), users
    step("the file still holds a hash the login works with")
    stored = world.config_text("authentication.yaml")
    assert MASK not in stored, "the mask was written to the file"
    assert stored_hash_for(stored, "rw"), "the stored account carries no hash"
    assert api_for_server(world, "rw").get("/api/status")[0] == 200, "a real login stopped working"
    step("the policy the page is told about is the one in security.yaml")
    meta = _data(api)
    assert meta["writable_enforced"] is True, meta
    assert "loopback_ports" in meta["writable_sections"] and "power" in meta["writable_sections"], meta["writable_sections"]
    assert meta["access_default"] == "allow", meta["access_default"]
    step("the Ports view lists the port without asking for a reload")
    viewer = _open_editor(console.user("web:admin", world, user="admin"))
    assert "keep" in _ports_view_text(viewer), _ports_view_text(viewer)[:400]


@surfaces("web:admin", "http")
def test_cfg04_the_page_refuses_a_new_port_row_that_omits_a_required_field(servers, api_for_server, console):
    """A new row the page cannot complete is refused there, and nothing is written.

    The reason names the field the form marks required, so the person sees what to fix before saving. The config file
    is unchanged afterwards because the request was never made.
    """
    world = servers(name="rowcheck", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    api = api_for_server(world)
    before = world.config_text()
    viewer = _open_editor(console.user("web:admin", world, user="admin"))
    step("open a new row and try to save it empty")
    _click_add_item(viewer)
    _click_panel_button(viewer, "Save item")
    refused = _panel_text(viewer)
    assert "Missing required" in refused, f"the empty row was accepted: {refused[:300]}"
    step("name the port and leave the device out")
    _fill_panel_field(viewer, "Name", "rowcheck1")
    _click_panel_button(viewer, "Save item")
    assert MISSING_DEVICE in _panel_text(viewer), f"the row saved without a device: {_panel_text(viewer)[:300]}"
    step("the row stays open, the table lists only the old port, and the file did not move")
    assert "rowcheck1" not in _ports_view_text(viewer), _ports_view_text(viewer)[:400]
    assert world.config_text() == before, "an incomplete row reached the config file"
    assert _running(api) == {"keep"}, api.port_names()


# ===============================================================================
# What a save writes, and what it takes to make it real
# ===============================================================================


@surfaces("http", "telnet:rw")
def test_cfg05_a_saved_port_starts_on_soft_reload_and_the_next_reload_calls_it_unchanged(servers, api_for_server, clients):
    """A save writes the file and starts nothing: the soft reconcile is what attaches a person to the port.

    The reload answer separates the outcomes a save can have, added, unchanged, and removed, so an operator can
    confirm what the file held before. A console attached to the old port stays attached through it.
    """
    world = servers(name="soft", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    api = api_for_server(world)
    attached = clients.telnet(user="rw", surface="telnet:rw", require_auth=True, server=world)
    attached.choose_port("keep")
    step("save a port nobody asked to start")
    assert _apply(api, _add_loopback(_payload(api), "later")) == {"ok": True}
    assert "later" in world.config_text(), "the save did not reach the file"
    assert _running(api) == {"keep"}, f"a save started the port: {api.port_names()}"
    step("the soft reconcile adds it and keeps the attached console")
    loopback = _reconciled(_summary(_reload(api, "soft")), "loopback")
    assert loopback["added"] == ["later"], f"the reconcile did not add the port: {loopback}"
    assert loopback["removed"] == [], loopback
    assert "AFTER" in attached.wait_for_echo("AFTER"), f"the attached console lost its port: {attached.text()[-300:]!r}"
    assert _running(api) == {"keep", "later"}, api.port_names()
    step("the next reconcile calls both ports unchanged")
    again = _reconciled(_summary(_reload(api, "soft")), "loopback")
    assert again["added"] == [] and set(again["unchanged"]) == {"keep", "later"}, again
    assert _running(api) == {"keep", "later"}, api.port_names()


@surfaces("http")
def test_cfg06_an_invalid_edit_is_refused_and_the_file_keeps_the_old_port(servers, api_for_server):
    """One bad field stops the whole save, and the reason names the port and the field.

    The page's check button runs the same rules, and neither path writes: the file keeps its previous content, so an
    unusable edit cannot half-apply.
    """
    world = servers(name="invalid", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    api = api_for_server(world)
    before = world.config_text()
    step("name a field the schema does not have")
    unknown = _add_loopback(_payload(api), "edged1")
    unknown["loopback_ports"][-1]["baud"] = 42
    refused = _apply(api, unknown, expect=400)
    assert refused.get("error") is True, refused
    assert "baud" in refused["message"], refused
    step("omit the name the list needs")
    unnamed = _add_loopback(_payload(api), "edged2")
    unnamed["loopback_ports"][-1].pop("name")
    second = _apply(api, unnamed, expect=400)
    assert "required" in second["message"].lower(), second["message"]
    step("neither attempt wrote anything")
    assert world.config_text() == before, "a refused save changed the config file"
    assert _running(api) == {"keep"}, api.port_names()
    step("the check button answers the same way, and agrees when the edit is good")
    status, _, text = api.post(f"{EDITOR}/validate", json_body=unnamed, expect=(200, 400))
    assert status == 400 and "required" in text.lower(), text[:200]
    ok_status, _, ok_text = api.post(f"{EDITOR}/validate", json_body=_add_loopback(_payload(api), "edged3"), expect=(200,))
    assert json.loads(ok_text) == {"ok": True}, ok_text
    assert world.config_text() == before, "the check button wrote the file"


@surfaces("http")
def test_cfg07_a_section_the_security_policy_closes_is_refused_by_name(servers, api_for_server):
    """``config_editor.allowed`` picks which sections a save may change, and a refusal names the section.

    The policy is checked after the schema, so a malformed edit is reported as malformed first. A save that changes
    nothing stays allowed under a closed policy, because there is nothing to block.
    """
    closed = servers(
        name="policy_closed",
        loopback=("keep",),
        telnet_kwargs={"require_auth": True},
        configure=lambda builder: builder.with_writable_sections(["loopback_ports", "authentication"]),
        start=True,
    )
    api = api_for_server(closed)
    step("the page is told which sections it may open")
    meta = _data(api)
    assert meta["writable_enforced"] is True, meta
    assert meta["writable_sections"] == ["authentication", "loopback_ports"], meta["writable_sections"]
    before = closed.config_text()
    step("a section the policy closed is refused, by name")
    refused = _apply(api, _add_pdu(_payload(api), "kept_out", ["1"]), expect=403)
    assert refused.get("error") is True, refused
    assert SECURITY_BLOCKED.format(section="power") in refused["message"], refused["message"]
    assert closed.config_text() == before, "a blocked save changed the config file"
    step("the same edit is written once the section is on the list")
    open_world = servers(
        name="policy_open",
        loopback=("keep",),
        telnet_kwargs={"require_auth": True},
        configure=lambda builder: builder.with_writable_sections(["loopback_ports", "power"]),
        start=True,
    )
    open_api = api_for_server(open_world)
    assert _apply(open_api, _add_pdu(_payload(open_api), "let_in", ["1"])) == {"ok": True}
    assert "let_in" in open_world.config_text(), open_world.config_text()
    step("a save that changes nothing is still allowed under the closed policy")
    assert _apply(api, _payload(api)) == {"ok": True}
    before_values = yaml.safe_load(before)
    assert yaml.safe_load(closed.config_text()) == before_values, "a save that changed nothing changed a value"


@surfaces("web:admin")
def test_cfg08_the_page_marks_a_section_the_policy_closed_as_read_only(servers, console):
    """A section ``security.yaml`` closed is shown unusable, not merely refused later.

    Its fields arrive disabled with the reason beside them, so an edit cannot be attempted in the hope that the save
    will take it.
    """
    world = servers(
        name="readonly",
        loopback=("keep",),
        telnet_kwargs={"require_auth": True},
        configure=lambda builder: builder.with_writable_sections(["loopback_ports"]),
        start=True,
    )
    viewer = _open_editor(console.user("web:admin", world, user="admin"), "?view=auth")
    step("the accounts view says it is closed")
    assert READ_ONLY_UI in viewer.page_text("#view-auth"), viewer.page_text("#view-auth")[:400]
    step("and every field in it is disabled")
    counted = viewer.page.evaluate(
        """() => { const view = document.getElementById('view-auth');
            const fields = [...view.querySelectorAll('input,select,textarea,button')];
            return {total: fields.length, disabled: fields.filter(f=>f.disabled).length}; }"""
    )
    assert counted["total"] > 0, counted
    assert counted["disabled"] == counted["total"], counted


@surfaces("http")
def test_cfg09_saving_keeps_what_the_editor_never_shows(servers, api_for_server):
    """A save writes what the page models and preserves the rest of the file.

    Two kinds of value sit in that rest: a secret the browser only ever saw masked, and a key the page does not
    render at all. Both survive a save of the section that holds them, and the mask never lands in the file.
    """
    world = servers(name="preserve", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    api = api_for_server(world)
    step("a secret the page cannot see keeps its stored value")
    bodies = _payload(api, keep_auth=True)
    assert MASK in json.dumps(bodies["authentication"]["users"]), bodies["authentication"]["users"]
    assert _apply(api, bodies) == {"ok": True}
    stored = world.config_text("authentication.yaml")
    assert MASK not in stored, "the mask was written instead of the stored hash"
    assert stored_hash_for(stored, "rw"), "a save replaced the stored hash"
    step("a key the page does not render survives a save of its own section")
    seeded = world.config_text().replace("web_console:\n", "web_console:\n  sso_secret: hand-written-shared-secret\n", 1)
    assert seeded != world.config_text(), "the world has no web_console section to seed"
    (world.config_dir / "server.yaml").write_text(seeded, encoding="utf-8")
    assert _reload(api, "soft")["ok"] is True, "the seeded secret was not picked up"
    assert _apply(api, _payload(api)) == {"ok": True}
    assert "hand-written-shared-secret" in world.config_text(), "a save deleted a key the editor never renders"
    step("a save changes what it names and leaves the other sections alone")
    assert _apply(api, _add_loopback(_payload(api), "kept_by_side")) == {"ok": True}
    after = world.config_text()
    assert "kept_by_side" in after, after[-500:]
    for section in ("telnet_listener", "client_listener", "web_console", "logging"):
        assert section in after, f"a save dropped the {section} section"
    step("a masked secret with no stored row is cleared rather than stored as the mask")
    ghost = _payload(api, keep_auth=True)
    ghost["authentication"]["users"].append({"username": "ghost1", "permissions": "read-only", "password_hash": MASK})
    status, _, text = api.post(f"{EDITOR}/apply", json_body=ghost, expect=(200, 400))
    file_text = world.config_text("authentication.yaml")
    assert MASK not in file_text, f"the mask became the account's secret: {file_text[:300]}"
    assert ("ghost1" in file_text) == (status == 200), f"save said {status} ({text[:120]}) and the file says {file_text[:200]}"


@surfaces("http")
def test_cfg10_a_cookie_save_without_the_csrf_token_is_refused(servers, api_for_server):
    """A save that arrives with a session cookie must also prove it came from that page.

    The token rides in ``X-OMX-CSRF``. Without it the request is refused, and the file is untouched. The same body
    with the token is accepted, and a request that authenticates by credential instead of cookie is not asked for a
    token, because it carries no session to forge.
    """
    world = servers(name="csrf", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    api = api_for_server(world)
    before = world.config_text()
    viewer = HttpSession(world.web_url, surface="http")
    viewer.login("admin", TEST_PASSWORD)
    step("the administrator's own body is refused without the token")
    forged = _add_loopback(_payload(api), "forged1")
    status, _, text = viewer.post_json(f"{EDITOR}/apply", forged, expect=(403,))
    assert "CSRF" in text, text[:200]
    assert world.config_text() == before, "a save without the token wrote the file"
    step("with the token the same body is accepted")
    saved = _add_loopback(_payload(api), "tokened1")
    status, _, text = viewer.post_json(f"{EDITOR}/apply", saved, csrf=True, expect=(200, 403))
    assert status == 200, f"a save carrying the token was refused: {text[:300]}"
    assert "tokened1" in world.config_text(), world.config_text()
    assert "forged1" not in world.config_text(), world.config_text()
    step("a request that carries a credential and no session cookie is asked for no token")
    unsigned = _add_loopback(_payload(api), "keyed1")
    assert api.request("POST", f"{EDITOR}/apply", json_body=unsigned, expect=(200,))[0] == 200
    assert "keyed1" in world.config_text(), world.config_text()
    step("but the same credential inside a session that carries a cookie is refused without one")
    key = viewer.with_basic("admin", TEST_PASSWORD)
    with_cookie = _add_loopback(_payload(api), "cookie_keyed")
    refused = key.post_json(f"{EDITOR}/apply", with_cookie, expect=(403,))
    assert "CSRF" in refused[2], refused[2][:200]
    assert "cookie_keyed" not in world.config_text(), world.config_text()


# ===============================================================================
# What an open page learns without being reloaded
# ===============================================================================


@surfaces("http", "web:rw")
def test_cfg11_a_pdu_the_editor_added_lands_in_the_power_menu_of_an_open_page(servers, api_for_server, console):
    """A PDU that appears while a page is open reaches that page's Power menu.

    The page is told over the socket it already holds, so the switch a person must not flip by accident is listed
    without anyone reloading.
    """
    world = servers(
        name="powermenu",
        loopback=("solo",),
        telnet_kwargs={"require_auth": True},
        configure=lambda builder: builder.with_dummy_pdu("rack1", ["1", "2"]),
        start=True,
    )
    api = api_for_server(world)
    viewer = console.user("web:rw", world, user="rw")
    assert viewer.power_nav_pdus() == ["rack1"], "the world did not start with one PDU listed"
    step("save and reconcile a second PDU")
    assert _apply(api, _add_pdu(_payload(api), "rack2", ["1"])) == {"ok": True}
    power = _reconciled(_summary(_reload(api, "soft")), "power")
    assert power["added"] == ["rack2"], power
    step("the open page lists both, with no reload")
    viewer.wait_until(lambda: viewer.power_nav_pdus() == ["rack1", "rack2"], timeout=12.0, what="both PDUs in the Power menu")
    assert {"rack1", "rack2"} <= set(_pdu_names(api)), api.power().get("pdus")


@surfaces("http", "web:rw")
def test_cfg12_feeds_added_to_a_console_show_on_an_open_console_page(servers, api_for_server, console):
    """Feeds given to a running console show on that console's page, because the reconcile tells the page.

    Before the edit the page cannot answer for power and shows a dash. After it the same page shows the feed lit, and
    its terminal was never dropped.
    """
    world = servers(
        name="feeds",
        loopback=("solo",),
        telnet_kwargs={"require_auth": True},
        configure=lambda builder: builder.with_dummy_pdu("rack1", ["1"]),
        start=True,
    )
    api = api_for_server(world)
    viewer = console.console("web:rw", world, "solo", user="rw")
    step("before the edit the page has no answer about power")
    assert viewer.power_badge()["label"] == "—", viewer.power_badge()
    step("give the port its feed and reconcile")
    assert _apply(api, _port_feeds(_payload(api), "solo", ["rack1.1"])) == {"ok": True}
    loopback = _reconciled(_summary(_reload(api, "soft")), "loopback")
    # Measured: a feeds-only edit leaves the adapter itself alone, so the reconcile calls the port unchanged. The feed
    # arrives through the wiring step, which is not part of the adapter diff. What a person sees is what follows.
    assert loopback["unchanged"] == ["solo"] and loopback["added"] == [] and loopback["removed"] == [], loopback
    assert _feed_refs(api, "solo") == ["rack1.1"], api.power()
    step("the open page lights it, without being reloaded")
    viewer.wait_until(lambda: viewer.power_badge()["label"] == "1/1", timeout=12.0, what="the power badge to read 1/1")
    assert "1 of 1" in viewer.power_badge_title(), viewer.power_badge_title()
    assert viewer.mode() == "read-write", viewer.view()


@surfaces("http", "telnet:rw", "telnet:new")
def test_cfg13_a_telnet_listener_the_editor_saved_answers_after_a_soft_reload(servers, api_for_server, clients):
    """A listener written to the file is started by a soft reload, and the open listener keeps its client.

    A listener used to need a full reload, which ended every session. The reconcile starts the new port in place, so
    the console attached to the first listener is untouched.
    """
    world = servers(name="listener", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    api = api_for_server(world)
    first = clients.telnet(user="rw", surface="telnet:rw", require_auth=True, server=world)
    first.choose_port("keep")
    second_port = _free_port()
    step("save the second listener, with the port it should serve, and reconcile")
    saved = _add_telnet_listener(_add_loopback(_payload(api), "later"), "telnet2", second_port)
    assert _apply(api, saved) == {"ok": True}
    listeners = _reconciled(_summary(_reload(api, "soft")), "telnet_listener")
    assert listeners["added"] == ["telnet2"], listeners
    assert _listener_answers(second_port), f"the new listener never opened on {second_port}"
    step("a client signs in on the new listener and drives the port the editor added")
    second = clients.telnet(user="rw", surface="telnet:new", require_auth=True, server=world, port=second_port)
    second.choose_port("later")
    assert "Port: later" in second.text(), second.text()[-300:]
    assert "ON-NEW" in second.wait_for_echo("ON-NEW"), second.text()[-300:]
    step("and the client on the first listener is still attached to its own port")
    assert "ON-FIRST" in first.wait_for_echo("ON-FIRST"), first.text()[-300:]


# ===============================================================================
# A full reload, which is the one that can lose the console itself
# ===============================================================================


@surfaces("web:admin", "telnet:rw")
def test_cfg14_the_page_reports_what_a_reload_changed_and_asks_before_a_full_one(servers, api_for_server, console, clients):
    """The Reload view reports the summary it was given, and asks first before a full reload.

    The soft button leaves the reconcile result on the page. The full button asks with the words that say connections
    are interrupted, and refusing that question changes nothing: the attached console is still attached.
    """
    world = servers(name="reloadview", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    attached = clients.telnet(user="rw", surface="telnet:rw", require_auth=True, server=world)
    attached.choose_port("keep")
    api = api_for_server(world)
    viewer = _open_editor(console.user("web:admin", world, user="admin"), "?view=reload")
    step("the soft button leaves the summary it was given")
    _click_id(viewer, "softReloadBtn")
    viewer.wait_for_selector("#reloadStatus:not(:empty)", state="visible", timeout=15000)
    reported = _reload_status(viewer)
    assert "adapters" in reported and "loopback" in reported, reported[:400]
    assert "unchanged" in reported, reported[:400]
    step("the full button asks first, in the words that name the cost")
    seen = viewer.accept_dialogs(accept=False)
    _click_id(viewer, "fullReloadBtn")
    asked = viewer.wait_for_dialogs(seen, 1)
    assert asked[-1] == FULL_RELOAD_CONFIRM, asked
    step("refusing it changes nothing, and the console is still attached")
    assert "STILL" in attached.wait_for_echo("STILL"), attached.text()[-300:]
    assert "keep" in _running(api), api.port_names()


@surfaces("http", "web:admin")
def test_cfg15_a_full_reload_restarts_the_adapters_and_the_web_console_answers_again(servers, api_for_server, console):
    """A full reload replaces every adapter and brings the web console back, even while a page is open.

    The console cannot stop itself inside its own reply, so its restart waits until after the reply. A browser page
    holds two sockets open, and the old console must close them before it gives up the port: while it waits for the
    handlers instead, nothing answers on that port again. The session lives in the console that stopped, so the open
    page has to sign in again afterwards, which this scenario reads as the pair of answers.
    """
    world = servers(name="full", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    api = api_for_server(world)
    viewer = _open_editor(console.user("web:admin", world, user="admin"), "?view=ports")
    step("a full reload while that page is open")
    summary = _summary(_reload(api, "full"))
    assert summary["errors"] == [], summary
    assert summary["web_console_restart_deferred"] is True, summary
    stopped = {entry["name"] for entry in summary["stopped_adapters"]}
    assert {"loopback_ports", "telnet_listener"} <= stopped, summary["stopped_adapters"]
    created = [entry["name"] for entry in summary["created_adapters"]]
    assert "web_console" in created and "loopback_ports" in created, created
    assert "loopback_ports" in [entry["name"] for entry in summary["started_adapters"]], summary["started_adapters"]
    step("the deferred restart finishes and the port answers again")
    world.wait_for_log("[deferred] Stopped WebConsole", timeout=20.0)
    world.wait_for_log("[deferred] Started WebConsole", timeout=20.0)
    world.wait_ready(timeout=20.0)
    answered = _health_answers(world)
    assert answered == 200, f"the restarted console refused its health route with {answered}"
    step("and the page that was open has to sign in again, because its session died with the old console")
    signed_out = viewer.page.evaluate("async () => (await fetch('/api/status')).status")
    assert signed_out == 401, f"the old page kept a session it should have lost: {signed_out}"
    step("and the rebuilt adapter serves the port again")
    assert _running(api) == {"keep"}, api.port_names()


@surfaces("telnet:rw", "telnet:fresh", "http")
def test_cfg16_a_full_reload_drops_an_attached_console_and_a_new_one_gets_in(servers, api_for_server, clients):
    """A full reload ends every session, on purpose, and a client that comes back afterwards is served.

    An old session is not moved to the new adapter: it is closed, and says nothing on the way out. What matters is
    that the listener takes the next client on the same port.
    """
    world = servers(name="fulldrop", loopback=("keep",), telnet_kwargs={"require_auth": True}, start=True)
    api = api_for_server(world)
    attached = clients.telnet(user="rw", surface="telnet:rw", require_auth=True, server=world)
    attached.choose_port("keep")
    assert "BEFORE" in attached.wait_for_echo("BEFORE"), attached.text()[-300:]
    step("the full reload ends it")
    _summary(_reload(api, "full"))
    assert attached.wait_for_close(timeout=15.0), "a full reload left a session attached"
    world.wait_ready(timeout=20.0)
    step("a client that comes back is served by the new adapter")
    fresh = clients.telnet(user="rw", surface="telnet:fresh", require_auth=True, server=world)
    fresh.choose_port("keep")
    assert "AGAIN" in fresh.wait_for_echo("AGAIN"), fresh.text()[-300:]
    assert _running(api) == {"keep"}, api.port_names()


# ===============================================================================
# An adapter that comes up with nothing behind it
# ===============================================================================


@surfaces("http", "web:ro", "web:admin")
def test_cfg17_a_port_whose_device_is_missing_is_listed_as_offline_with_the_reason(servers, api_for_server, console):
    """A Serial port with no device behind it is listed, reported in the log, and still takes a console.

    The reload counts the port as it configured it, because the adapter did start: the device is what is missing, and
    the server names the path once in its log. The console page attaches read-only and reports the port as down rather
    than refusing the visitor, which is why an offline port never blocks the port list.
    """
    world = servers(
        name="offline",
        loopback=(),
        telnet_kwargs={"require_auth": True},
        configure=lambda builder: _with_absent_serial(builder, "dead1"),
        start=True,
    )
    api = api_for_server(world)
    assert ABSENT_TTY in world.config_text(), f"the world never wrote the Serial port: {world.config_text()[:600]}"
    step("the reload that starts it reports no failure")
    summary = _summary(_reload(api, "soft"))
    assert summary.get("errors") in ([], None), summary
    serial = _reconciled(summary, "serial")
    assert "dead1" in serial["unchanged"] + serial["added"], serial
    assert serial["removed"] == [], serial
    step("the port is listed and the server says why it is not usable")
    assert "dead1" in api.port_names(), f"{api.status()}\nserver said: {world.tail(40)}"
    assert any(ABSENT_TTY in line and NO_SUCH_DEVICE in line for line in world.log_lines()), world.tail(120)
    step("its console page attaches, and knows the device is down")
    viewer = console.console("web:ro", world, "dead1", user="ro")
    view = viewer.view()
    assert view["connected"] is True, view
    assert view["port_up"] is False, view
    assert viewer.mode() == "read-only", view
    editor = _open_editor(console.user("web:admin", world, user="admin"))
    assert "dead1" in _ports_view_text(editor), _ports_view_text(editor)[:400]
