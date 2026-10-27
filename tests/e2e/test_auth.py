"""AUTH: who gets in, what they are handed, and what a refusal looks like (AUTH-01 to AUTH-10).

These scenarios drive the login form and the routes behind it over HTTP, as a browser does: a real
form post, a real session cookie, redirects read instead of followed, and the refusal a wrong
credential actually produces. The point of that shape is that every assertion is a sentence or a
status a person could observe. An assertion on a private attribute would pass long after the login
page stopped working.

Three facts drive most of the file, and each is verified against the server's own code:

- A failed login is ``200`` with the page's own sentence, not a ``401``. A wrong password and an
  unknown account answer the same way, so the page does not say which part was wrong.
- The CSRF token is the session id, and a request with no session cookie is exempt. So the Basic
  path an operator scripts with needs no token, and the cookie path a browser uses does.
- A session POST that lacks that token is refused before the handler runs, and the refusal is worded
  by the endpoint that owns it.
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any, Dict

import pytest

from .harness import TEST_PASSWORD, BasicSession, HttpSession, sso_v1_header
from .story import step, surfaces

pytestmark = [pytest.mark.e2e]

#: The login page's own refusal, quoted from ``login.html.j2``. A scenario asserts on this text
#: because it is what a person reads, and it is the only signal a failed login gives over HTTP.
INVALID_CREDENTIALS = "Invalid username or password"

#: The lockout refusal, from ``handle_login`` in ``web_console.py``.
TOO_MANY_ATTEMPTS = "Too many failed attempts for this account. Please try again later."

#: The ``server.description`` the harness writes, which the server uses as its Basic realm.
E2E_DESCRIPTION = "OpenMux e2e test server"

#: The Config Editor's answer to a session request with no CSRF token: the HTTP status is ``403`` and
#: the body is this one word. That endpoint refuses before parsing, so its wording differs from the
#: power and port-action endpoints, which answer ``CSRF check failed``.
CSRF_REFUSAL = "CSRF"

#: A Config Editor body that its own schema rejects, because it names no section. A scenario posts
#: this to show the token gate passed: the answer turns from a CSRF refusal into a validation error,
#: and no file is written because there is nothing in the payload to write.
NO_SECTION_PAYLOAD: Dict[str, Any] = {"files": {}}

#: The Config Editor route, which needs the ``admin`` permission and checks the CSRF token first.
APPLY_ROUTE = "/config-editor/apply"


def _config_fingerprint(config_dir: Any) -> Dict[str, str]:
    """A hash of each config file the server reads, so a scenario can prove none of them changed.

    A scenario that proves a refusal must prove the refusal came first. A ``403`` that follows a
    partial write is a worse outcome than the scenario claims, and the status code alone hides it.
    """
    names = ("server.yaml", "authentication.yaml", "security.yaml")
    return {name: hashlib.sha256((config_dir / name).read_bytes()).hexdigest() for name in names}


def _assert_config_unchanged(before: Dict[str, str], config_dir: Any, label: str) -> None:
    """Fail with the changed file named when a config file differs from ``before``."""
    now = _config_fingerprint(config_dir)
    changed = sorted(name for name in now if now[name] != before.get(name))
    assert not changed, f"{label}: {', '.join(changed)} changed"


def _set_cookie_for(session: Any, name: str) -> str:
    """The raw ``Set-Cookie`` header that names ``name``, from the last response.

    Raw, because the attributes are the assertion: ``HttpOnly``, ``SameSite``, ``Path``, and
    ``Max-Age`` decide what a browser may do with the value.
    """
    for raw in session.set_cookies:
        if raw.startswith(f"{name}="):
            return raw
    raise AssertionError(f"no Set-Cookie for {name!r} in {session.set_cookies}")


def _wait_for_signed_in(session: Any, username: str, seconds: float = 20.0) -> bool:
    """Keep offering the correct password until the form accepts it, or the deadline passes.

    A locked account short-circuits before the password is checked, so these attempts neither extend
    the lock nor count as failures. Waiting on the outcome instead of sleeping a fixed time keeps the
    scenario honest about what it is waiting for.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status, _, _ = session.login(username)
        if status == 302:
            return True
        time.sleep(0.2)
    return False


