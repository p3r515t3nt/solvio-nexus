"""research.fetch_public_url — fetch a single PUBLIC http(s) URL, return text.

Read-only, stateless, SSRF-guarded. One invocation fetches exactly ONE URL. No
search engine, no browser, no JavaScript, no crawling, no binary downloads.

Website content is DATA only. The node never interprets it; the Mac Core always
classifies research.* results as UNTRUSTED_WEB. Nothing is persisted or logged;
the body lives in RAM for the duration of the request.
"""
from __future__ import annotations

import asyncio
import hashlib
import ssl
from datetime import datetime, timezone

import aiohttp
from pydantic import BaseModel, Field
from yarl import URL

from solvio_node.capabilities.base import (
    Capability,
    ExecutionMode,
    PrivacyClass,
    RiskLevel,
)
from solvio_node.capabilities.research.html_extract import extract_html, strip_tags
from solvio_node.capabilities.research.web_guard import (
    InvalidUrl,
    PublicOnlyResolver,
    SsrfBlocked,
    WebFetchError,
    ip_is_public,
    validate_url,
)

MAX_RAW_BYTES = 512 * 1024      # 512 KiB hard cap on the raw body
MAX_TEXT_CHARS = 200_000        # cap on extracted text
MAX_REDIRECTS = 5
READ_CHUNK = 16_384

DNS_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 10.0
TOTAL_TIMEOUT_S = 15.0          # < capability timeout_s (runtime backstop)
CONNECTION_LIMIT = 4

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
ALLOWED_CONTENT_TYPES = frozenset({
    "text/html", "text/plain", "application/json",
    "application/xhtml+xml", "application/xml", "text/xml",
})
_FIXED_HEADERS = {
    "User-Agent": "SolvioResearch/1.0",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.1",
    "Accept-Encoding": "identity",
    "Accept-Language": "en",
}


class ContentTypeNotAllowed(WebFetchError):
    pass


class TooManyRedirects(WebFetchError):
    pass


class RedirectLoop(WebFetchError):
    pass


class CompressedResponse(WebFetchError):
    pass


class FetchIn(BaseModel):
    url: str = Field(max_length=2048)


class RedirectHop(BaseModel):
    from_url: str
    to_url: str
    status_code: int


class FetchOut(BaseModel):
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    title: str | None = None
    text: str
    sha256: str
    bytes_read: int
    truncated: bool
    redirect_chain: list[RedirectHop] = Field(default_factory=list)
    resolved_peer: str | None = None
    fetched_at: str


