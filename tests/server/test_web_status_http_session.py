"""Branch tests for WebStatusAdapter._handle_http_session.

Covers request-line parsing (empty / malformed), header collection,
content-length handling (missing, non-integer, body read, short-body
error), the enable_http_api master switch, OPTIONS/CORS branches, every
GET/POST dispatch target, and the unknown-route 404. No real sockets:
the reader is a scripted line feed and the writer records bytes.
"""

import asyncio

import pytest

from openmux.server.adapters.web_status import WebStatusAdapter


class _Reader:
    """Scripted feed of raw lines (each item a bytes chunk, no \\r\\n)."""

    def __init__(self, lines, body=b"", readexactly_exc=False):
        self._lines = list(lines)
        self._body = body
        self._readexactly_exc = readexactly_exc

    async def readline(self):
        if not self._lines:
            return b""
        out = self._lines.pop(0)
        return b"" if out is None else out + b"\r\n"

    async def readexactly(self, n):
        if self._readexactly_exc:
            raise asyncio.IncompleteReadError(b"", n)
        if not self._body:
            raise asyncio.IncompleteReadError(b"", n)
        return self._body[:n]


class _Writer:
    def __init__(self):
        self.chunks = []

    def write(self, data):
        self.chunks.append(data)

    async def drain(self):
        pass

    def text(self):
        return b"".join(self.chunks).decode("utf-8", errors="replace")


def _adapter(**config):
    return WebStatusAdapter("ws", dict({"port": 9999}, **config))


def _req(method="GET", path="/", headers=(), body=b""):
    lines = [f"{method} {path} HTTP/1.1".encode()] + [h.encode() for h in headers] + [b""]
    return _Reader(lines, body=body)


async def _run(reader, adapter):
    writer = _Writer()
    await adapter._handle_http_session(reader, writer)
    return writer.text()


@pytest.mark.asyncio
async def test_empty_stream_is_ignored():
    a = _adapter()
    out = await _run(_Reader([]), a)
    assert out == ""


@pytest.mark.asyncio
async def test_malformed_request_line_is_400():
    a = _adapter()
    out = await _run(_Reader([b"just-garbage"]), a)
    assert "400" in out and "Bad Request" in out


@pytest.mark.asyncio
async def test_root_listing_lists_endpoints():
    a = _adapter()
    out = await _run(_req("GET", "/"), a)
    assert "200" in out
    assert "/api/status" in out and "/api/fault (POST)" in out


@pytest.mark.asyncio
async def test_api_status_reports_counts():
    a = _adapter()
    a.clients["c1"] = {"address": "h:1", "connected_time": 0, "protocol": "http"}
    out = await _run(_req("GET", "/api/status"), a)
    assert '"connections": 1' in out
    assert '"http_api_enabled": true' in out


@pytest.mark.asyncio
async def test_api_clients_lists_tracked():
    a = _adapter()
    a.clients["c9"] = {"address": "1.2.3.4:1", "connected_time": 5.0, "protocol": "http"}
    out = await _run(_req("GET", "/api/clients"), a)
    assert '"c9"' in out and '"1.2.3.4:1"' in out


@pytest.mark.asyncio
async def test_api_ports_without_port_manager_is_503():
    a = _adapter()
    out = await _run(_req("GET", "/api/ports"), a)
    assert "503" in out and "Port manager not available" in out


@pytest.mark.asyncio
async def test_api_ports_with_port_manager():
    a = _adapter()

    class _PM:
        async def get_port_list_with_federation(self):
            return [{"name": "lo1"}]

    a.main_port_manager = _PM()
    out = await _run(_req("GET", "/api/ports"), a)
    assert '"lo1"' in out


@pytest.mark.asyncio
async def test_api_ports_with_port_manager_error_is_500():
    a = _adapter()

    class _PM:
        async def get_port_list_with_federation(self):
            raise RuntimeError("pm boom")

    a.main_port_manager = _PM()
    out = await _run(_req("GET", "/api/ports"), a)
    assert "500" in out and "pm boom" in out


@pytest.mark.asyncio
async def test_api_federation_and_multipath_without_muxcon_are_200_empty():
    a = _adapter()
    out = await _run(_req("GET", "/api/federation"), a)
    assert "200" in out
    out = await _run(_req("GET", "/api/multipath"), a)
    assert "200" in out and '"groups": []' in out


@pytest.mark.asyncio
async def test_post_fault_dispatched_with_body():
    a = _adapter()
    out = await _run(_req("POST", "/api/fault", headers=["Content-Length: 2"], body=b"{}"), a)
    # Fault injection is disabled by default in this adapter instance
    assert "403" in out and "Fault injection disabled" in out


@pytest.mark.asyncio
async def test_post_fault_body_short_read_falls_back_to_empty():
    a = _adapter(enable_fault_injection=True, muxcon=None)
    reader = _Reader(
        [b"POST /api/fault HTTP/1.1", b"Content-Length: 99", b""],
        body=b"{}",
        readexactly_exc=True,
    )
    writer = _Writer()
    await a._handle_http_session(reader, writer)
    out = writer.text()
    # Empty body -> missing action -> 400 (not a 500)
    assert "400" in out and "action" in out


@pytest.mark.asyncio
async def test_options_with_cors_enabled_204():
    a = _adapter()
    out = await _run(_req("OPTIONS", "/api/status", headers=["Access-Control-Request-Headers: x"]), a)
    assert "204 No Content" in out
    assert "Access-Control-Allow-Origin: *" in out
    # requested headers echoed when provided
    out = await _run(_req("OPTIONS", "/x", headers=["Access-Control-Request-Headers: x-test"]), a)
    assert "Access-Control-Allow-Headers: x-test" in out


@pytest.mark.asyncio
async def test_options_with_cors_disabled_is_404():
    a = _adapter(cors_enable=False)
    out = await _run(_req("OPTIONS", "/api/status"), a)
    assert "404" in out


@pytest.mark.asyncio
async def test_unknown_route_is_404():
    a = _adapter()
    out = await _run(_req("GET", "/nope"), a)
    assert "404" in out
    out = await _run(_req("DELETE", "/api/status"), a)
    assert "404" in out


@pytest.mark.asyncio
async def test_api_disabled_is_404_before_dispatch():
    a = _adapter(enable_http_api=False)
    out = await _run(_req("GET", "/api/status"), a)
    assert "404" in out


@pytest.mark.asyncio
async def test_malformed_content_length_treated_as_zero():
    a = _adapter()
    out = await _run(_req("GET", "/api/status", headers=["Content-Length: abc"]), a)
    assert "200" in out


@pytest.mark.asyncio
async def test_header_lines_without_colon_ignored():
    a = _adapter()
    out = await _run(_req("GET", "/api/status", headers=["no-colon-here", "X-Ok: 1"]), a)
    assert "200" in out


@pytest.mark.asyncio
async def test_malformed_header_line_ignored():
    # A header line that fails to decode/strip is skipped without breaking the loop
    reader = _Reader([b"GET /api/status HTTP/1.1", b"", b""])  # extra blank line
    a = _adapter()
    out = await _run(reader, a)
    assert "200" in out
