"""HTTP adapter for the Urja Meter Ops portal.

This is the only module that knows portal URLs, headers and auth mechanics
(documented in PROTOCOL.md). It returns raw portal JSON; turning that into our
own shapes is the job of `app.portal.parsing`.

Responsibilities:
- log in (SvelteKit form action) and keep the better-auth session cookie;
- notice an expired session (three different shapes, see `_session_expired`)
  and log in again exactly once, even under concurrent requests;
- stay under the portal's rate limit and retry 429 / 5xx / network errors;
- sign requests to the bulk export endpoint.
"""

import asyncio
import email.utils
import hashlib
import hmac
import logging
import time
from collections import deque
from typing import Any

import httpx

from app.config import Settings
from app.errors import NotFound, PortalAuthError, PortalError, PortalRateLimited, PortalUnavailable
from app.portal.devalue import SessionExpired, page_data

log = logging.getLogger(__name__)

SESSION_COOKIE = "__Secure-better-auth.session_token"
PORTAL_PAGE_SIZE = 20  # fixed server-side; `pageSize`/`limit` params are ignored


class RateLimiter:
    """Sliding-window limiter: at most `limit` calls in any `window_s` seconds."""

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = limit
        self.window_s = window_s
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.window_s:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                await asyncio.sleep(self.window_s - (now - self._calls[0]))