class FetchPublicUrlCapability(Capability):
    id = "research.fetch_public_url"
    version = "1"
    description = ("Fetch a single PUBLIC http(s) URL and return extracted text. "
                   "SSRF-guarded, read-only, stateless; results are UNTRUSTED_WEB.")
    risk_level = RiskLevel.SAFE
    privacy_class = PrivacyClass.SYNTHETIC
    execution_mode = ExecutionMode.INLINE
    supports_batch = False
    max_concurrency = 4
    timeout_s = 18.0
    persistent_data = False
    supports_background = True
    input_model = FetchIn
    output_model = FetchOut

    def __init__(self, *, ip_policy=ip_is_public) -> None:
        self._ip_policy = ip_policy

    async def invoke(self, data: BaseModel) -> FetchOut:
        assert isinstance(data, FetchIn)
        return await self._fetch(data.url)

    async def health(self) -> dict:
        return {"id": self.id, "ok": True}

    # -- internals -----------------------------------------------------------
    @staticmethod
    def _peer_ip(resp: aiohttp.ClientResponse) -> str | None:
        conn = resp.connection
        if conn is None or conn.transport is None:
            return None
        transport = conn.transport
        peer = transport.get_extra_info("peername")
        if not peer:  # SSL transports often expose it only on the raw socket
            sock = transport.get_extra_info("socket")
            if sock is not None:
                try:
                    peer = sock.getpeername()
                except OSError:
                    peer = None
        if isinstance(peer, (tuple, list)) and peer:
            return peer[0]
        return None

    def _plan_redirect(self, current_url: URL, location: str, seen: set[str],
                       done_count: int) -> tuple[object, URL]:
        """Validate ONE redirect hop (pure, no I/O). Returns (target, joined_url).

        Every hop is fully revalidated (scheme/port/credentials/ip-literal); a
        hostname that resolves to a private address is additionally caught by the
        PublicOnlyResolver on the next request.
        """
        if done_count >= MAX_REDIRECTS:
            raise TooManyRedirects("too many redirects")
        try:
            nxt = current_url.join(URL(location))
        except (ValueError, TypeError):
            raise InvalidUrl("bad redirect location") from None
        target = validate_url(str(nxt))
        if str(target.url) in seen:
            raise RedirectLoop("redirect loop")
        return target, nxt

    async def _fetch(self, requested_url: str) -> FetchOut:
        target = validate_url(requested_url)  # fail fast before any I/O
        resolver = PublicOnlyResolver(policy=self._ip_policy, dns_timeout=DNS_TIMEOUT_S)
        ssl_ctx = ssl.create_default_context()  # verification ON; never ssl=False
        connector = aiohttp.TCPConnector(
            resolver=resolver, ssl=ssl_ctx,
            use_dns_cache=False, ttl_dns_cache=0, limit=CONNECTION_LIMIT,
        )
        timeout = aiohttp.ClientTimeout(
            total=TOTAL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S,
            sock_connect=CONNECT_TIMEOUT_S, sock_read=READ_TIMEOUT_S,
        )
        redirect_chain: list[RedirectHop] = []
        seen = {str(target.url)}
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, trust_env=False,
            auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
            headers=_FIXED_HEADERS,
        ) as session:
            current = target
            while True:
                async with session.get(current.url, allow_redirects=False) as resp:
                    peer = self._peer_ip(resp)
                    if peer is not None and peer not in resolver.validated:
                        raise SsrfBlocked("connected peer not in validated set")
                    if resp.status in _REDIRECT_STATUSES and resp.headers.get("Location"):
                        nxt_target, nxt = self._plan_redirect(
                            current.url, resp.headers["Location"], seen,
                            len(redirect_chain))
                        redirect_chain.append(RedirectHop(
                            from_url=str(current.url), to_url=str(nxt),
                            status_code=resp.status))
                        seen.add(str(nxt_target.url))
                        current = nxt_target
                        continue
                    reported = peer or next(iter(sorted(resolver.validated)), None)
                    return await self._read(requested_url, str(current.url),
                                            resp, reported, redirect_chain)

    async def _read(self, requested_url: str, final_url: str,
                    resp: aiohttp.ClientResponse, peer: str | None,
                    redirect_chain: list[RedirectHop]) -> FetchOut:
        enc = resp.headers.get("Content-Encoding", "").strip().lower()
        if enc and enc != "identity":
            raise CompressedResponse("unexpected content-encoding")
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype not in ALLOWED_CONTENT_TYPES:
            raise ContentTypeNotAllowed("content-type not allowed")

        chunks: list[bytes] = []
        total = 0
        truncated = False
        async for chunk in resp.content.iter_chunked(READ_CHUNK):
            if not chunk:
                continue
            if total + len(chunk) > MAX_RAW_BYTES:
                chunks.append(chunk[: MAX_RAW_BYTES - total])
                total = MAX_RAW_BYTES
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        sha = hashlib.sha256(raw).hexdigest()

        charset = resp.charset or "utf-8"
        try:
            decoded = raw.decode(charset, errors="replace")
        except (LookupError, ValueError):
            decoded = raw.decode("utf-8", errors="replace")

        title: str | None = None
        if ctype in ("text/html", "application/xhtml+xml"):
            title, text = extract_html(decoded)
        elif ctype in ("application/xml", "text/xml"):
            text = strip_tags(decoded)
        else:  # text/plain, application/json
            text = decoded
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
            truncated = True

        return FetchOut(
            requested_url=requested_url, final_url=final_url,
            status_code=resp.status, content_type=ctype, title=title, text=text,
            sha256=sha, bytes_read=len(raw), truncated=truncated,
            redirect_chain=redirect_chain, resolved_peer=peer,
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
