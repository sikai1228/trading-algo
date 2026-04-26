"""KalshiClient: typed, rate-limited, signed REST client for Phase 1.

Phase 1 only exposes read endpoints (markets, events, orderbook,
balance, candlesticks). Order placement methods are deliberately
absent until Phase 4.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
import pydantic
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from trumpbot.kalshi.auth import build_auth_headers
from trumpbot.kalshi.exceptions import (
    KalshiError,
    StateError,
    TransientError,
    ValidationError,
)
from trumpbot.kalshi.rate_limit import TokenBucket, rate_limited
from trumpbot.kalshi.schemas import (
    KalshiBalance,
    KalshiCandlesticksResponse,
    KalshiEventResponse,
    KalshiMarket,
    KalshiMarketListResponse,
    KalshiMarketResponse,
    KalshiOrderbook,
    KalshiOrderbookResponse,
)
from trumpbot.utils.logging import get_logger

T = TypeVar("T", bound=pydantic.BaseModel)

log = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
# Kalshi documents 100 reads/sec on the standard tier; cap at 80 % per spec.
DEFAULT_RATE_PER_SEC = 80.0
DEFAULT_BURST = 40.0

# State-error keywords surfaced in Kalshi 4xx error bodies.
_STATE_ERROR_KEYWORDS = (
    "insufficient_funds",
    "insufficient funds",
    "market_closed",
    "market is closed",
    "account_suspended",
    "account suspended",
    "trading_halted",
    "trading halted",
)


class KalshiClient:
    """Read-only REST client.

    Construct with an authenticated context (api_key_id + RSA private
    key) for endpoints that require auth, or omit auth for the small
    set of public endpoints. Phase 1 calls everything authenticated for
    consistency with future phases.
    """

    def __init__(
        self,
        *,
        api_key_id: str,
        private_key: RSAPrivateKey,
        base_url: str = DEFAULT_BASE_URL,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        burst: float = DEFAULT_BURST,
        rate_limit_pct: float = 0.8,
        http_client: httpx.AsyncClient | None = None,
        timeout_sec: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        self._api_key_id = api_key_id
        self._private_key = private_key
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_sec)
        self._rate_limiter = TokenBucket(
            rate_per_sec=rate_per_sec * rate_limit_pct,
            capacity=burst,
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # -- public endpoints --------------------------------------------------

    @rate_limited()
    async def list_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = "open",
        limit: int = 1000,
        cursor: str | None = None,
    ) -> KalshiMarketListResponse:
        """One page of markets. Use ``iter_markets`` for full pagination."""
        params: dict[str, Any] = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets", params=params, model=KalshiMarketListResponse)

    async def iter_markets(
        self,
        *,
        series_ticker: str | None = None,
        status: str | None = "open",
        page_limit: int = 1000,
    ) -> list[KalshiMarket]:
        """Iterate every page of /markets and return the flattened list."""
        out: list[KalshiMarket] = []
        cursor: str | None = None
        while True:
            page = await self.list_markets(
                series_ticker=series_ticker,
                status=status,
                limit=page_limit,
                cursor=cursor,
            )
            out.extend(page.markets)
            if not page.cursor or page.cursor == cursor:
                break
            cursor = page.cursor
        return out

    @rate_limited()
    async def get_market(self, ticker: str) -> KalshiMarket:
        path = f"/markets/{ticker}"
        resp = await self._request("GET", path, model=KalshiMarketResponse)
        return resp.market

    @rate_limited()
    async def get_event(self, event_ticker: str) -> KalshiEventResponse:
        return await self._request(
            "GET",
            f"/events/{event_ticker}",
            model=KalshiEventResponse,
        )

    @rate_limited()
    async def get_orderbook(self, ticker: str) -> KalshiOrderbook:
        resp = await self._request(
            "GET",
            f"/markets/{ticker}/orderbook",
            model=KalshiOrderbookResponse,
        )
        return resp.orderbook

    @rate_limited()
    async def get_balance(self) -> KalshiBalance:
        return await self._request("GET", "/portfolio/balance", model=KalshiBalance)

    @rate_limited()
    async def get_market_candlesticks(
        self,
        ticker: str,
        *,
        period_interval: int,
        start_ts: int,
        end_ts: int,
        series_ticker: str | None = None,
    ) -> KalshiCandlesticksResponse:
        if series_ticker is None:
            market = await self.get_market(ticker)
            if not market.series_ticker:
                raise ValidationError(
                    f"cannot resolve series_ticker for {ticker}",
                    request_method="GET",
                    request_path=f"/markets/{ticker}",
                )
            series_ticker = market.series_ticker
        path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
        params: dict[str, Any] = {
            "period_interval": period_interval,
            "start_ts": start_ts,
            "end_ts": end_ts,
        }
        return await self._request("GET", path, params=params, model=KalshiCandlesticksResponse)

    # -- core HTTP --------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        model: type[T],
    ) -> T:
        url = f"{self._base_url}{path}"
        sign_path = f"/trade-api/v2{path}"

        async def attempt() -> T:
            headers = build_auth_headers(
                api_key_id=self._api_key_id,
                private_key=self._private_key,
                method=method,
                path=sign_path,
            ).as_dict()
            try:
                response = await self._http.request(method, url, params=params, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                raise TransientError(
                    f"network error: {exc!r}",
                    request_method=method,
                    request_path=path,
                ) from exc
            return self._parse_response(response, method=method, path=path, model=model)

        return await self._with_retries(attempt, method=method, path=path)

    async def _with_retries(
        self,
        op: Callable[[], Awaitable[T]],
        *,
        method: str,
        path: str,
    ) -> T:
        delay = 0.25
        last: TransientError | None = None
        for attempt_idx in range(self._max_retries):
            try:
                return await op()
            except TransientError as exc:
                last = exc
                log.warning(
                    "kalshi_transient_error",
                    method=method,
                    path=path,
                    attempt=attempt_idx + 1,
                    error=str(exc),
                )
                if attempt_idx == self._max_retries - 1:
                    break
                await asyncio.sleep(delay)
                delay *= 2
        assert last is not None
        raise last

    def _parse_response(
        self,
        response: httpx.Response,
        *,
        method: str,
        path: str,
        model: type[T],
    ) -> T:
        status = response.status_code
        if 500 <= status < 600:
            raise TransientError(
                f"kalshi {status}",
                status_code=status,
                request_method=method,
                request_path=path,
                response_body=_safe_text(response),
            )
        if status == 429:
            raise TransientError(
                "kalshi 429 rate limited",
                status_code=status,
                request_method=method,
                request_path=path,
                response_body=_safe_text(response),
            )
        if 400 <= status < 500:
            body_text = _safe_text(response)
            if _looks_like_state_error(body_text):
                raise StateError(
                    f"kalshi state error {status}: {body_text[:200]}",
                    status_code=status,
                    request_method=method,
                    request_path=path,
                    response_body=body_text,
                )
            log.error(
                "kalshi_validation_error",
                method=method,
                path=path,
                status=status,
                body=body_text[:1000],
            )
            raise ValidationError(
                f"kalshi {status}: {body_text[:200]}",
                status_code=status,
                request_method=method,
                request_path=path,
                response_body=body_text,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ValidationError(
                f"kalshi response is not valid JSON: {exc!r}",
                status_code=status,
                request_method=method,
                request_path=path,
                response_body=_safe_text(response),
            ) from exc

        try:
            return model.model_validate(payload)
        except pydantic.ValidationError as exc:
            log.error(
                "kalshi_schema_mismatch",
                method=method,
                path=path,
                model=model.__name__,
                errors=exc.errors(),
            )
            raise ValidationError(
                f"kalshi response failed schema validation: {exc.errors()}",
                status_code=status,
                request_method=method,
                request_path=path,
                response_body=payload,
            ) from exc


def _safe_text(response: httpx.Response) -> str:
    try:
        return response.text
    except (UnicodeDecodeError, httpx.ResponseNotRead):
        return ""


def _looks_like_state_error(body_text: str) -> bool:
    lowered = body_text.lower()
    return any(kw in lowered for kw in _STATE_ERROR_KEYWORDS)


__all__ = [
    "KalshiClient",
    "KalshiError",
    "StateError",
    "TransientError",
    "ValidationError",
]