@surfaces("http")
def test_auth01_the_login_form_hands_out_a_session_cookie(web, story):
    """The login form accepts a configured user and issues a session cookie with its attributes."""
    with step("the login page asks for a name and a password, and hands out nothing yet"):
        _, _, body = web.get("/login", expect=(200,))
        assert 'name="username"' in body and 'name="password"' in body, body[:400]
        assert web.cookie is None, web.cookie
        assert not [raw for raw in web.set_cookies if raw.startswith("omx_session=")], web.set_cookies
    with step("the correct password returns a redirect to the start page"):
        _, _, _ = web.login("rw", expect=(302,))
        assert web.location == "/", web.location
    with step("and a session cookie a script cannot read"):
        assert web.cookie, "the login set no session cookie"
        raw = _set_cookie_for(web, "omx_session")
        assert "Path=/" in raw, raw
        assert "HttpOnly" in raw, raw
        assert "SameSite=Lax" in raw, raw
        assert "Max-Age=" in raw, raw
        # The harness runs the console over plain HTTP, so a Secure cookie could never come back.
        assert "Secure" not in raw, raw
    with step("the cookie opens a page the anonymous visitor was turned away from"):
        _, _, body = web.get("/console", expect=(200,))
        assert "Console" in body, body[:400]
    with step("and asking for the login page again bounces out of it"):
        web.get("/login", expect=(302,))
        assert web.location == "/", web.location


@surfaces("http")
def test_auth02_a_wrong_password_is_answered_with_the_pages_own_sentence(web, web_for, server, story):
    """A bad password or an unknown account keeps the visitor signed out, with one shared refusal."""
    with step("a wrong password stays on the login page and sets no cookie"):
        _, _, body = web.login("rw", "not-the-password", expect=(200,))
        assert INVALID_CREDENTIALS in body, body[:400]
        assert web.cookie is None, web.cookie
    with step("an unknown account is refused in the same words, so the page gives no hint away"):
        _, _, body = web.login("no-such-user", TEST_PASSWORD, expect=(200,))
        assert INVALID_CREDENTIALS in body, body[:400]
        assert web.cookie is None, web.cookie
    with step("the API behind it still challenges, and names the server in the challenge"):
        _, _, _ = web.get("/api/ports", expect=(401,))
        assert web.header("WWW-Authenticate") == f'Basic realm="{E2E_DESCRIPTION}"', web.last_headers
    with step("a second visitor who has never signed in is refused the same way"):
        stranger = web_for(server, "http:stranger")
        stranger.get("/api/ports", expect=(401,))
        assert stranger.cookie is None


@surfaces("http")
def test_auth03_too_many_failed_logins_lock_the_account_and_then_release_it(servers, web_for, story):
    """Repeated failures for one account are refused before the password is checked, then released."""
    server = servers(name="lockout", configure=lambda builder: builder.with_login_lockout(300, 3, 5), start=True)
    visitor = web_for(server, "http")
    with step("the first three wrong passwords are answered as wrong passwords"):
        for attempt in range(3):
            _, _, body = visitor.login("rw", "nope", expect=(200,))
            assert INVALID_CREDENTIALS in body, f"attempt {attempt + 1}: {body[:400]}"
    with step("the next attempt is refused even with the correct password"):
        _, _, body = visitor.login("rw", TEST_PASSWORD, expect=(200,))
        assert TOO_MANY_ATTEMPTS in body, body[:400]
        assert INVALID_CREDENTIALS not in body, body[:400]
        assert visitor.cookie is None, visitor.cookie
    with step("another account signs in while that one is locked"):
        # The counter is keyed per account and address, so mistyping one name cannot lock a colleague
        # out. A counter shared per address, or per server, fails here.
        colleague = web_for(server, "http:colleague")
        _, _, _ = colleague.login("ro", expect=(302,))
        assert colleague.cookie, "the read-only account should have signed straight in"
    with step("that account needs its own failures before the same rule reaches it"):
        mistyper = web_for(server, "http:mistyper")
        for attempt in range(3):
            _, _, body = mistyper.login("ro", "nope", expect=(200,))
            assert INVALID_CREDENTIALS in body, f"attempt {attempt + 1}: {body[:400]}"
        _, _, body = mistyper.login("ro", TEST_PASSWORD, expect=(200,))
        assert TOO_MANY_ATTEMPTS in body, body[:400]
    with step("once the configured lock period passes, the same password is accepted again"):
        # The lock here is 5 s, and the shipped default is 30 s, which no scenario should wait out.
        assert _wait_for_signed_in(visitor, "rw"), "the account was still locked after 20 s"


