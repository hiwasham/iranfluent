"""FluentCRM HTTP client: read primitives, single-shot mutation, redaction.

Stdlib-only (``http.client``/``urllib``) so the package keeps ``deps == []``.
The client owns request construction, contract-driven streaming pagination,
method-specific timeout and retry policy, response parsing, and remote-error
normalization into :class:`CommandError`. It does NOT own preview/execute
policy sequencing (that is ``application``) and it never touches SQLite,
the environment, or the terminal.

Envelope shape assumed here is the reviewed FluentCRM shape confirmed by the
live contract probe (T3): a page-number list response is
``{"data": [...], "page": <int>, "total_pages": <int>}`` and a cursor list
response is ``{"data": [...], "next_cursor": <str|null>}``. A subscriber item
carries ``id``/``email``/``status`` and a ``tags`` list of tag objects; a tag
object carries ``id``/``title``/``slug``. Any deviation fails closed as
``remote_response_invalid`` rather than being coerced.

Read-only requests (contact/tag lookup, verification re-fetch) retry at most
once on a transport error or HTTP 429/502/503/504, honoring ``Retry-After`` up
to five seconds, else a bounded one-second delay, with every wait and request
timeout clipped to the remaining monotonic phase deadline. A mutation POST is
issued exactly once and never retried; any failure after it is dispatched is
reported with ``mutation_attempted = True`` so the caller reconciles.
"""

from __future__ import annotations

import base64
import http.client
import json
import ssl
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

from .config import ContractFixture, Credentials
from .models import CommandError, ErrorCategory

CONNECT_TIMEOUT = 3.0
RESPONSE_TIMEOUT = 10.0
MAX_RETRY_AFTER = 5.0
DEFAULT_RETRY_DELAY = 1.0
ABSOLUTE_PAGE_CAP = 100
WP_JSON_PREFIX = "/wp-json/"

_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

@dataclass(frozen=True)
class ContactRecord:
    """One subscriber as returned by FluentCRM (real email, never masked)."""

    id: int
    email: str
    full_name: str
    status: str
    tag_ids: tuple[int, ...]


@dataclass(frozen=True)
class TagRecord:
    """One tag object as returned by FluentCRM."""

    id: int
    title: str
    slug: str


@dataclass(frozen=True)
class HttpRequest:
    """A fully-built request handed to the injected transport."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    connect_timeout: float
    response_timeout: float


@dataclass(frozen=True)
class HttpResponse:
    """A parsed transport response; ``headers`` keys are lowercased."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class TransportError(Exception):
    """Any connect/read/protocol failure below the status-code layer."""


Transport = Callable[[HttpRequest], HttpResponse]


def _make_connection(
    scheme: str, host: str, port: int | None, timeout: float
) -> http.client.HTTPConnection:
    # Constructing a connection does not open a socket; both scheme branches
    # are therefore unit-coverable without a live network.
    if scheme == "https":
        return http.client.HTTPSConnection(
            host, port, timeout=timeout, context=ssl.create_default_context()
        )
    return http.client.HTTPConnection(host, port, timeout=timeout)


def default_transport(request: HttpRequest) -> HttpResponse:
    """Issue one request over ``http.client``; normalize failures.

    The connect timeout governs socket establishment; once connected the
    response timeout governs the read. Any low-level error becomes a
    :class:`TransportError` with no message, so nothing about the wire or the
    credentials can leak into a diagnostic.
    """

    parts = urllib.parse.urlsplit(request.url)
    if parts.scheme not in ("http", "https"):
        raise TransportError()
    conn = _make_connection(
        parts.scheme, parts.hostname, parts.port, request.connect_timeout
    )
    try:
        conn.connect()
        conn.sock.settimeout(request.response_timeout)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        conn.request(request.method, path, body=request.body, headers=dict(request.headers))
        raw = conn.getresponse()
        body = raw.read()
        headers = {key.lower(): value for key, value in raw.getheaders()}
        return HttpResponse(raw.status, headers, body)
    except (OSError, http.client.HTTPException) as exc:
        raise TransportError() from exc
    finally:
        conn.close()


def _is_int(value: object) -> bool:
    # JSON booleans are ``int`` subclasses; a real int field must reject them.
    return isinstance(value, int) and not isinstance(value, bool)


def _invalid() -> CommandError:
    return CommandError(ErrorCategory.REMOTE_RESPONSE_INVALID)


