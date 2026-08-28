"""
Minimal HTTP client for the Cloudflare Gateway API, built only on the
standard library — but structured the way `requests`/`urllib3` would:
a Session that keeps one connection alive across calls, a Response
object instead of a raw tuple, and a Retry policy object instead of
scattered constants.

Public surface used by the rest of the app (unchanged, so callers in
cloudflare.py don't need to change): cloudflare_gateway_request(),
retry(), retry_config, rate_limited_request(), RateLimiter, and the
HTTPException / RateLimitException / NotFoundException exceptions.
"""

import ssl
import gzip
import json
import time
import random
import atexit
import http.client
import socket
import zlib
from io import BytesIO
from functools import wraps
from typing import Optional, Tuple
from urllib.parse import urlparse, urljoin
from src import info, silent_error, error, CF_IDENTIFIER, CF_API_TOKEN

CLOUDFLARE_HOST = "api.cloudflare.com"

# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------
class HTTPException(Exception):
    pass

class RateLimitException(HTTPException):
    def __init__(self, message, retry_after: Optional[int] = None):
        super().__init__(message)
        # Seconds Cloudflare told us to wait (from the Retry-After header),
        # when it gave us one. None means the caller falls back to a
        # fixed default.
        self.retry_after = retry_after

class NotFoundException(HTTPException):
    """Raised on 404: the list/rule id in our cache no longer exists on
    Cloudflare (e.g. deleted manually from the dashboard). Recoverable —
    callers should evict the stale id from cache and recreate the resource
    instead of treating this as a fatal error."""
    pass


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    """Cloudflare's Retry-After header is a whole number of seconds
    (rounded up). Falls back to None if missing/unparseable so the
    caller can use its own default instead of guessing a bad value."""
    if not value:
        return None
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Response — a small stand-in for requests.Response
# ----------------------------------------------------------------------
class Response:
    def __init__(self, status_code: int, reason: str, header_pairs, raw_content: bytes):
        self.status_code = status_code
        self.reason = reason
        self._headers = header_pairs  # list of (name, value), original case
        self._decoded_content: Optional[bytes] = None
        self.raw_content = raw_content

    def get_header(self, name: str, default=None):
        name = name.lower()
        for key, value in self._headers:
            if key.lower() == name:
                return value
        return default

    @property
    def content(self) -> bytes:
        """Body bytes after undoing Content-Encoding, if any."""
        if self._decoded_content is not None:
            return self._decoded_content

        data = self.raw_content
        encoding = self.get_header("Content-Encoding")
        if data and encoding == "gzip":
            with gzip.GzipFile(fileobj=BytesIO(data)) as f:
                data = f.read()
        elif data and encoding == "deflate":
            data = zlib.decompress(data)

        self._decoded_content = data
        return data

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="ignore")

    def json(self) -> dict:
        return json.loads(self.content.decode("utf-8"))

    @property
    def ok(self) -> bool:
        return self.status_code < 400