def sign(secret: str, method: str, path: str, query: str, timestamp: str) -> str:
    """Replicates the portal's in-browser signer (see PROTOCOL.md, 'Bulk export')."""
    message = "\n".join([method, path, query, timestamp]).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class PortalClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=settings.timeout_s,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": "flock-urja-api/1.0"},
        )
        self._limiter = RateLimiter(settings.max_requests_per_minute)
        self._login_lock = asyncio.Lock()
        self._session_generation = 0  # bumped on every successful login
        self._logged_in = False
        self._signing_secret: str | None = None
        self._clock_offset_s = 0.0  # portal clock minus ours, from the Date header
        self.retry_delays_s = (1.0, 3.0, 8.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth

    async def login(self) -> None:
        self._http.cookies.clear()
        response = await self._send(
            "POST",
            "/login",
            data={"email": self._settings.email, "password": self._settings.password},
            # SvelteKit rejects cross-site form posts unless Origin matches the host.
            headers={"Origin": self._base_url, "Accept": "application/json", "x-sveltekit-action": "true"},
        )
        # A wrong password is still HTTP 200; the outcome is in the body.
        try:
            body = response.json()
        except ValueError:
            raise PortalAuthError(f"unexpected login response (HTTP {response.status_code})")
        if body.get("type") != "redirect" or SESSION_COOKIE not in self._http.cookies:
            raise PortalAuthError("portal rejected the configured credentials")
        self._logged_in = True
        self._session_generation += 1
        log.info("logged in to portal")

    async def _ensure_logged_in(self) -> None:
        if not self._logged_in:
            async with self._login_lock:
                if not self._logged_in:
                    await self.login()

    async def _relogin(self, seen_generation: int) -> None:
        """Log in again unless another request already did so since we noticed expiry."""
        async with self._login_lock:
            if self._session_generation == seen_generation:
                log.info("portal session expired; logging in again")
                self._logged_in = False
                await self.login()

    @staticmethod
    def _session_expired(response: httpx.Response) -> bool:
        if response.status_code == 401:
            return _json_or_empty(response).get("error") == "unauthorized"
        if response.is_redirect:
            return response.headers.get("location", "").startswith("/login")
        if response.status_code == 200 and response.request.url.path.endswith("__data.json"):
            body = _json_or_empty(response)
            return body.get("type") == "redirect" and str(body.get("location", "")).startswith("/login")
        return False

    # ------------------------------------------------------------ transport

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One request with throttling and retries for 429 / 5xx / network errors."""
        for attempt in range(self._settings.max_retries + 1):
            await self._limiter.acquire()
            try:
                response = await self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self._settings.max_retries:
                    raise PortalUnavailable(f"portal unreachable: {exc.__class__.__name__}", retry_after=30)
                await self._backoff(attempt, f"network error {exc.__class__.__name__}")
                continue

            self._track_clock(response)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._settings.max_retries:
                    if response.status_code == 429:
                        raise PortalRateLimited("portal rate limit exceeded", retry_after=60)
                    raise PortalUnavailable(f"portal returned HTTP {response.status_code}", retry_after=30)
                await self._backoff(attempt, f"HTTP {response.status_code}")
                continue
            return response
        raise AssertionError("unreachable")

    async def _backoff(self, attempt: int, reason: str) -> None:
        delay = self.retry_delays_s[min(attempt, len(self.retry_delays_s) - 1)]
        log.warning("portal %s; retrying in %.0fs", reason, delay)
        await asyncio.sleep(delay)

    def _track_clock(self, response: httpx.Response) -> None:
        if date := response.headers.get("date"):
            try:
                self._clock_offset_s = email.utils.parsedate_to_datetime(date).timestamp() - time.time()
            except (TypeError, ValueError):
                pass

    async def _authed(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Authenticated request; transparently re-logs in once if the session expired."""
        await self._ensure_logged_in()
        generation = self._session_generation
        response = await self._send(method, path, **kwargs)
        if self._session_expired(response):
            await self._relogin(generation)
            response = await self._send(method, path, **kwargs)
            if self._session_expired(response):
                raise PortalAuthError("portal session rejected immediately after login")
        return response

    async def _get_json(self, path: str, params: dict | None = None) -> Any:
        response = await self._authed("GET", path, params=params)
        if response.status_code == 404:
            raise NotFound(_json_or_empty(response).get("message", "not found"))
        if response.status_code != 200:
            raise PortalError(f"GET {path} returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise PortalError(f"GET {path} returned non-JSON content")

    # ------------------------------------------------------------- endpoints

    async def list_transformers(self) -> list[dict]:
        first = await self._get_json("/portal/dts", {"page": 1})
        rows = list(first["data"])
        pages = -(-first["total"] // PORTAL_PAGE_SIZE)
        for page in range(2, pages + 1):
            rows.extend((await self._get_json("/portal/dts", {"page": page}))["data"])
        return rows

    async def get_meter_page(self, meter_id: str) -> dict:
        """Decoded data of the meter detail page: {meterId, detail, hierarchy}."""
        response = await self._authed("GET", f"/meters/{meter_id}/__data.json")
        if response.status_code != 200:
            raise PortalError(f"meter page returned HTTP {response.status_code}")
        try:
            return page_data(response.json())
        except SessionExpired:  # only if re-login inside _authed did not stick
            raise PortalAuthError("portal session rejected immediately after login")

    async def get_geo(self, meter_id: str) -> dict:
        return (await self._get_json(f"/portal/meters/{meter_id}/geo"))["data"]

    async def get_energy(self, meter_id: str, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
        params = {k: v for k, v in (("from", date_from), ("to", date_to)) if v}
        return (await self._get_json(f"/portal/meters/{meter_id}/energy", params or None))["data"]

    async def export_meters(self) -> list[dict]:
        """All meters with hierarchy and coordinates in one signed request."""
        for attempt in range(2):
            secret = await self._get_signing_secret(refresh=attempt > 0)
            query = "page=1"
            timestamp = str(int(time.time() + self._clock_offset_s))
            headers = {"x-timestamp": timestamp, "x-signature": sign(secret, "GET", "/portal/export", query, timestamp)}
            response = await self._authed("GET", f"/portal/export?{query}", headers=headers)
            if response.status_code == 401 and _json_or_empty(response).get("error") == "signature_invalid":
                log.warning("export signature rejected; refreshing signing secret")
                continue  # secret may have rotated
            if response.status_code != 200:
                raise PortalError(f"export returned HTTP {response.status_code}")
            body = response.json()
            if len(body["data"]) != body.get("total", len(body["data"])):
                raise PortalError("export returned a partial result")
            return body["data"]
        raise PortalError("portal rejected the export signature twice")

    async def _get_signing_secret(self, refresh: bool = False) -> str:
        if self._signing_secret is None or refresh:
            self._signing_secret = (await self._get_json("/portal/keys"))["data"]["signingSecret"]
        return self._signing_secret


def _json_or_empty(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