class FluentCrmClient:
    """Read/write primitives against one reviewed FluentCRM site.

    All policy sequencing lives in ``application``; this object only builds
    requests, streams pages within the contract's page caps, and normalizes
    every failure into a :class:`CommandError`. The transport, monotonic clock,
    and sleep are injected so wire behavior is fully deterministic under test.
    """

    def __init__(
        self,
        credentials: Credentials,
        contract: ContractFixture,
        *,
        transport: Transport = default_transport,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._contract = contract
        self._transport = transport
        self._monotonic = monotonic
        self._sleep = sleep
        self._base = credentials.base_url.rstrip("/")
        self._namespace = contract.rest_namespace.strip("/")
        token = f"{credentials.username}:{credentials.app_password}".encode("utf-8")
        # Held in memory only; never logged, never placed in an audit row.
        self._auth_header = "Basic " + base64.b64encode(token).decode("ascii")

    def _headers(self, body: bytes | None) -> dict[str, str]:
        headers = {"Authorization": self._auth_header, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str, params: Mapping[str, object] | None = None) -> str:
        url = f"{self._base}{WP_JSON_PREFIX}{self._namespace}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url


    def _send(
        self,
        method: str,
        url: str,
        *,
        deadline: float,
        body: bytes | None = None,
        retryable: bool,
        mutation: bool,
    ) -> HttpResponse:
        """Issue a request within the phase deadline, retrying reads once.

        Timeouts are clipped to the remaining budget; a request never starts
        without room for a positive timeout. Reads retry once on a transport
        error or a retryable status; a mutation is dispatched exactly once.
        Transport failure after a mutation is dispatched carries
        ``mutation_attempted=True`` so the caller reconciles.
        """

        retried = False
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise CommandError(
                    ErrorCategory.OPERATION_DEADLINE_EXCEEDED, mutation_attempted=False
                )
            request = HttpRequest(
                method,
                url,
                self._headers(body),
                body,
                min(CONNECT_TIMEOUT, remaining),
                min(RESPONSE_TIMEOUT, remaining),
            )
            try:
                response = self._transport(request)
            except TransportError:
                if retryable and not retried and self._wait(DEFAULT_RETRY_DELAY, deadline):
                    retried = True
                    continue
                raise CommandError(
                    ErrorCategory.REMOTE_TRANSPORT_FAILED, mutation_attempted=mutation
                )
            if (
                retryable
                and not retried
                and response.status in _RETRYABLE_STATUSES
                and self._wait(self._retry_delay(response), deadline)
            ):
                retried = True
                continue
            return response



    def _wait(self, delay: float, deadline: float) -> bool:
        """Sleep ``delay`` only if a positive request timeout would remain."""

        remaining = deadline - self._monotonic()
        if delay >= remaining:
            return False
        self._sleep(delay)
        return True

    def _retry_delay(self, response: HttpResponse) -> float:
        """Honor a well-formed ``Retry-After`` up to the cap, else the default."""

        raw = response.headers.get("retry-after")
        if raw is None:
            return DEFAULT_RETRY_DELAY
        try:
            seconds = int(raw.strip())
        except ValueError:
            return DEFAULT_RETRY_DELAY
        if seconds < 0:
            return DEFAULT_RETRY_DELAY
        return min(float(seconds), MAX_RETRY_AFTER)

    def _classified(self, response: HttpResponse, *, mutation: bool) -> CommandError:
        """Map a non-success status to the reviewed error category."""

        status = response.status
        if status == 401:
            return CommandError(ErrorCategory.AUTHENTICATION_FAILED, http_status=status)
        if status == 403:
            return CommandError(ErrorCategory.AUTHORIZATION_FAILED, http_status=status)
        if mutation:
            category = (
                ErrorCategory.REMOTE_SERVER_ERROR
                if status >= 500
                else ErrorCategory.REMOTE_REJECTED
            )
            return CommandError(category, http_status=status, mutation_attempted=True)
        if status == 429:
            return CommandError(ErrorCategory.REMOTE_RATE_LIMITED, http_status=status)
        if status >= 500:
            return CommandError(ErrorCategory.REMOTE_SERVER_ERROR, http_status=status)
        return CommandError(ErrorCategory.REMOTE_RESPONSE_INVALID, http_status=status)

    def _read_json(self, response: HttpResponse) -> object:
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _invalid()


    def _stream_pages(
        self,
        endpoint: str,
        params: Mapping[str, object],
        page_limit: int,
        deadline: float,
    ) -> Iterator[list]:
        """Yield each page's item list, failing closed on bad pagination.

        Page-number mode requests monotonically and requires the echoed page to
        match; cursor mode follows ``next_cursor`` and rejects any repeat. The
        page count is capped at the lower of the endpoint limit and the absolute
        guard, so a runaway or looping server cannot exhaust memory.
        """

        cap = min(page_limit, ABSOLUTE_PAGE_CAP)
        path = self._contract.endpoints[endpoint]
        page_number = self._contract.pagination_mode == "page_number"
        visited: set[str] = set()
        cursor: str | None = None
        page = 1
        count = 0
        while True:
            count += 1
            if count > cap:
                raise _invalid()
            if page_number:
                query = {**params, "page": page, "per_page": page_limit}
            else:
                query = dict(params)
                if cursor is not None:
                    query["cursor"] = cursor
            response = self._send(
                "GET", self._url(path, query), deadline=deadline, retryable=True, mutation=False
            )
            if response.status != 200:
                raise self._classified(response, mutation=False)
            body = self._read_json(response)
            if page_number:
                data, echoed, total = _page_number_envelope(body)
                if echoed != page:
                    raise _invalid()
                yield data
                if page >= total:
                    return
                page += 1
            else:
                data, nxt = _cursor_envelope(body)
                yield data
                if nxt is None:
                    return
                if nxt in visited:
                    raise _invalid()
                visited.add(nxt)
                cursor = nxt


    def fetch_contact_by_email(self, email: str, *, deadline: float) -> ContactRecord | None:
        """Return the one subscriber whose email matches, else ``None``.

        The search term is trimmed and lowercased for both the query parameter
        and the exact comparison; a second exact match fails closed as
        ``contact_ambiguous`` rather than silently tagging the wrong person.
        """

        normalized = email.strip().lower()
        match: ContactRecord | None = None
        pages = self._stream_pages(
            "contact_search",
            {"search": normalized},
            self._contract.contact_search_page_limit,
            deadline,
        )
        for items in pages:
            for item in items:
                record = _parse_contact_item(item)
                if record.email.strip().lower() == normalized:
                    if match is not None:
                        raise CommandError(ErrorCategory.CONTACT_AMBIGUOUS)
                    match = record
        return match

    def fetch_contact_by_id(
        self, contact_id: int, *, deadline: float, retryable: bool = True
    ) -> ContactRecord:
        """Re-fetch one subscriber by id for post-write verification.

        Reconcile and pre-dispatch re-validation reads retry once per the
        read-only policy (``retryable=True``, the default). Post-dispatch
        verification reads are single-shot (``retryable=False``): the caller
        owns the 1/2/4-second cadence and each read is clipped to the shared
        post-dispatch budget, so an internal retry would both double the reads
        and consume that budget.
        """

        path = self._contract.endpoints["contact_detail"].replace("{id}", str(contact_id))
        response = self._send(
            "GET", self._url(path), deadline=deadline, retryable=retryable, mutation=False
        )
        if response.status != 200:
            raise self._classified(response, mutation=False)
        return _parse_contact_item(self._read_json(response))

    def fetch_tag(self, tag_id: int, *, deadline: float) -> TagRecord | None:
        """Return the reviewed tag object by id, else ``None``."""

        pages = self._stream_pages(
            "tag_lookup", {}, self._contract.tag_lookup_page_limit, deadline
        )
        for items in pages:
            for item in items:
                record = _parse_tag_item(item)
                if record.id == tag_id:
                    return record
        return None

    def attach_tag(self, contact_id: int, tag_id: int, *, deadline: float) -> None:
        """Issue the single-shot attach POST; raise on any non-success status.

        The POST is dispatched exactly once (``retryable=False``); a transport
        failure after dispatch surfaces as ``mutation_attempted=True`` from
        ``_send`` so the caller reconciles the real remote state.
        """

        path = self._contract.mutation_path.replace("{id}", str(contact_id))
        body = json.dumps({"tags": [tag_id]}).encode("utf-8")
        response = self._send(
            self._contract.mutation_method,
            self._url(path),
            deadline=deadline,
            body=body,
            retryable=False,
            mutation=True,
        )
        if response.status != self._contract.success_status:
            raise self._classified(response, mutation=True)


def _page_number_envelope(body: object) -> tuple[list, int, int]:
    """Destructure a page-number envelope, failing closed on any deviation."""

    if not isinstance(body, dict):
        raise _invalid()
    data = body.get("data")
    page = body.get("page")
    total = body.get("total_pages")
    if not isinstance(data, list) or not _is_int(page) or not _is_int(total):
        raise _invalid()
    if page < 1 or total < 1:
        raise _invalid()
    return data, page, total


def _cursor_envelope(body: object) -> tuple[list, str | None]:
    """Destructure a cursor envelope; ``next_cursor`` is null or a non-empty str."""

    if not isinstance(body, dict) or "data" not in body or "next_cursor" not in body:
        raise _invalid()
    data = body["data"]
    if not isinstance(data, list):
        raise _invalid()
    nxt = body["next_cursor"]
    if nxt is not None and (not isinstance(nxt, str) or not nxt):
        raise _invalid()
    return data, nxt


def _parse_contact_item(item: object) -> ContactRecord:
    """Parse one subscriber item; any missing or mistyped field fails closed."""

    if not isinstance(item, dict):
        raise _invalid()
    cid = item.get("id")
    email = item.get("email")
    status = item.get("status")
    tags = item.get("tags")
    if not _is_int(cid) or not isinstance(email, str) or not isinstance(status, str):
        raise _invalid()
    if not isinstance(tags, list):
        raise _invalid()
    tag_ids = tuple(_parse_tag_id(tag) for tag in tags)
    name = item.get("full_name")
    full_name = name if isinstance(name, str) else ""
    return ContactRecord(cid, email, full_name, status, tag_ids)


def _parse_tag_id(tag: object) -> int:
    if not isinstance(tag, dict) or not _is_int(tag.get("id")):
        raise _invalid()
    return tag["id"]


def _parse_tag_item(item: object) -> TagRecord:
    """Parse one tag object; require int id and str title/slug."""

    if not isinstance(item, dict):
        raise _invalid()
    tid = item.get("id")
    title = item.get("title")
    slug = item.get("slug")
    if not _is_int(tid) or not isinstance(title, str) or not isinstance(slug, str):
        raise _invalid()
    return TagRecord(tid, title, slug)