@surfaces("http")
def test_auth04_the_login_page_returns_the_visitor_to_the_page_they_asked_for(web, story):
    """A redirect to the login page carries the requested page, and the form hands it back."""
    with step("an anonymous request for one port's console is sent to the login page for it"):
        web.get("/console?port=loop1", expect=(302,))
        assert web.location == "/login?next=/console?port%3Dloop1", web.location
    with step("a second query parameter stays inside the target instead of ending it"):
        web.get("/status?sort=name&q=x", expect=(302,))
        assert web.location == "/login?next=/status?sort%3Dname%26q%3Dx", web.location
    with step("the login page holds that target in its hidden field, whole"):
        _, _, body = web.get("/login?next=/console?port=loop1", expect=(200,))
        assert 'value="/console?port=loop1"' in body, body[:600]
    with step("signing in returns the visitor there instead of to the start page"):
        _, _, _ = web.login("ro", next_url="/console?port=loop1", expect=(302,))
        assert web.location == "/console?port=loop1", web.location
    with step("and that page opens for the session that asked for it"):
        _, _, body = web.get("/console?port=loop1", expect=(200,))
        assert "loop1" in body, body[:400]


@surfaces("http")
def test_auth05_logout_clears_the_cookie_and_the_page_asks_to_sign_in_again(web, story):
    """Signing out clears the cookie, and every page behind it asks for a login again."""
    with step("a signed-in session leaves with GET /logout"):
        web.login("rw", expect=(302,))
        assert web.cookie
        _, _, _ = web.logout(expect=(302,))
        assert web.location == "/login", web.location
    with step("the answer clears the cookie it just handed out"):
        raw = _set_cookie_for(web, "omx_session")
        # aiohttp writes the empty value quoted; both forms mean "take this cookie back".
        assert raw.partition(";")[0] in ("omx_session=", 'omx_session=""'), raw
        assert "Max-Age=0" in raw, raw
        assert web.cookie is None, web.cookie
    with step("the console is behind the login page again"):
        web.get("/console", expect=(302,))
        assert web.location == "/login?next=/console", web.location
    with step("the route answers GET only, so a form post is refused outright"):
        web.post_form("/logout", {}, expect=(405,))
    with step("and a visitor who was never signed in is not confused by it"):
        web.logout(expect=(302,))
        assert web.location == "/login", web.location


@surfaces("http")
def test_auth06_a_bad_basic_header_is_refused_even_with_a_valid_cookie(web, web_for, server, story):
    """Basic credentials and a session cookie are two separate decisions, not a combined one."""
    with step("a session cookie alone answers the API"):
        web.login("rw", expect=(302,))
        _, _, _ = web.get("/api/ports", expect=(200,))
    with step("the same call carrying wrong Basic credentials is refused, cookie or not"):
        bad = "Basic " + base64.b64encode(b"not-a-user:pass").decode("ascii")
        _, _, _ = web.get("/api/ports", headers={"Authorization": bad}, expect=(401,))
        assert web.header("WWW-Authenticate"), web.last_headers
    with step("a correct Basic pair is accepted on its own, with no cookie at all"):
        api_only = web_for(server, "http:basic")
        token = base64.b64encode(f"rw:{TEST_PASSWORD}".encode("utf-8")).decode("ascii")
        _, _, _ = api_only.get("/api/ports", headers={"Authorization": f"Basic {token}"}, expect=(200,))
        assert api_only.cookie is None, api_only.cookie