# ----------------------------------------------------------------------
# Session — pooled, keep-alive HTTP client shared across the app
# ----------------------------------------------------------------------
class Session:
    """A minimal requests.Session-alike: keep-alive connections pooled
    per host and reused across calls, instead of paying a fresh TLS
    handshake for every request. Shared by both the Cloudflare API
    client and the adlist/whitelist downloader — hitting the same host
    twice (Cloudflare itself, or two adlists on the same raw file host)
    reuses one connection instead of opening a new one each time.

    A pooled connection can be closed by the server or the OS between
    calls without us knowing until we try to use it again — so a
    request is allowed exactly one silent reconnect-and-retry per host
    before the failure is surfaced to the caller.
    """

    # Errors that specifically mean "the connection I was holding is no
    # longer good" — worth a silent reconnect. Checked first (see
    # _NETWORK_ERRORS below) since ConnectionResetError/BrokenPipeError
    # are themselves OSError subclasses.
    _STALE_CONNECTION_ERRORS = (
        http.client.RemoteDisconnected,
        ConnectionResetError,
        BrokenPipeError,
    )

    # Every other network-level failure: DNS resolution, connection
    # refused, TLS handshake failure, timeout, etc. These aren't worth
    # retrying instantly (unlike a stale keep-alive connection), but
    # they still deserve a clear, logged message that names the method
    # and URL — the same clarity plain http.client gives you at the
    # call site — rather than a bare exception bubbling up silently.
    _NETWORK_ERRORS = (http.client.HTTPException, ssl.SSLError, socket.timeout, OSError)

    def __init__(self, default_timeout: int = 20):
        self.default_timeout = default_timeout
        self._connections: dict = {}  # (scheme, netloc) -> HTTPConnection

    def _get_connection(self, scheme: str, netloc: str, timeout: int):
        key = (scheme, netloc)
        conn = self._connections.get(key)
        if conn is None:
            if scheme == "https":
                context = ssl.create_default_context()
                conn = http.client.HTTPSConnection(netloc, context=context, timeout=timeout)
            else:
                conn = http.client.HTTPConnection(netloc, timeout=timeout)
            self._connections[key] = conn
        return conn

    def _drop_connection(self, scheme: str, netloc: str):
        conn = self._connections.pop((scheme, netloc), None)
        if conn is not None:
            conn.close()

    def close(self):
        for conn in self._connections.values():
            conn.close()
        self._connections.clear()

    def _request_once(self, scheme, netloc, method, path, body, headers, timeout) -> Response:
        display_url = f"{scheme}://{netloc}{path}"
        for attempt in (1, 2):
            conn = self._get_connection(scheme, netloc, timeout)
            try:
                conn.request(method, path, body, headers)
                raw_response = conn.getresponse()
                data = raw_response.read()
                return Response(raw_response.status, raw_response.reason,
                                 raw_response.getheaders(), data)
            except self._STALE_CONNECTION_ERRORS as e:
                self._drop_connection(scheme, netloc)
                if attempt == 2:
                    message = f"Network error while requesting {method} {display_url}: {e}"
                    silent_error(message)
                    raise HTTPException(message) from e
                # else: loop once more on a freshly (re)opened connection
            except self._NETWORK_ERRORS as e:
                # A connection that just failed like this shouldn't stay
                # pooled for the next call to trip over again.
                self._drop_connection(scheme, netloc)
                message = f"Network error while requesting {method} {display_url}: {e}"
                silent_error(message)
                raise HTTPException(message) from e

        raise HTTPException("Unreachable: Session._request_once retry loop exited without a result")

    def request(self, method: str, url: str, body=None, headers=None, timeout=None,
                follow_redirects: bool = False, max_redirects: int = 5) -> Response:
        timeout = timeout or self.default_timeout
        headers = dict(headers or {})
        redirects = 0

        while True:
            parsed = urlparse(url)
            scheme = parsed.scheme or "https"
            netloc = parsed.netloc
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"

            response = self._request_once(scheme, netloc, method, path, body, headers, timeout)

            if follow_redirects and response.status_code in (301, 302, 303, 307, 308):
                location = response.get_header("Location")
                if not location:
                    return response
                redirects += 1
                if redirects > max_redirects:
                    raise HTTPException(f"Too many redirects ({redirects}) while fetching {url}")
                if not urlparse(location).netloc:
                    location = urljoin(url, location)
                url = location
                continue

            return response


# One shared Session for the whole process — connections are pooled per
# host, so the Cloudflare API client and the adlist/whitelist downloader
# both benefit from keep-alive reuse instead of managing their own
# connections separately.
_shared_session = Session(default_timeout=20)
atexit.register(_shared_session.close)


def get_session() -> Session:
    return _shared_session


def cloudflare_gateway_request(
    method: str, endpoint: str,
    body: Optional[str] = None,
    timeout: int = 20
) -> Tuple[int, dict]:
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip, deflate"
    }

    path = f"/client/v4/accounts/{CF_IDENTIFIER}/gateway{endpoint}"
    url = f"https://{CLOUDFLARE_HOST}{path}"

    # Network-level failures (DNS, connection refused, TLS, timeout,
    # stale keep-alive connection) are already turned into a clear,
    # logged HTTPException by Session — nothing to catch here.
    response = get_session().request(method, url, body=body, headers=headers, timeout=timeout)

    if response.status_code >= 400:
        error_message = (
            f"Request failed: {response.status_code} {response.reason}, "
            f"Body: {response.text} "
            f"for URL: {url}"
        )
        if response.status_code == 429:
            retry_after = _parse_retry_after(response.get_header("Retry-After"))
            silent_error(error_message)
            raise RateLimitException(error_message, retry_after=retry_after)
        elif response.status_code == 404:
            silent_error(error_message)
            raise NotFoundException(error_message)
        elif response.status_code in (400, 403):
            error(error_message)
        else:
            silent_error(error_message)
        raise HTTPException(error_message)

    try:
        return response.status_code, response.json()
    except json.JSONDecodeError:
        error_message = "Failed to decode JSON response"
        silent_error(error_message)
        raise HTTPException(error_message)


