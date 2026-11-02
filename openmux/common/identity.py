"""Shared server identity resolution.

One place that knows how the server identifies itself. Every surface that
shows the server's name (web console, Basic-Auth headers, telnet/SSH menu
banners, the CLI client's LIST banner, the command port banner, the MuxCon
federation advert, the autogen TLS CN) uses the same resolution instead of a
per-surface default:

1. ``server.description``  - the operator's free-form label
2. ``OpenMux {id}``        - derived from ``server.id``, or the system
                             hostname when ``id`` is absent

``server.id`` is the sole identity key. ``server.name``,
``server.server_id``, and ``muxcon.server_id`` are removed (ticket #74):
the schema rejects them and the ConfigManager deprecation shim strips them
with a warning at load time.

The derived form keeps "OpenMux" as a product prefix and appends the node
identity, so several OpenMux consoles are distinguishable in browser dialogs
and terminal banners without any configuration.

``certificate_cn`` maps that identity into an X.509 CommonName, and
``warn_long_server_id`` reports an identity too long for one. The server calls
the reporter on every config load, so the length is known at startup and not
only once a certificate is built.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional


def get_server_id(server_cfg: Optional[Any]) -> Optional[str]:
    """Return the server's identity value from a ``server`` config section.

    ``server.id`` is the sole identity key (ticket #74). Returns None when it
    is absent or blank; callers fall back to the system hostname.
    ``server.name`` / ``server.server_id`` are not read: the schema rejects
    them and the deprecation shim strips them at load time.
    """
    if not isinstance(server_cfg, dict):
        return None
    val = server_cfg.get("id")
    if val is not None:
        text = str(val).strip()
        if text:
            return text
    return None


def get_server_label(server_cfg: Optional[Any], hostname: Optional[str] = None) -> str:
    """Return the human-readable server label for display surfaces.

    Args:
        server_cfg: The ``server`` section of server.yaml (may be None).
        hostname: System hostname, used only when the server section carries
            no id of its own. Resolved via ``socket.gethostname()`` when
            omitted.

    Returns:
        ``server.description`` when set, else ``"OpenMux {id}"`` where id is
        ``server.id`` or the system hostname. Never empty.
    """
    desc = None
    if isinstance(server_cfg, dict):
        raw = server_cfg.get("description")
        if raw is not None:
            desc = str(raw).strip()
    if desc:
        return desc
    sid = get_server_id(server_cfg)
    if not sid:
        if hostname is None:
            hostname = _system_hostname()
        sid = str(hostname).strip() or "server"
    return f"OpenMux {sid}"


def _sanitize_label(text: str) -> str:
    """Make a label safe for a ``WWW-Authenticate: Basic realm="..."`` header.

    Strips NUL bytes and quotes the result, escaping backslashes and double
    quotes per HTTP semantics.
    """
    safe = (text or "").replace("\x00", "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{safe}"'


def basic_authenticate_header(realm: str) -> str:
    """Build a ``WWW-Authenticate: Basic`` header value for a realm string."""
    return f"Basic realm={_sanitize_label(realm)}"


#: Ceiling of an X.509 attribute value (``ub-common-name``, RFC 5280). The
#: cryptography package refuses a longer CommonName, and a certificate
#: carrying one cannot be built at all.
CERT_CN_MAX_LEN = 64

#: Hex digits of the whole id kept at the end of a shortened CommonName. Enough
#: to tell two ids apart in practice, short enough to leave a readable head.
CERT_CN_DIGEST_LEN = 12

#: Marks where a CommonName was shortened. Plain ASCII, so a certificate viewer
#: shows it without a character-set question.
CERT_CN_TRUNCATION_MARK = ".."


def _system_hostname() -> str:
    """Return the system hostname, or an empty string when the host cannot name itself."""
    try:
        import socket

        return socket.gethostname()
    except Exception:
        # justification: a host that cannot name itself still needs a usable identity fallback
        return ""


def _cn_subject(server_id: Optional[str], hostname: Optional[str]) -> str:
    """Resolve what a certificate label is built from: the id, else the hostname, never empty.

    A blank id counts as unset. The chain lives here alone, so the label rule, the reporter
    ``warn_long_server_id``, and both certificate builders cannot drift apart on what the subject is.
    An unset ``hostname`` is NOT a hostname lookup: callers that want one pass ``_system_hostname()``.
    """
    return str(server_id or hostname or "OpenMux").strip() or "OpenMux"


def certificate_cn(server_id: Optional[str], hostname: Optional[str] = None) -> str:
    """Return the CommonName to put in an autogenerated self-signed certificate.

    The same identity as every other surface: ``server.id``, the hostname when
    the server section carries no id, and never empty. A short id is returned
    unchanged.

    An id past the X.509 ceiling cannot be carried at all, so it is shortened
    rather than rejected: a certificate that cannot be built takes the TLS
    listener down with it. The shortened label keeps the head of the id and ends
    with a digest of the WHOLE id. Cutting instead would hand two ids that share
    a 64-character prefix one CommonName, and a human could not tell those nodes
    apart (issue #118). The digest makes such a clash need an engineered SHA-256
    prefix rather than a shared asset name.

    Nothing in the product reads this label. Federation trusts a certificate by
    its fingerprint, recorded on first use, so the CommonName only tells a human
    which node a certificate belongs to.
    """
    cn = _cn_subject(server_id, hostname)
    if len(cn) <= CERT_CN_MAX_LEN:
        return cn
    head_len = CERT_CN_MAX_LEN - len(CERT_CN_TRUNCATION_MARK) - CERT_CN_DIGEST_LEN
    digest = hashlib.sha256(cn.encode("utf-8")).hexdigest()[:CERT_CN_DIGEST_LEN]
    return f"{cn[:head_len]}{CERT_CN_TRUNCATION_MARK}{digest}"


def warn_long_server_id(logger: Any, server_section: Optional[Any]) -> None:
    """Log one WARNING when the server identity is too long for a certificate CommonName.

    The server calls this on every config load, whether TLS is on or not. The length is a property
    of ``server.id``, not of a certificate built so far: a node that turns TLS on later meets the
    same ceiling, and one line at startup is what tells an operator that a certificate viewer will
    show a label nobody typed. Values this long are rare, so the line is not background noise.

    Nothing about a link changes: federation trusts a certificate by its fingerprint, recorded on
    first use, and no code reads the label (ADR-0003).
    """
    server_id = get_server_id(server_section)
    identity = _cn_subject(server_id, None if server_id else _system_hostname())
    cn = certificate_cn(identity)
    if cn == identity:
        return
    # Name what the operator has to edit: server.id when set, else the hostname it falls back to.
    source = "server.id" if server_id else "The system hostname (server.id is unset)"
    logger.warning(
        "%s is %d characters and an X.509 CommonName holds %d, so an autogenerated TLS certificate is labelled '%s' instead "
        "— the head of the value plus a digest of all of it. That label is display text: federation trusts a certificate by "
        "its fingerprint, recorded on first connect. Nothing else uses the length of the value.",
        source,
        len(identity),
        CERT_CN_MAX_LEN,
        cn,
    )