@surfaces("http")
def test_auth07_the_config_editor_answers_per_permission_level(server, web, web_for):
    """The Config Editor belongs to administrators: an administrator opens it and every other level is told ``403``."""
    # The gate is an exact match on the permission, so ``admin`` opens the page and a ``read-write``
    # user does not, even though that level writes elsewhere in the console. A visitor with no
    # session at all is sent to the login page instead, which AUTH-04 covers.
    with step("a signed-in administrator opens the editor page"):
        _, _, _ = web.login("admin")
        assert web.cookie, "the scenario needs a signed-in administrator"
        status, _, body = web.get("/config-editor")
        assert status == 200, (status, body[:120])
        assert "<html" in body.lower(), body[:120]

    for username in ("rw", "ro"):
        visitor = web_for(server, f"http:{username}")
        with step(f"a signed-in {username} user is refused with the status, not the login page"):
            _, _, _ = visitor.login(username)
            assert visitor.cookie, "the scenario needs a signed-in session to prove the level gate"
            status, _, body = visitor.get("/config-editor")
            assert status == 403, (status, body[:120])
            assert "403: Forbidden" in body, body[:120]
            assert "Sign in" not in body, "a signed-in user must not be handed the login page"

    with step("a scripted request with credentials is refused the same way"):
        status, _, body = BasicSession(HttpSession(server.web_url), "rw").get("/config-editor")
        assert status == 403, (status, body[:120])


@surfaces("http", "config files")
def test_auth08_a_session_post_without_the_csrf_token_is_refused_before_it_writes(server, web):
    """A cookie alone cannot write config: the token check answers first and the config files stay as they were."""
    # The token is the session id, so a page script reads it from ``GET /api/csrf`` and echoes it
    # back in a header. Only a request that also carries that header may write, which is what stops
    # another site's form posting to this server with the visitor's cookie.
    untouched = _config_fingerprint(server.config_dir)
    with step("an administrator signs in, which is the shape the editor's own page uses"):
        _, _, _ = web.login("admin")
        assert web.cookie, "the CSRF rule only applies to a request that carries a session cookie"

    with step("the editor asks for a token and refuses a request that carries only the cookie"):
        status, _, body = web.post_json(APPLY_ROUTE, NO_SECTION_PAYLOAD)
        assert status == 403, (status, body[:120])
        assert body.strip() == CSRF_REFUSAL, body[:120]

    with step("the same session reads its token from the endpoint the page uses"):
        status, _, body = web.get("/api/csrf")
        assert status == 200, (status, body[:120])
        assert web.csrf_token in body, body[:120]

    with step("the same POST with that token in a header passes the token gate"):
        status, _, body = web.post_json(APPLY_ROUTE, NO_SECTION_PAYLOAD, csrf=True)
        assert CSRF_REFUSAL not in body, body[:120]
        assert status == 400, (status, body[:120])
        assert "required property" in body, body[:120]

    with step("credentials sent together with a cookie do not lift the rule"):
        # Exempt means no session cookie, so a request with both credentials and a cookie is still
        # checked: the credentials alone do not make the browser's cookie disappear.
        status, _, body = BasicSession(web, "admin").post_json(APPLY_ROUTE, NO_SECTION_PAYLOAD)
        assert status == 403, (status, body[:120])
        assert body.strip() == CSRF_REFUSAL, body[:120]

    _assert_config_unchanged(untouched, server.config_dir, "auth08: the refused POSTs must not have written config")


