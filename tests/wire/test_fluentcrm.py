"""Wire-level tests for the FluentCRM client in ``fluentcrm.py``.

These never touch a live FluentCRM instance. Client policy (retry, timeout
clipping, pagination, parsing, error classification) is exercised through an
injected fake transport with a deterministic monotonic clock and sleep. The
one exception is ``default_transport`` / ``_make_connection``, which are driven
against a throwaway loopback ``HTTPServer`` and a closed port so the real
stdlib socket path is covered without any network dependency. No test issues a
live request, and no test mutates FluentCRM.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from iranfluent_tag_operator import config
from iranfluent_tag_operator import fluentcrm as fc
from iranfluent_tag_operator.config import Credentials
from iranfluent_tag_operator.fluentcrm import (
    ContactRecord,
    FluentCrmClient,
    HttpRequest,
    HttpResponse,
    TagRecord,
    TransportError,
    default_transport,
)
from iranfluent_tag_operator.models import CommandError, ErrorCategory


def _contract_doc(**over: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "schema_version": 1,
        "generated_at": "2026-09-24T00:00:00Z",
        "expires_at": "2026-10-24T00:00:00Z",
        "site": "https://crm.example.com",
        "rest_namespace": "fluent-crm/v2",
        "plugin_version": "3.1.5",
        "endpoints": {
            "contact_search": "/subscribers",
            "tag_lookup": "/tags",
            "contact_detail": "/subscribers/{id}",
            "attach_tags": "/subscribers/{id}",
        },
        "pagination_mode": "page_number",
        "contact_search_page_limit": 20,
        "tag_lookup_page_limit": 5,
        "response_keys": {
            "contact_search": ["id", "email", "status", "tags"],
            "tag_lookup": ["id", "title", "slug"],
        },
        "mutation": {"method": "POST", "path": "/subscribers/{id}", "body_keys": ["tags"]},
        "success_markers": {"status": 200, "body_keys": ["subscriber"]},
        "runtime_identity": {
            "package_version": "0.1.0",
            "git_commit": "a" * 40,
            "uv_lock_sha256": "b" * 64,
            "allowlist_sha256": "c" * 64,
        },
    }
    doc.update(over)
    return doc


def _fixture(**over: object) -> config.ContractFixture:
    return config.load_contract(json.dumps(_contract_doc(**over)).encode("utf-8"))


class FakeTransport:
    """Return (or raise) one queued outcome per call, recording every request."""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(
    transport,
    *,
    fixture: config.ContractFixture | None = None,
    monotonic=None,
    sleep=None,
    slept: list[float] | None = None,
) -> FluentCrmClient:
    creds = Credentials(
        base_url="https://crm.example.com",
        username="user",
        app_password="app pass word",
        audit_hmac_key="hmac",
    )
    if sleep is None and slept is not None:
        sleep = slept.append
    return FluentCrmClient(
        creds,
        fixture or _fixture(),
        transport=transport,
        monotonic=monotonic or (lambda: 0.0),
        sleep=sleep or (lambda _d: None),
    )


def _resp(status: int, body: object = b"", headers: dict | None = None) -> HttpResponse:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return HttpResponse(status, headers or {}, raw)


def _page(data: list, page: int, total: int) -> HttpResponse:
    return _resp(200, {"data": data, "page": page, "total_pages": total})


def _cursor(data: list, nxt: object) -> HttpResponse:
    return _resp(200, {"data": data, "next_cursor": nxt})


_CONTACT = {"id": 7, "email": "a@x.com", "status": "subscribed", "tags": [{"id": 269}]}
_DEADLINE = 1000.0


# --- default_transport / _make_connection (real stdlib socket path) --------


@pytest.fixture
def loopback_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("X-Test", "yes")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _req(url: str) -> HttpRequest:
    return HttpRequest("GET", url, {"Accept": "application/json"}, None, 2.0, 2.0)


def test_default_transport_success(loopback_server) -> None:
    url = f"http://127.0.0.1:{loopback_server.server_port}/x"
    response = default_transport(_req(url))
    assert response.status == 200
    # Header keys are lowercased for case-insensitive lookup downstream.
    assert response.headers["x-test"] == "yes"
    assert response.body == b'{"ok": true}'


def test_default_transport_rejects_unsupported_scheme() -> None:
    with pytest.raises(TransportError):
        default_transport(_req("ftp://host/path"))


def test_default_transport_wraps_connection_error() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # Port is now free; the connect below is refused.
    with pytest.raises(TransportError):
        default_transport(_req(f"http://127.0.0.1:{port}/x"))


def test_make_connection_https_branch() -> None:
    import http.client

    conn = fc._make_connection("https", "crm.example.com", 443, 1.0)
    # Constructing does not open a socket, so this is network-free.
    assert isinstance(conn, http.client.HTTPSConnection)


def test_make_connection_http_branch() -> None:
    import http.client

    conn = fc._make_connection("http", "crm.example.com", 80, 1.0)
    assert isinstance(conn, http.client.HTTPConnection)
    assert not isinstance(conn, http.client.HTTPSConnection)


# --- _send: deadline, retry, timeout clipping -------------------------------


def test_send_rejects_when_no_budget_remains() -> None:
    transport = FakeTransport([])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_id(7, deadline=0.0)
    assert caught.value.category is ErrorCategory.OPERATION_DEADLINE_EXCEEDED
    assert caught.value.mutation_attempted is False
    assert transport.requests == []  # Nothing dispatched without budget.


def test_read_retries_once_after_transport_error() -> None:
    slept: list[float] = []
    transport = FakeTransport([TransportError(), _resp(200, _CONTACT)])
    client = _client(transport, slept=slept)
    record = client.fetch_contact_by_id(7, deadline=_DEADLINE)
    assert record.id == 7
    assert len(transport.requests) == 2
    assert slept == [1.0]  # DEFAULT_RETRY_DELAY on a bare transport error.


def test_read_transport_error_twice_fails_closed() -> None:
    transport = FakeTransport([TransportError(), TransportError()])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_id(7, deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.REMOTE_TRANSPORT_FAILED
    assert caught.value.mutation_attempted is False
    assert len(transport.requests) == 2  # One retry, then give up.


def test_read_transport_error_no_retry_when_deadline_too_tight() -> None:
    slept: list[float] = []
    transport = FakeTransport([TransportError()])
    client = _client(transport, slept=slept)
    # remaining (0.5) < DEFAULT_RETRY_DELAY (1.0) => _wait returns False.
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_id(7, deadline=0.5)
    assert caught.value.category is ErrorCategory.REMOTE_TRANSPORT_FAILED
    assert len(transport.requests) == 1
    assert slept == []


def test_read_retries_once_on_retryable_status() -> None:
    slept: list[float] = []
    transport = FakeTransport([_resp(503), _resp(200, _CONTACT)])
    client = _client(transport, slept=slept)
    record = client.fetch_contact_by_id(7, deadline=_DEADLINE)
    assert record.id == 7
    assert len(transport.requests) == 2
    assert slept == [1.0]


def test_read_retryable_status_twice_is_classified() -> None:
    transport = FakeTransport([_resp(503), _resp(503)])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_id(7, deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.REMOTE_SERVER_ERROR
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    ("header", "expected"),
    [("2", 2.0), ("10", 5.0), ("-1", 1.0), ("abc", 1.0), (" 3 ", 3.0)],
)
def test_retry_after_header_parsing(header: str, expected: float) -> None:
    slept: list[float] = []
    transport = FakeTransport(
        [_resp(503, headers={"retry-after": header}), _resp(200, _CONTACT)]
    )
    client = _client(transport, slept=slept)
    client.fetch_contact_by_id(7, deadline=_DEADLINE)
    assert slept == [expected]


def test_mutation_transport_error_reports_attempted() -> None:
    transport = FakeTransport([TransportError()])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.attach_tag(7, 269, deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.REMOTE_TRANSPORT_FAILED
    assert caught.value.mutation_attempted is True
    assert len(transport.requests) == 1  # POST is single-shot, never retried.


# --- request construction, timeout clipping --------------------------------


def test_get_request_shape_and_timeouts() -> None:
    transport = FakeTransport([_resp(200, _CONTACT)])
    client = _client(transport)
    client.fetch_contact_by_id(7, deadline=_DEADLINE)
    req = transport.requests[0]
    assert req.method == "GET"
    assert req.url == "https://crm.example.com/wp-json/fluent-crm/v2/subscribers/7"
    assert req.headers["Authorization"].startswith("Basic ")
    assert req.headers["Accept"] == "application/json"
    assert "Content-Type" not in req.headers  # No body on a GET.
    assert req.body is None
    assert req.connect_timeout == 3.0  # min(CONNECT_TIMEOUT, remaining)
    assert req.response_timeout == 10.0  # min(RESPONSE_TIMEOUT, remaining)


def test_timeouts_clip_to_remaining_budget() -> None:
    transport = FakeTransport([_resp(200, _CONTACT)])
    client = _client(transport)
    client.fetch_contact_by_id(7, deadline=1.5)
    req = transport.requests[0]
    assert req.connect_timeout == 1.5
    assert req.response_timeout == 1.5


def test_post_request_shape() -> None:
    transport = FakeTransport([_resp(200, {"subscriber": {}})])
    client = _client(transport)
    client.attach_tag(7, 269, deadline=_DEADLINE)
    req = transport.requests[0]
    assert req.method == "POST"
    assert req.url == "https://crm.example.com/wp-json/fluent-crm/v2/subscribers/7"
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.body) == {"tags": [269]}


# --- _classified: status -> ErrorCategory -----------------------------------


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (401, ErrorCategory.AUTHENTICATION_FAILED),
        (403, ErrorCategory.AUTHORIZATION_FAILED),
        (500, ErrorCategory.REMOTE_SERVER_ERROR),
        (404, ErrorCategory.REMOTE_RESPONSE_INVALID),
    ],
)
def test_read_status_classification(status: int, category: ErrorCategory) -> None:
    transport = FakeTransport([_resp(status)])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_id(7, deadline=_DEADLINE)
    assert caught.value.category is category
    assert caught.value.http_status == status


def test_read_rate_limited_after_retry_exhausted() -> None:
    transport = FakeTransport([_resp(429), _resp(429)])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_id(7, deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.REMOTE_RATE_LIMITED


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (401, ErrorCategory.AUTHENTICATION_FAILED),
        (403, ErrorCategory.AUTHORIZATION_FAILED),
        (500, ErrorCategory.REMOTE_SERVER_ERROR),
        (422, ErrorCategory.REMOTE_REJECTED),
    ],
)
def test_mutation_status_classification(status: int, category: ErrorCategory) -> None:
    transport = FakeTransport([_resp(status)])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.attach_tag(7, 269, deadline=_DEADLINE)
    assert caught.value.category is category
    # 401/403 are auth failures without a mutation flag; the rest carry it.
    if category in (ErrorCategory.REMOTE_SERVER_ERROR, ErrorCategory.REMOTE_REJECTED):
        assert caught.value.mutation_attempted is True


@pytest.mark.parametrize("body", [b"{bad json", b"\xff\xfe"])
def test_read_json_fails_closed(body: bytes) -> None:
    transport = FakeTransport([HttpResponse(200, {}, body)])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_id(7, deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.REMOTE_RESPONSE_INVALID


# --- _stream_pages: page-number mode (via fetch_contact_by_email) ----------


def test_search_single_page_match() -> None:
    transport = FakeTransport([_page([_CONTACT], 1, 1)])
    client = _client(transport)
    record = client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE)
    assert record is not None and record.id == 7


def test_search_no_match_returns_none() -> None:
    transport = FakeTransport([_page([], 1, 1)])
    client = _client(transport)
    assert client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE) is None


def test_search_skips_non_matching_item_in_page() -> None:
    other = {"id": 8, "email": "b@x.com", "status": "subscribed", "tags": []}
    transport = FakeTransport([_page([other], 1, 1)])
    client = _client(transport)
    assert client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE) is None


def test_search_walks_pages_until_total() -> None:
    transport = FakeTransport([_page([], 1, 2), _page([_CONTACT], 2, 2)])
    client = _client(transport)
    record = client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE)
    assert record is not None and record.id == 7
    assert len(transport.requests) == 2


def test_search_ambiguous_second_match_fails_closed() -> None:
    other = {**_CONTACT, "id": 8}  # Same email, different id => ambiguous.
    transport = FakeTransport([_page([_CONTACT, other], 1, 1)])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.CONTACT_AMBIGUOUS


def test_search_rejects_echoed_page_mismatch() -> None:
    transport = FakeTransport([_page([], 2, 2)])  # Asked page 1, got page 2.
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.REMOTE_RESPONSE_INVALID


def test_search_runaway_page_count_is_capped() -> None:
    # page_limit 1 => cap 1; a server that always claims another page trips it.
    transport = FakeTransport([_page([], 1, 2)])
    client = _client(transport, fixture=_fixture(contact_search_page_limit=1))
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.REMOTE_RESPONSE_INVALID
    assert len(transport.requests) == 1  # Second page never dispatched.


def test_search_non_200_in_stream_is_classified() -> None:
    transport = FakeTransport([_resp(500)])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.REMOTE_SERVER_ERROR


# --- _stream_pages: cursor mode (via fetch_contact_by_email) ---------------


def test_cursor_single_page_terminates_on_null() -> None:
    transport = FakeTransport([_cursor([_CONTACT], None)])
    client = _client(transport, fixture=_fixture(pagination_mode="cursor"))
    record = client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE)
    assert record is not None and record.id == 7
    assert "cursor" not in transport.requests[0].url  # First page carries none.


def test_cursor_follows_next_then_stops() -> None:
    transport = FakeTransport([_cursor([], "c1"), _cursor([_CONTACT], None)])
    client = _client(transport, fixture=_fixture(pagination_mode="cursor"))
    record = client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE)
    assert record is not None and record.id == 7
    assert len(transport.requests) == 2
    assert "cursor=c1" in transport.requests[1].url  # Second page echoes cursor.


def test_cursor_cycle_is_rejected() -> None:
    transport = FakeTransport([_cursor([], "c1"), _cursor([], "c1")])
    client = _client(transport, fixture=_fixture(pagination_mode="cursor"))
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_email("a@x.com", deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.REMOTE_RESPONSE_INVALID


# --- envelope parsers: fail-closed branches --------------------------------


def test_page_number_envelope_accepts_valid() -> None:
    assert fc._page_number_envelope({"data": [1], "page": 1, "total_pages": 2}) == ([1], 1, 2)


@pytest.mark.parametrize(
    "body",
    [
        [1, 2, 3],  # Not a dict.
        {"data": "x", "page": 1, "total_pages": 1},  # data not a list.
        {"data": [], "page": "1", "total_pages": 1},  # page not an int.
        {"data": [], "page": 1, "total_pages": "1"},  # total not an int.
        {"data": [], "page": True, "total_pages": 1},  # bool rejected as int.
        {"data": [], "page": 0, "total_pages": 1},  # page below 1.
        {"data": [], "page": 1, "total_pages": 0},  # total below 1.
    ],
)
def test_page_number_envelope_rejects(body: object) -> None:
    with pytest.raises(CommandError) as caught:
        fc._page_number_envelope(body)
    assert caught.value.category is ErrorCategory.REMOTE_RESPONSE_INVALID


def test_cursor_envelope_accepts_null_and_str() -> None:
    assert fc._cursor_envelope({"data": [1], "next_cursor": None}) == ([1], None)
    assert fc._cursor_envelope({"data": [], "next_cursor": "c"}) == ([], "c")


@pytest.mark.parametrize(
    "body",
    [
        [1],  # Not a dict.
        {"next_cursor": None},  # Missing data key.
        {"data": []},  # Missing next_cursor key.
        {"data": "x", "next_cursor": None},  # data not a list.
        {"data": [], "next_cursor": 5},  # next_cursor not str/None.
        {"data": [], "next_cursor": ""},  # next_cursor empty string.
    ],
)
def test_cursor_envelope_rejects(body: object) -> None:
    with pytest.raises(CommandError) as caught:
        fc._cursor_envelope(body)
    assert caught.value.category is ErrorCategory.REMOTE_RESPONSE_INVALID


# --- item parsers: fail-closed + lenient full_name -------------------------


def test_parse_contact_item_accepts_and_defaults_full_name() -> None:
    record = fc._parse_contact_item(
        {"id": 7, "email": "a@x.com", "status": "subscribed", "tags": [{"id": 269}]}
    )
    assert record.id == 7 and record.tag_ids == (269,)
    assert record.full_name == ""  # Absent name defaults to empty string.


def test_parse_contact_item_keeps_str_full_name() -> None:
    record = fc._parse_contact_item(
        {"id": 7, "email": "a@x.com", "status": "subscribed", "tags": [], "full_name": "Ann"}
    )
    assert record.full_name == "Ann"


def test_parse_contact_item_ignores_non_str_full_name() -> None:
    record = fc._parse_contact_item(
        {"id": 7, "email": "a@x.com", "status": "subscribed", "tags": [], "full_name": 5}
    )
    assert record.full_name == ""  # Non-str name falls back to empty string.


@pytest.mark.parametrize(
    "item",
    [
        [1],  # Not a dict.
        {"id": "7", "email": "a@x.com", "status": "subscribed", "tags": []},  # id not int.
        {"id": True, "email": "a@x.com", "status": "subscribed", "tags": []},  # bool id.
        {"id": 7, "email": 1, "status": "subscribed", "tags": []},  # email not str.
        {"id": 7, "email": "a@x.com", "status": 1, "tags": []},  # status not str.
        {"id": 7, "email": "a@x.com", "status": "subscribed", "tags": "x"},  # tags not list.
    ],
)
def test_parse_contact_item_rejects(item: object) -> None:
    with pytest.raises(CommandError) as caught:
        fc._parse_contact_item(item)
    assert caught.value.category is ErrorCategory.REMOTE_RESPONSE_INVALID


@pytest.mark.parametrize("tag", [5, {"id": "1"}, {"id": True}, {"slug": "x"}])
def test_parse_tag_id_rejects(tag: object) -> None:
    with pytest.raises(CommandError) as caught:
        fc._parse_tag_id(tag)
    assert caught.value.category is ErrorCategory.REMOTE_RESPONSE_INVALID


def test_parse_tag_item_accepts_valid() -> None:
    record = fc._parse_tag_item({"id": 269, "title": "set_vocab_B1", "slug": "set_vocab_b1"})
    assert record == TagRecord(269, "set_vocab_B1", "set_vocab_b1")


@pytest.mark.parametrize(
    "item",
    [
        [1],  # Not a dict.
        {"id": "1", "title": "t", "slug": "s"},  # id not int.
        {"id": 1, "title": 2, "slug": "s"},  # title not str.
        {"id": 1, "title": "t", "slug": 3},  # slug not str.
    ],
)
def test_parse_tag_item_rejects(item: object) -> None:
    with pytest.raises(CommandError) as caught:
        fc._parse_tag_item(item)
    assert caught.value.category is ErrorCategory.REMOTE_RESPONSE_INVALID


# --- fetch_tag: match vs exhaust -------------------------------------------

_TAG = {"id": 269, "title": "set_vocab_B1", "slug": "set_vocab_b1"}


def test_fetch_tag_returns_matching_record() -> None:
    transport = FakeTransport([_page([_TAG], 1, 1)])
    client = _client(transport)
    record = client.fetch_tag(269, deadline=_DEADLINE)
    assert record == TagRecord(269, "set_vocab_B1", "set_vocab_b1")


def test_fetch_tag_returns_none_when_absent() -> None:
    transport = FakeTransport([_page([_TAG], 1, 1)])
    client = _client(transport)
    assert client.fetch_tag(999, deadline=_DEADLINE) is None


# --- fetch_contact_by_id: verification re-fetch ----------------------------


def test_fetch_contact_by_id_returns_record() -> None:
    transport = FakeTransport([_resp(200, _CONTACT)])
    client = _client(transport)
    record = client.fetch_contact_by_id(7, deadline=_DEADLINE)
    assert record.id == 7 and record.tag_ids == (269,)


def test_fetch_contact_by_id_id_mismatch_is_response_invalid() -> None:
    # A 200 that echoes a different subscriber id (proxy/cache confusion) must
    # not be trusted: the detail endpoint has to return the requested id.
    wrong = {**_CONTACT, "id": 8}
    transport = FakeTransport([_resp(200, wrong)])
    client = _client(transport)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_id(7, deadline=_DEADLINE)
    assert caught.value.category is ErrorCategory.REMOTE_RESPONSE_INVALID


def test_fetch_contact_by_id_non_retryable_is_single_shot() -> None:
    # Post-dispatch verification reads pass retryable=False: a retryable 503 that
    # the default policy would retry once must instead fail after a single GET.
    slept: list[float] = []
    transport = FakeTransport([_resp(503), _resp(200, _CONTACT)])
    client = _client(transport, slept=slept)
    with pytest.raises(CommandError) as caught:
        client.fetch_contact_by_id(7, deadline=_DEADLINE, retryable=False)
    assert caught.value.category is ErrorCategory.REMOTE_SERVER_ERROR
    assert len(transport.requests) == 1  # no retry
    assert slept == []
