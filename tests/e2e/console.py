"""Browser side: a signed-in console page, read through one small gated hook.

Playwright cannot read the xterm buffer, because [`console.js`](../../openmux/server/webui/static/js/console.js)
keeps the terminal in a module-scoped `term`, and the ``.xterm-rows`` DOM shows the visible viewport
only. Phase 0 adds a read-only `window.__omxE2E.view()` hook to that file, gated on ``?e2e=1``, which
returns one point-in-time picture of the page. The gate matters: a production page must not expose it.

Playwright is imported lazily. A machine without the package or its browser can still collect this
directory, and the skip says what to install.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from .harness import TEST_PASSWORD
from .story import Story

#: Reason this run cannot drive a browser, or None when Playwright and Chromium are usable.
MISSING_PLAYWRIGHT: Optional[str] = None
try:  # pragma: no cover - depends on the machine, not on the code
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright  # type: ignore
except ImportError as exc:  # pragma: no cover
    PlaywrightError = Exception  # type: ignore[assignment,misc]
    sync_playwright = None  # type: ignore[assignment]
    MISSING_PLAYWRIGHT = (
        f"playwright is not installed ({exc}). Run: .venv/bin/pip install playwright && .venv/bin/playwright install chromium"
    )


_BROWSER_PROBE: Optional[str] = None


def browser_available() -> Optional[str]:
    """None when a browser can run, else the reason it cannot. Used to skip, never to pass silently.

    The launch is probed once per process and remembered, because each probe costs about a second and
    both collection and the fixtures ask.
    """
    global _BROWSER_PROBE
    if sync_playwright is None:
        return MISSING_PLAYWRIGHT or "playwright is not installed"
    if _BROWSER_PROBE is not None:
        return _BROWSER_PROBE
    try:
        with sync_playwright() as driver:
            driver.chromium.launch(timeout=20000).close()
        _BROWSER_PROBE = ""
        return None
    except Exception as exc:  # justification: any launch failure is a skip, reported with the browser's own message
        text = str(exc)
        hint = " Run: .venv/bin/playwright install chromium" if "doesn't exist" in text or "download" in text else ""
        _BROWSER_PROBE = f"Chromium cannot start: {text}{hint}"
        return _BROWSER_PROBE


class Browser:
    """One Chromium instance for the session, with tracing options decided per context.

    One browser process serves every context in a suite, because a launch costs about a second and a
    suite has many pages. Each user gets their own context so cookies never leak between surfaces.
    """

    def __init__(self, headless: bool = True, slow_mo_ms: int = 0):
        if sync_playwright is None:
            raise RuntimeError(MISSING_PLAYWRIGHT or "playwright is not installed")
        self._driver = sync_playwright().start()
        self._browser = self._driver.chromium.launch(headless=headless, slow_mo=slow_mo_ms)

    def context(self, tracing: bool = False, screenshots: bool = False) -> Any:
        """Create one browser context, optionally with a trace buffered from the start.

        Tracing stays buffered until the test stops it, so a green run discards it and pays almost
        nothing.
        """
        context = self._browser.new_context(viewport={"width": 1440, "height": 900})
        if tracing:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        return context

    def close(self) -> None:
        """Close the browser and the driver, so no Chromium process survives the session."""
        try:
            self._browser.close()
        finally:
            self._driver.stop()


def login(context: Any, base_url: str, username: str, password: str = TEST_PASSWORD, next_url: Optional[str] = None) -> Any:
    """Sign in through the real ``POST /login`` form and wait for the session cookie.

    Goes through the form rather than injecting a cookie, so a change to the login page fails a test
    instead of being hidden by a shortcut.

    Args:
        context: A browser context, one per user, so cookies stay separate.
        base_url: Server root, for example ``http://127.0.0.1:53110``.
        username: Configured user.
        password: Test password, which is synthetic and printed in logs on purpose.
        next_url: Optional ``next`` query value, to prove the redirect is honoured.

    Raises:
        AssertionError: when the form or the session cookie is missing.
    """
    page = context.new_page()
    target = "/login" + (f"?next={quote(next_url, safe='')}" if next_url else "")
    page.goto(f"{base_url}{target}", wait_until="domcontentloaded")
    # The login form has no element ids, so name attributes are the selector here and nowhere else:
    # a login page field is a stable contract, while a console status region is replaced by innerHTML.
    page.fill("input[name='username']", username)
    page.fill("input[name='password']", password)
    page.click("form button[type=submit]")
    # The session cookie lands with the redirect that follows the POST, so wait for the redirect
    # instead of reading cookies the instant the click returns.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "omx_session" in {str(cookie["name"]) for cookie in context.cookies()}:
            break
        time.sleep(0.05)
    cookies = {cookie["name"] for cookie in context.cookies()}
    if "omx_session" not in cookies:
        said = page.text_content("body") or ""
        raise AssertionError(f"login as {username!r} set no omx_session cookie; page says: {said[:400]!r}")
    return page


class ConsoleSession:
    """One open web console page for one user and one port.

    Every read goes through ``view()``, which is one JSON picture of the page: mode, terminal text,
    banner, power badge, viewer count, notices, and recent control frames. Reading whole state avoids
    stale element handles, because the console replaces status regions with ``innerHTML`` on each
    nudge.
    """

    def __init__(self, page: Any, base_url: str, surface: str, story: Optional[Story] = None):
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.surface = surface
        self.story = story
        self.port_name: Optional[str] = None

    # --- navigation ---------------------------------------------------------------------

    def open(self, port: Optional[str] = None, e2e: bool = True) -> "ConsoleSession":
        """Open the console page, with the test hook on, and wait for the hook to answer.

        Raises:
            AssertionError: when the hook is missing, which means either the URL lost ``e2e=1`` or
                ``console.js`` failed to parse. A JS syntax error kills every console behaviour with
                no build error (see AGENTS.md), so this must read as itself.
        """
        query = []
        if port:
            query.append(f"port={port}")
        if e2e:
            query.append("e2e=1")
        url = f"{self.base_url}/console" + ("?" + "&".join(query) if query else "")
        self.action(f"goto {url}")
        self.page.goto(url, wait_until="domcontentloaded")
        self.port_name = port
        try:
            self.page.wait_for_function("() => !!(window.__omxE2E && window.__omxE2E.view)", timeout=10000)
        except PlaywrightError:
            raise AssertionError(
                "window.__omxE2E is missing: is '&e2e=1' in the URL, and did console.js parse? " f"URL was {url}"
            ) from None
        return self

    def goto(self, path: str) -> Any:
        """Navigate this page to one server path, and record it."""
        self.action(f"goto {path}")
        self.page.goto(f"{self.base_url}{path}", wait_until="domcontentloaded")
        return self.page

    # --- actions ------------------------------------------------------------------------

    def click(self, selector: str) -> None:
        """Click one element by id, and record the action before it happens.

        Selectors here are ids on purpose: ``querySelector`` returns the first match, and a class
        shared with another control makes a click land on the wrong node with no error at all.
        """
        self.action(f"click {selector}")
        self.page.click(selector)

    def fill(self, selector: str, value: str) -> None:
        """Type into one field."""
        self.action(f"fill {selector}={value!r}")
        self.page.fill(selector, value)

    def type(self, text: str) -> None:
        """Type into the terminal, which is what a user does after clicking it."""
        self.action(f"type {text!r}")
        self.page.locator(".xterm-helper-textarea").first.type(text, delay=0)

    def click_terminal(self) -> None:
        """Focus the terminal pane, so the next key reaches the port and not a menu."""
        self.action("click terminal")
        self.page.click("#term")

    def press(self, key: str) -> None:
        """Press one key on the focused element, for a shortcut such as ``Control+e``."""
        self.action(f"press {key}")
        self.page.keyboard.press(key)

    def send_line(self, text: str) -> None:
        """Focus the terminal, type text, and press Enter.

        Enter is a separate key press on purpose: typing a newline into the hidden xterm textarea
        inserts a line break there instead of sending a carriage return to the port.
        """
        self.click_terminal()
        self.type(text)
        self.press("Enter")

    def connect(self) -> None:
        """Press the console's Connect button."""
        self.click("#connect")

    def set_offline(self, offline: bool) -> None:
        """Cut or restore this page's network, to drive the live-data badge."""
        self.action(f"set_offline {offline}")
        self.page.context.set_offline(offline)

    def reload(self) -> None:
        """Reload the page, keeping the same context and cookies."""
        self.action("reload")
        self.page.reload(wait_until="domcontentloaded")
        self.page.wait_for_function("() => !!(window.__omxE2E && window.__omxE2E.view)", timeout=10000)

    def accept_dialogs(self, accept: bool = True) -> List[str]:
        """Answer every confirm dialog, and keep each message the page showed.

        The power page asks a pre-flight question per switch, and the wording names the consoles that
        lose power, which is worth asserting on.
        """
        seen: List[str] = []

        def handler(dialog: Any) -> None:
            seen.append(dialog.message)
            if accept:
                dialog.accept()
            else:
                dialog.dismiss()

        self.page.on("dialog", handler)
        return seen

    def wait_for_dialogs(self, seen: List[str], count: int, timeout: float = 10.0) -> List[str]:
        """Wait until ``count`` dialogs have been answered, and return every message shown.

        A dialog is modal, and the sync Playwright API delivers it only while a call is in flight. Polling
        the list in plain Python would therefore never see one, so each turn of this loop makes one cheap
        call, which is what lets the handler registered by :meth:`accept_dialogs` run.

        Args:
            seen: The list :meth:`accept_dialogs` returned for this page.
            count: How many dialogs the action is expected to raise.
            timeout: Seconds before this fails.
        """
        self.action(f"wait_for_dialogs {count}")
        deadline = time.monotonic() + timeout
        while len(seen) < count:
            if time.monotonic() > deadline:
                raise AssertionError(f"{self.surface}: expected {count} dialog(s), saw {seen}")
            self.page.evaluate("() => 1")
            time.sleep(0.05)
        return list(seen)

    # --- state --------------------------------------------------------------------------

    def view(self) -> Dict[str, Any]:
        """The whole page state, as one point-in-time picture."""
        payload = self.page.evaluate("() => (window.__omxE2E && window.__omxE2E.view) ? window.__omxE2E.view() : null")
        if payload is None:
            raise AssertionError(f"{self.surface}: window.__omxE2E.view() is gone — did the page navigate away?")
        if not isinstance(payload, dict):
            raise AssertionError(f"{self.surface}: unexpected hook payload {type(payload).__name__}")
        return payload

    def snapshot(self, reason: str = "") -> Dict[str, Any]:
        """Record what this page shows right now, and return it."""
        payload = self.view()
        if self.story is not None:
            summary = " ".join(
                f"{key}={payload.get(key)}"
                for key in ("mode", "connected", "badge", "viewers")
                if payload.get(key) is not None
            )
            banner = (payload.get("banner") or {}).get("text")
            if banner:
                summary += f" banner={banner!r}"
            if reason:
                summary = f"({reason}) " + summary
            self.story.snapshot(self.surface, summary, payload)
        return payload

    def text(self) -> str:
        """Everything in the terminal's scrollback, newest line last."""
        return "\n".join(str(line) for line in self.lines())

    def lines(self) -> List[str]:
        """The terminal's scrollback lines, with trailing spaces stripped."""
        return [str(line) for line in self.view().get("lines") or []]

    def text_of(self, selector: str) -> str:
        """The text one element holds, with surrounding space removed, as a person reads it.

        Use this for a label that is asserted once. For anything the page redraws, read ``view()``
        instead: those regions are replaced whole, so a handle taken before the redraw is stale.
        """
        return str(self.page.text_content(selector) or "").strip()

    def page_text(self, selector: str = "body") -> str:
        """The text one page shows, for a page with no ``?e2e=1`` hook: a PDU page, the login page.

        The hook belongs to the console page, so a server-rendered page is read as written text here.
        """
        return str(self.page.inner_text(selector))

    def wait_for_page_text(self, needle: str, selector: str = "body", timeout: float = 10.0) -> str:
        """Wait until one element's text contains ``needle``, on a page with no test hook."""
        self.action(f"wait_for_page_text {needle!r}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.page_text(selector):
                return self.page_text(selector)
            time.sleep(0.05)
        raise AssertionError(
            f"{self.surface}: {selector} never carried {needle!r} in {timeout}s: {self.page_text(selector)[-600:]!r}"
        )

    def wait_for_selector(self, selector: str, *, state: str = "visible", timeout: float = 8000) -> None:
        """Wait for one element with Playwright's own wait, for a page that has no ``?e2e=1`` hook.

        The Config Editor page carries no hook, so ``wait_until()`` cannot observe it and
        ``view()`` raises. Its regions appear as its script fills them, which is what this waits for.
        """
        self.action(f"wait_for_selector {selector} state={state}")
        self.page.wait_for_selector(selector, state=state, timeout=timeout)

    def wait_for_page_text_to_clear(self, needle: str, selector: str = "body", timeout: float = 10.0) -> None:
        """Wait until one element no longer shows ``needle``, on a page with no test hook."""
        self.action(f"wait_for_page_text_to_clear {needle!r}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle not in self.page_text(selector):
                return
            time.sleep(0.05)
        raise AssertionError(f"{self.surface}: {selector} still carried {needle!r} after {timeout}s")

    def table_rows(self, selector: str) -> List[List[str]]:
        """Each row of one table as its cell texts, for a table the server rendered."""
        raw = self.page.evaluate(
            """(selector) => Array.from(document.querySelectorAll(selector + ' tr')).map(
                   (tr) => Array.from(tr.querySelectorAll('td,th'))
                     .map((cell) => (cell.textContent || '').replace(/\\s+/g, ' ').trim()))""",
            selector,
        )
        return [[str(cell) for cell in row] for row in raw or []]

    def rows_referring(self, selector: str, href: str) -> List[List[str]]:
        """The rows of one table that link to ``href``, which is how a PDU row is named by its target."""
        raw = self.page.evaluate(
            """([selector, href]) => Array.from(document.querySelectorAll(selector + ' tr'))
                 .filter((tr) => Array.from(tr.querySelectorAll('a')).some((a) => a.getAttribute('href') === href))
                 .map((tr) => Array.from(tr.querySelectorAll('td,th'))
                   .map((cell) => (cell.textContent || '').replace(/\\s+/g, ' ').trim()))""",
            [selector, href],
        )
        return [[str(cell) for cell in row] for row in raw or []]

    def sidebar_width(self) -> float:
        """The rendered width of the sidebar in CSS pixels, which is 0 when the page hides it.

        The width is what a person sees. An embedded page keeps its navigation in the document and
        collapses it to nothing, so a check on the box is the honest check.
        """
        return float(self.page.evaluate("() => document.getElementById('sidebar').getBoundingClientRect().width"))

    def wait_until(self, condition: Callable[[], bool], timeout: float = 10.0, what: str = "the condition") -> None:
        """Wait for a Python-side condition, and name it when it does not happen.

        A hook read costs one round trip, so a condition over Python state - the list of dialog
        messages a page raised, for example - cannot go through ``wait_for_view``.
        """
        self.action(f"wait_until {what}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        raise AssertionError(f"{self.surface}: {what} never happened within {timeout}s")

    def nav_links(self) -> List[Dict[str, Optional[str]]]:
        """The sidebar links the server rendered, as ``{label, href}``, icon glyphs left out.

        The sidebar is server-rendered per request, not replaced by ``innerHTML``, so reading it from
        the DOM is honest here. Permission tests need it because ``view()`` reports one page's session
        state, and the nav is what a person uses to find out what they may open.
        """
        raw = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('a.nav-item')).map((a) => ({
                   label: Array.from(a.childNodes)
                     .filter((n) => n.nodeType === Node.TEXT_NODE)
                     .map((n) => n.textContent.trim())
                     .join(' ')
                     .replace(/\\s+/g, ' '),
                   href: a.getAttribute('href'),
                 }))"""
        )
        links: List[Dict[str, Optional[str]]] = []
        for entry in raw or []:
            label = str(entry.get("label") or "").strip()
            if label:
                links.append({"label": label, "href": entry.get("href")})
        return links

    def nav_labels(self) -> List[str]:
        """Just the labels of :meth:`nav_links`, for an assertion that reads as a list of names."""
        return [str(link["label"]) for link in self.nav_links()]

    def mode(self) -> Optional[str]:
        """The access mode this page believes it holds: ``read-write`` or ``read-only``."""
        value = self.view().get("mode")
        return str(value) if value is not None else None

    def frame_types(self) -> List[str]:
        """The control frames this page received, newest last, as ``type:code``."""
        frames = self.view().get("frames") or []
        out: List[str] = []
        for frame in frames:
            if isinstance(frame, dict):
                out.append(f"{frame.get('type')}:{frame.get('code')}" if frame.get("code") else str(frame.get("type")))
        return out

    def frames(self) -> List[Dict[str, Any]]:
        """The decoded control frames this page received."""
        return [dict(frame) for frame in self.view().get("frames") or [] if isinstance(frame, dict)]

    def power_badge(self) -> Dict[str, Any]:
        """The power badge: its tint (``all``, ``some``, ``none``, ``unknown``, ``hidden``) and its label."""
        return {"state": str(self.view().get("badge_state") or "hidden"), "label": str(self.view().get("badge") or "")}

    def power_badge_title(self) -> str:
        """The tooltip text of the power badge, which is where the page writes ALL POWER LOST."""
        return str(self.page.get_attribute("#powerBadge", "title") or "")

    def open_power_menu(self) -> None:
        """Click the power badge so its per-feed menu is on screen, and wait for it."""
        self.click("#powerBadge")
        self.page.wait_for_selector("#powerMenu:visible", timeout=10000)

    def power_menu_items(self) -> List[Dict[str, Any]]:
        """One entry per feed row of the badge menu: the row text and the button it carries.

        The button label is what an operator reads ("Turn off rack1.1"), and it is absent for a page
        that is not read-write, so returning it or ``None`` proves the permission.
        """
        raw = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#powerMenuList > div')).map((row) => {
                   const button = row.querySelector('button');
                   const text = (row.textContent || '').replace(/\\s+/g, ' ').trim();
                   const label = button ? (button.textContent || '').trim() : null;
                   return {feed: label ? text.slice(0, text.indexOf(label)).trim() : text, button: label};
                 })"""
        )
        return [{"feed": str(row.get("feed") or ""), "button": row.get("button")} for row in raw or []]

    def click_power_toggle(self, ref: str, off: bool = True) -> None:
        """Click the badge menu's button for one feed, by the label a person reads.

        The menu closes on any outside click, so the badge must be open: call :meth:`open_power_menu`
        first.
        """
        label = f"Turn {'off' if off else 'on'} {ref}"
        self.action(f"click power menu {label!r}")
        self.page.locator("#powerMenuList").get_by_role("button", name=label, exact=True).first.click()

    def power_nav_pdus(self) -> List[str]:
        """The PDU names the sidebar lists under Power, in the order it shows them.

        The page rebuilds this list from ``/api/power`` when a ``pdus_changed`` notice arrives, so it is what proves
        a PDU that appeared later reached an open page.
        """
        raw = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#power-pdus a.nav-sub-item[data-pdu-name]'))
                   .map((a) => (a.getAttribute('data-pdu-name') || '').trim())"""
        )
        return [str(name) for name in raw or [] if str(name)]

    def outlet_rows(self) -> List[Dict[str, Any]]:
        """The rows of one PDU page: the ref, the row text as written, and the toggle label if any.

        A row is found by its ``data-ref``, which the server and the page both stamp, so a click can
        never land on a neighbouring outlet.
        """
        raw = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('#outlet-rows tr[data-ref]')).map((tr) => {
                   const button = tr.querySelector('button[data-toggle]');
                   return {
                     ref: tr.getAttribute('data-ref'),
                     text: (tr.textContent || '').replace(/\\s+/g, ' ').trim(),
                     button: button ? (button.textContent || '').trim() : null,
                   };
                 })"""
        )
        return [
            {"ref": str(row.get("ref") or ""), "text": str(row.get("text") or ""), "button": row.get("button")}
            for row in raw or []
        ]

    def click_outlet_toggle(self, ref: str) -> None:
        """Click the Turn off / Turn on button of one outlet row on a PDU page."""
        row = next((entry for entry in self.outlet_rows() if entry["ref"] == ref), None)
        if row is None or not row["button"]:
            raise AssertionError(f"{self.surface}: outlet row for {ref!r} carries no toggle: {self.outlet_rows()}")
        self.action(f"click {ref} {row['button']!r}")
        self.page.click(f'#outlet-rows tr[data-ref="{ref}"] button[data-toggle="{ref}"]')

    def metric_cards(self) -> List[Dict[str, str]]:
        """The metric cards of a page as ``{label, value}`` pairs, in the order shown."""
        raw = self.page.evaluate(
            """() => Array.from(document.querySelectorAll('.card')).map((card) => ({
                   label: (card.querySelector('.muted') || {}).textContent || '',
                   value: (card.querySelector('.metric') || {}).textContent || '',
                 }))"""
        )
        return [
            {"label": str(entry.get("label") or "").strip(), "value": str(entry.get("value") or "").strip()}
            for entry in raw or []
        ]

    def banner(self) -> Dict[str, Any]:
        """The banner region: its text and its class, or an empty dict when hidden."""
        banner = self.view().get("banner") or {}
        return dict(banner) if isinstance(banner, dict) else {}

    def body_classes(self) -> List[str]:
        """The classes on ``document.body``, which carry the mode and port-down state."""
        return str(self.view().get("body_class") or "").split()

    def menu_items(self, menu: str = "ro") -> List[Dict[str, Any]]:
        """The visible items of one menu (``ro``, ``ctrl``, ``viewers``), each with ``label`` and ``disabled``.

        A greyed-out button proves a permission as strongly as a missing one, so the disabled flag
        stays visible instead of being filtered out. The viewer list is plain text rows, so those
        entries come back with ``disabled`` false.
        """
        menus = self.view().get("menus") or {}
        items = menus.get(menu) or []
        return [dict(item) if isinstance(item, dict) else {"label": str(item), "disabled": False} for item in items]

    def menu_text(self, menu: str = "ro") -> List[str]:
        """The visible labels of one menu only, for a test that ignores whether an item is disabled."""
        return [str(item.get("label") or "") for item in self.menu_items(menu)]

    def wait_for_view(self, predicate: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Wait until a JS predicate over ``view()`` becomes true, and return the view.

        The predicate gets the same object ``view()`` returns, so a test states the condition it
        cares about instead of sleeping.
        """
        self.action(f"wait_for_view {predicate}")
        try:
            self.page.wait_for_function(
                "(predicate) => { const view = window.__omxE2E && window.__omxE2E.view ? window.__omxE2E.view() : null; if (!view) return false; return eval('(' + predicate + ')')(view); }",
                arg=predicate,
                timeout=int(timeout * 1000),
            )
        except PlaywrightError:
            snapshot = json.dumps(self.view(), default=str)[:1500]
            raise AssertionError(f"{self.surface}: view never matched {predicate!r} in {timeout}s; view={snapshot}") from None
        return self.view()

    # --- assertions ---------------------------------------------------------------------

    def wait_for_text(self, needle: str, timeout: float = 10.0) -> str:
        """Wait for ``needle`` to appear anywhere in the terminal scrollback."""
        self.wait_for_view(f"(v) => (v.lines || []).join('\\n').includes({json.dumps(needle)})", timeout=timeout)
        return self.text()

    def expect_notice(self, needle: str, timeout: float = 10.0) -> str:
        """Prove the page showed the server's own sentence, in the terminal or in the banner.

        Asserting on the server's wording, never on a locally invented sentence, is the point of
        ADR-0001: a console renders what the Access decision carries.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.view()
            haystack = "\n".join(
                [
                    *(str(line) for line in view.get("lines") or []),
                    str((view.get("banner") or {}).get("text") or ""),
                    *(str(item) for item in view.get("notices") or []),
                ]
            )
            if needle in haystack:
                return needle
            time.sleep(0.05)
        raise AssertionError(
            f"{self.surface}: notice {needle!r} never appeared. terminal={self.text()[-600:]!r} banner={self.banner()!r}"
        )

    def expect_no_text(self, needle: str, for_seconds: float = 2.0) -> None:
        """Prove one sentence does NOT appear while watching for a moment.

        Used where the product must stay silent, for example an attach grant on a terminal that
        already printed its connect line.
        """
        deadline = time.monotonic() + for_seconds
        while time.monotonic() < deadline:
            if needle in self.text():
                raise AssertionError(f"{self.surface}: {needle!r} appeared, and the page should stay silent about it")
            time.sleep(0.05)

    def expect_banner(self, needle: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Wait for the banner to carry ``needle``, and return the banner.

        The ws-down banner debounces for 1000 ms, so a fixed sleep both flakes and misses it.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            banner = self.banner()
            if needle in str(banner.get("text") or ""):
                return banner
            time.sleep(0.05)
        raise AssertionError(
            f"{self.surface}: banner never carried {needle!r}; banner={self.banner()!r} body={self.body_classes()}"
        )

    def expect_absent(self, selector: str) -> None:
        """Prove one control is not on the page, which is how a permission test reads."""
        if self.page.locator(selector).count():
            raise AssertionError(f"{self.surface}: {selector} is present, and this permission must not see it")

    def expect_absent_for(self, selector: str, for_seconds: float = 2.0) -> None:
        """Prove one control stays off the page for a stated moment.

        Absence needs a window, so a dismissed confirm or a blocked switch is proven this way: the
        control that would appear once the change landed never appears.
        """
        self.action(f"expect_absent_for {selector} for {for_seconds}s")
        deadline = time.monotonic() + for_seconds
        while time.monotonic() < deadline:
            if self.page.locator(selector).count():
                raise AssertionError(f"{self.surface}: {selector} appeared, and this test expected the change to stay away")
            time.sleep(0.05)

    def screenshot(self, path: Path) -> None:
        """Write a PNG of the viewport into the artifact bundle."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path))

    def pause(self) -> None:
        """Open Playwright Inspector on the live page, for a human to drive it."""
        self.page.pause()

    # --- teardown -----------------------------------------------------------------------

    def close(self) -> None:
        """Close this page only. The context and its cookies belong to the fixture."""
        try:
            self.page.close()
        except PlaywrightError:
            pass  # justification: the browser may already be gone after a crash

    def action(self, note: str) -> None:
        """Record one action on the timeline before performing it, so cause precedes effect."""
        if self.story is not None:
            self.story.timeline.record(self.surface, "act", note)


def expect_notice(page: ConsoleSession, needle: str, timeout: float = 10.0) -> str:
    """Backward-friendly helper form used in a test body: ``expect_notice(web_rw, \"[Taken from:\")``."""
    return page.expect_notice(needle, timeout=timeout)


def expect_banner(page: ConsoleSession, needle: str, timeout: float = 10.0) -> Dict[str, Any]:
    """Backward-friendly helper form: ``expect_banner(web_rw, \"Port down\")``."""
    return page.expect_banner(needle, timeout=timeout)