# ----------------------------------------------------------------------
# Retry policy — modeled loosely on urllib3.util.retry.Retry: one object
# owns "how many attempts" and "how long to back off", instead of that
# logic being spread across free functions and magic numbers.
# ----------------------------------------------------------------------
class Retry:
    def __init__(self, total: int = 5, backoff_factor: float = 1.0, max_backoff: float = 10.0):
        self.total = total
        self.backoff_factor = backoff_factor
        self.max_backoff = max_backoff

    def is_exhausted(self, attempt_number: int) -> bool:
        return attempt_number >= self.total

    def get_backoff_time(self, attempt_number: int) -> float:
        exponent = max(attempt_number - 1, 0)
        return min(self.backoff_factor * (2 ** random.uniform(0, exponent)), self.max_backoff)


# Cloudflare Gateway CI jobs run with a 30-minute timeout — if 429s never
# stopped retrying, a persistent rate limit would hang until GitHub kills
# the job instead of failing with a clear error. 10 attempts (the first
# 120s/Retry-After cooldown plus up to nine 10s-capped backoffs, ~3-4
# minutes total) gives Cloudflare a real chance to recover while staying
# well inside that timeout.
DEFAULT_RETRY = Retry(total=5, backoff_factor=1.0, max_backoff=10.0)
RATE_LIMIT_RETRY = Retry(total=10, backoff_factor=1.0, max_backoff=10.0)


def retry_if_exception_type(exceptions):
    return lambda e: isinstance(e, exceptions)


def custom_stop_condition(exception, attempt_number):
    if isinstance(exception, RateLimitException):
        return RATE_LIMIT_RETRY.is_exhausted(attempt_number)
    return DEFAULT_RETRY.is_exhausted(attempt_number)


# Retry configuration — kept as a plain dict so existing call sites can
# keep doing `@retry(**retry_config)` unchanged.
retry_config = {
    'stop': custom_stop_condition,
    'wait': lambda attempt_number: DEFAULT_RETRY.get_backoff_time(attempt_number),
    'retry': retry_if_exception_type((HTTPException,)),
    'before_sleep': lambda retry_state: info(
        f"[·] Retrying (attempt {retry_state['attempt_number']})"
    )
}


# Retry Decorator
def retry(stop=None, wait=None, retry=None, after=None, before_sleep=None):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt_number = 0
            first_rate_limit_encountered = False  # Cờ để theo dõi lần đầu gặp 429
            while True:
                try:
                    attempt_number += 1
                    return func(*args, **kwargs)
                except NotFoundException:
                    # Not transient — raise immediately so the caller can
                    # evict the stale id from cache and self-heal.
                    raise
                except RateLimitException as e:
                    if not first_rate_limit_encountered:
                        # First time meeting 429: use Cloudflare's own
                        # Retry-After if it gave us one, otherwise fall
                        # back to a fixed 2-minute cooldown.
                        first_rate_limit_encountered = True
                        wait_time = e.retry_after or 120
                        info(f"[·] Rate limited by Cloudflare — sleeping {wait_time}s before retrying")
                        time.sleep(wait_time)
                    else:
                        # Subsequent 429 encounters follow the old retry logic
                        if stop and stop(e, attempt_number):
                            raise
                        if before_sleep:
                            before_sleep({'attempt_number': attempt_number})
                        wait_time = wait(attempt_number) if wait else 1
                        time.sleep(wait_time)
                except Exception as e:
                    if retry and not retry(e):
                        raise
                    if after:
                        after({'attempt_number': attempt_number, 'outcome': e})
                    if stop and stop(e, attempt_number):
                        raise
                    if before_sleep:
                        before_sleep({'attempt_number': attempt_number})
                    wait_time = wait(attempt_number) if wait else 1
                    time.sleep(wait_time)
        return wrapper
    return decorator


# ----------------------------------------------------------------------
# Client-side rate limiter — paces our own outgoing requests so we stay
# under Cloudflare's Gateway Lists limit before it ever returns a 429.
# ----------------------------------------------------------------------
class RateLimiter:
    def __init__(self, interval: float = 1):
        self.interval = interval
        self.timestamp = 0.0  # 0 so the very first call never waits

    def wait_for_next_request(self):
        now = time.time()
        elapsed = now - self.timestamp
        sleep_time = max(0, self.interval - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)
        self.timestamp = time.time()

# Shared across every decorated call, since Cloudflare's rate limit is
# global per API token, not per endpoint. A fresh RateLimiter per call
# would always see elapsed ≈ 0 (timestamp = "now" at construction) and
# sleep the full interval on every single request; one shared instance
# measures elapsed time against the *previous* request, which is what
# the interval is supposed to enforce.
_shared_rate_limiter = RateLimiter(interval=1)

# Rate Limited Request Decorator
def rate_limited_request(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        _shared_rate_limiter.wait_for_next_request()
        return func(*args, **kwargs)
    return wrapper
