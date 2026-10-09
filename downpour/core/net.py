"""HTTP plumbing on top of urllib: openers, ranged requests, header parsing.

urllib is used rather than a third-party client because it already knows the
Windows system proxy settings, follows redirects, handles cookies and auth,
and verifies TLS against the Windows certificate store - and it adds nothing
to the supply chain.
"""

from __future__ import annotations

import http.client
import http.cookiejar
import re
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from downpour.core import filenames
from downpour.core.settings import DEFAULT_USER_AGENT

# Failures worth retrying: the network or the server may recover.
NETWORK_ERRORS = (OSError, http.client.HTTPException, urllib.error.URLError)
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})

_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)
_UNSATISFIED_RANGE = re.compile(r"bytes\s+\*/(\d+)", re.I)

_ssl_context: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context()
    return _ssl_context


class HttpError(Exception):
    def __init__(self, status: int, reason: str, url: str, total: int | None = None, challenge: bool = False):
        super().__init__(f"HTTP {status} {reason}".strip())
        self.status = status
        self.reason = reason
        self.url = url
        self.total = total  # from "Content-Range: bytes */N" on a 416
        self.challenge = challenge  # a bot check (such as Cloudflare's) refused us

    @property
    def transient(self) -> bool:
        return self.status in TRANSIENT_STATUS


def describe(error: BaseException) -> str:
    """A one-line, human explanation of a download failure."""
    if isinstance(error, HttpError):
        if error.challenge:
            return (f"{error} - the site's bot protection turned Downpour away. "
                    "Choose Download with the browser instead.")
        hints = {
            401: "the server wants a user name and password",
            403: "access denied; the link may have expired - try Refresh address",
            404: "the file is not on the server any more",
            410: "the file has been removed from the server",
            416: "the server rejected the byte range; the file may have changed",
        }
        hint = hints.get(error.status)
        return f"{error} - {hint}" if hint else str(error)
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return f"Secure connection failed: {reason.verify_message or reason}"
        if isinstance(reason, socket.gaierror):
            return "Could not find the server - check the address and your connection"
        return describe(reason) if isinstance(reason, BaseException) else str(reason)
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "The connection timed out"
    if isinstance(error, ConnectionResetError):
        return "The server closed the connection"
    if isinstance(error, http.client.IncompleteRead):
        return "The server stopped sending data early"
    return str(error) or error.__class__.__name__


def make_cookie(raw: dict) -> http.cookiejar.Cookie:
    """Convert a browser-extension cookie object into a cookiejar Cookie."""
    host_only = bool(raw.get("hostOnly", False))
    domain = str(raw.get("domain", ""))
    if not host_only and domain and not domain.startswith("."):
        domain = "." + domain
    expires = raw.get("expirationDate")
    return http.cookiejar.Cookie(
        version=0,
        name=str(raw.get("name", "")),
        value=str(raw.get("value", "")),
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=not host_only,
        domain_initial_dot=domain.startswith("."),
        path=str(raw.get("path") or "/"),
        path_specified=True,
        secure=bool(raw.get("secure", False)),
        expires=int(expires) if expires else None,
        discard=not expires,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None} if raw.get("httpOnly") else {},
        rfc2109=False,
    )


def build_opener(
    *,
    url: str,
    cookies: list[dict] | None = None,
    username: str = "",
    password: str = "",
    proxy_mode: str = "system",
    proxy_url: str = "",
) -> urllib.request.OpenerDirector:
    handlers: list = []
    if proxy_mode == "none":
        handlers.append(urllib.request.ProxyHandler({}))
    elif proxy_mode == "manual" and proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    else:
        handlers.append(urllib.request.ProxyHandler())  # environment, then the Windows registry

    # Host-only cookies must not leak to subdomains (browsers enforce this).
    policy = http.cookiejar.DefaultCookiePolicy(
        strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain
    )
    jar = http.cookiejar.CookieJar(policy)
    for raw in cookies or []:
        try:
            jar.set_cookie(make_cookie(raw))
        except (TypeError, ValueError):
            continue
    handlers.append(urllib.request.HTTPCookieProcessor(jar))
    handlers.append(urllib.request.HTTPSHandler(context=ssl_context()))
    if username:
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, url, username, password)
        handlers.append(urllib.request.HTTPBasicAuthHandler(manager))
        handlers.append(urllib.request.HTTPDigestAuthHandler(manager))
    return urllib.request.build_opener(*handlers)


@dataclass
class ResponseInfo:
    status: int
    url: str  # after redirects
    start: int  # offset of the first body byte within the file
    length: int | None  # body length, if declared
    total: int | None  # full file size, if known
    ranged: bool  # a 206 whose Content-Range we can trust
    accept_ranges: bool
    filename: str | None  # from Content-Disposition
    disposition: str | None
    mime: str
    etag: str
    last_modified: str


def parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    if not value:
        return None
    m = _CONTENT_RANGE.search(value)
    if not m:
        return None
    total = None if m.group(3) == "*" else int(m.group(3))
    return int(m.group(1)), int(m.group(2)), total