@surfaces("http", "config files")
def test_auth09_the_basic_path_needs_no_token_and_never_rises_above_its_permission(server, web_for):
    """Scripting with credentials skips the token check, and still stops at the caller's permission level."""
    # Operators script this server with ``curl -u``, and no browser is involved, so a cross-site page
    # cannot send that header by accident. CSRF is therefore exempt for a request with credentials
    # and no session cookie. The exemption covers the token only: the permission level is checked
    # separately, which is what the ``read-write`` half of this scenario shows.
    untouched = _config_fingerprint(server.config_dir)
    admin = web_for(server, "http:basic").with_basic("admin")
    with step("an administrator over Basic is asked for no token"):
        status, _, body = admin.post_json(APPLY_ROUTE, NO_SECTION_PAYLOAD)
        assert CSRF_REFUSAL not in body, body[:120]
        assert status == 400, (status, body[:120])

    with step("a read-write user over Basic is refused, and the reason is permission"):
        read_write = web_for(server, "http:basic").with_basic("rw")
        status, _, body = read_write.post_json(APPLY_ROUTE, NO_SECTION_PAYLOAD)
        assert status == 403, (status, body[:120])
        assert CSRF_REFUSAL not in body, body[:120]
        assert "403: Forbidden" in body, body[:120]

    _assert_config_unchanged(untouched, server.config_dir, "auth09: neither caller may have written config")


@surfaces("http", "server config")
def test_auth10_only_the_holder_of_the_sso_secret_may_speak_for_a_user(servers, web_for):
    """A trust header signed with ``sso_secret`` is believed, its permission included, and every forgery is refused."""
    # A proxy in front of this server signs the identity it has already checked. The signature is the
    # whole access control on this path: the claims name the user and the permission, so a claim may
    # name somebody who has no entry in ``authentication.yaml``. Anything that fails verification
    # falls through to the normal rules, which is a ``401`` plus ``WWW-Authenticate`` on an API path.
    secret = "e2e-sso-secret"
    world = servers(name="sso", configure=lambda builder: builder.with_sso(secret=secret), start=True)
    visitor = web_for(world, "http:sso")

    def refused(when: str, header_value: str) -> None:
        with step(f"{when} is refused"):
            status, _, body = visitor.get("/api/ports", headers={"X-OMX-SSO": header_value}, expect=None)
            assert status == 401, f"{when} was believed: {status} {body[:80]}"
            assert visitor.header("WWW-Authenticate"), visitor.last_headers

    with step("a request with no trust header at all is asked for Basic credentials"):
        status, _, _ = visitor.get("/api/ports", expect=(401,))
        assert status == 401
        assert visitor.header("WWW-Authenticate"), visitor.last_headers

    with step("a claim signed with the right secret opens an administrator page for a read-only user"):
        # The same user is refused this page with their own credentials, which AUTH-07 shows on this
        # same route. Here only the header differs, so the difference is the claim's doing.
        claim = sso_v1_header(secret, user="ro", perm="admin")
        status, _, body = visitor.get("/config-editor", headers={"X-OMX-SSO": claim}, expect=(200,))
        assert status == 200, (status, body[:120])

    refused("a signature that does not match the claims", sso_v1_header(secret, user="ro", perm="admin", tamper=True))
    refused("a claim signed with another secret", sso_v1_header("another-secret", user="ro", perm="admin"))
    refused("a claim whose expiry passed", sso_v1_header(secret, user="ro", perm="admin", expires_at=int(time.time()) - 300))
    refused("a claim dated in the future", sso_v1_header(secret, user="ro", perm="admin", issued_at=int(time.time()) + 600))

    with step("the claim's permission is the one that is honoured, not the user's own"):
        read_only_claim = sso_v1_header(secret, user="ro", perm="read-only")
        status, _, _ = visitor.get("/config-editor", headers={"X-OMX-SSO": read_only_claim}, expect=(403,))
        assert status == 403, "a claim of read-only must not open an administrator page"

    with step("the claim names the user, so a name with no entry here is believed too"):
        # The proxy in front decides identity, and a directory user has no entry in
        # ``authentication.yaml``. That is why ``sso_secret`` is the whole access control on this
        # path, and why a leaked secret is an administrator takeover rather than one bad login.
        unlisted = sso_v1_header(secret, user="no-such-user", perm="read-only")
        status, _, body = visitor.get("/api/ports", headers={"X-OMX-SSO": unlisted}, expect=(200,))
        assert status == 200, (status, body[:120])
