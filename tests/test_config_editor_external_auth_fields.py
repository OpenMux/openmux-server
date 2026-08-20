"""Regression tests for the Config Editor's external-auth view.

The editor speaks the ``authentication.external_auth`` config shape (see
AuthManager._load_ext_auth_config). These asset tests guard that the
template and static JS still reference the current field ids and never
regress to the removed ``auth.pam.*`` shape. No JS test runner exists for
the editor, so the check is performed on the shipped assets.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "templates" / "web_console" / "config_editor.html.j2"
JS_PATH = REPO_ROOT / "static" / "js" / "config_editor.js"

EXPECTED_TEMPLATE_IDS = [
    "auth.extauth.enabled",
    "auth.extauth.service",
    "auth.extauth.helper",
    "auth.extauth.timeout",
    "auth.extauth.allow_root",
    "auth.extauth.allowed_users",
    "auth.extauth.groups.admin_group",
    "auth.extauth.groups.write_group",
    "auth.extauth.groups.read_group",
    "auth.extauth.default_permission",
]


def _template_text() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _js_text() -> str:
    return JS_PATH.read_text(encoding="utf-8")


def test_template_defines_external_auth_field_ids():
    template = _template_text()

    for field_id in EXPECTED_TEMPLATE_IDS:
        assert f'id="{field_id}"' in template, f"missing input id {field_id}"


def test_template_has_no_deprecated_pam_field_ids():
    assert "auth.pam." not in _template_text()


def test_js_populates_external_auth_shape():
    js = _js_text()

    for path in (
        "authentication.external_auth.enabled",
        "authentication.external_auth.service",
        "authentication.external_auth.helper",
        "authentication.external_auth.timeout",
        "authentication.external_auth.allow_root",
        "authentication.external_auth.allowed_users",
        "authentication.external_auth.groups.admin_group",
        "authentication.external_auth.default_permission",
    ):
        assert path in js, f"js does not reference {path}"


def test_js_saves_external_auth_shape():
    js = _js_text()

    assert "deepSet(out, 'authentication.external_auth'" in js


def test_js_has_no_deprecated_pam_reference():
    js = _js_text()

    assert "auth.pam." not in js
    assert "authentication.pam" not in js