def response_info(resp: http.client.HTTPResponse) -> ResponseInfo:
    headers = resp.headers
    status = resp.status
    length_header = headers.get("Content-Length")
    try:
        length = int(length_header) if length_header is not None else None
    except ValueError:
        length = None
    encoded = (headers.get("Content-Encoding") or "identity").lower() not in ("identity", "")
    if encoded:
        length = None  # the declared length is of the encoded body
    content_range = parse_content_range(headers.get("Content-Range"))
    if status == 206 and content_range:
        start, last, total = content_range
        length = last - start + 1
        ranged = True
    else:
        start, ranged = 0, False
        total = length
    disposition = headers.get("Content-Disposition")
    return ResponseInfo(
        status=status,
        url=resp.geturl() if hasattr(resp, "geturl") else resp.url,  # type: ignore[attr-defined]
        start=start,
        length=length,
        total=total,
        ranged=ranged and not encoded,
        accept_ranges="bytes" in (headers.get("Accept-Ranges") or "").lower() and not encoded,
        filename=filenames.from_content_disposition(disposition),
        disposition=disposition,
        mime=(headers.get("Content-Type") or "").split(";")[0].strip(),
        etag=headers.get("ETag") or "",
        last_modified=headers.get("Last-Modified") or "",
    )


def open_range(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    start: int | None,
    end: int | None = None,
    user_agent: str = "",
    referrer: str = "",
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[http.client.HTTPResponse, ResponseInfo]:
    """GET ``url``, asking for bytes ``[start, end)`` when start is not None.

    Raises HttpError for any status of 400 and above.
    """
    hdrs = {
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Accept": "*/*",
        # Byte ranges are only meaningful on the unencoded representation.
        "Accept-Encoding": "identity",
    }
    if referrer:
        hdrs["Referer"] = referrer
    if start is not None:
        hdrs["Range"] = f"bytes={start}-" + (str(end - 1) if end is not None else "")
    for key, value in (headers or {}).items():
        if key.lower() not in ("range", "accept-encoding", "host", "content-length"):
            hdrs[key] = value
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        total = None
        headers = e.headers
        m = _UNSATISFIED_RANGE.search(headers.get("Content-Range", "") if headers else "")
        if m:
            total = int(m.group(1))
        challenge = bool(headers) and (
            bool(headers.get("cf-mitigated"))
            or (e.code in (403, 429, 503) and "cloudflare" in (headers.get("Server") or "").lower()
                and "text/html" in (headers.get("Content-Type") or ""))
        )
        e.close()
        raise HttpError(e.code, e.reason or "", url, total, challenge) from None
    return resp, response_info(resp)


def pick_name(info: ResponseInfo, original_url: str) -> str:
    """The file name for a response: the server's, else the URL's."""
    if info.filename:
        return filenames.sanitize(info.filename)
    # The URL after redirects usually names the real file; fall back to the
    # original link when the final one is an opaque token.
    for url in (info.url, original_url):
        name = filenames.choose(url, None, info.mime)
        if "." in name.strip("."):
            return name
    return filenames.choose(info.url, None, info.mime)


@dataclass
class Probe:
    filename: str
    size: int | None
    mime: str
    resumable: bool
    final_url: str


def probe(
    url: str,
    *,
    cookies: list[dict] | None = None,
    referrer: str = "",
    user_agent: str = "",
    headers: dict[str, str] | None = None,
    username: str = "",
    password: str = "",
    proxy_mode: str = "system",
    proxy_url: str = "",
    timeout: float = 15.0,
) -> Probe:
    """Ask the server about a file without downloading it.

    Requests a single byte rather than using HEAD, which many download
    servers answer wrongly or not at all.
    """
    opener = build_opener(
        url=url, cookies=cookies, username=username, password=password,
        proxy_mode=proxy_mode, proxy_url=proxy_url,
    )
    kwargs = dict(user_agent=user_agent, referrer=referrer, headers=headers, timeout=timeout)
    try:
        resp, info = open_range(opener, url, start=0, end=1, **kwargs)
    except HttpError as e:
        if e.status != 416:
            raise
        resp, info = open_range(opener, url, start=None, **kwargs)  # an empty file
    resp.close()
    size = info.total if info.ranged else info.length
    return Probe(
        filename=pick_name(info, url),
        size=size,
        mime=info.mime,
        resumable=size is not None and (info.ranged or info.accept_ranges),
        final_url=info.url,
    )


def abort(resp: object) -> None:
    """Unblock a thread stuck reading ``resp`` by shutting its socket down.

    Closing a response from another thread is not safe; shutting the socket
    down is, and makes the blocked read return at once.
    """
    try:
        sock = resp.fp.raw._sock  # type: ignore[attr-defined]
    except AttributeError:
        return
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except (OSError, TypeError):
        pass


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def safe_url(url: str) -> str:
    """``url`` without any user:password part, for logs and file metadata."""
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return parts._replace(netloc=netloc).geturl()
