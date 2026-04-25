"""Tests for the Kalshi REST client. HTTP transport mocked via respx."""

from __future__ import annotations

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from trumpbot.kalshi.client import DEFAULT_BASE_URL, KalshiClient
from trumpbot.kalshi.exceptions import StateError, TransientError, ValidationError


@pytest.fixture()
def client(rsa_private_key: RSAPrivateKey) -> KalshiClient:
    return KalshiClient(
        api_key_id="key-1",
        private_key=rsa_private_key,
        rate_per_sec=10000.0,
        burst=1000.0,
        max_retries=2,
    )


@respx.mock
async def test_list_markets_pagination(client: KalshiClient) -> None:
    base = DEFAULT_BASE_URL
    page1 = {
        "markets": [
            {"ticker": "T1", "status": "open"},
            {"ticker": "T2", "status": "open"},
        ],
        "cursor": "abc",
    }
    page2 = {
        "markets": [{"ticker": "T3", "status": "open"}],
        "cursor": None,
    }
    respx.get(f"{base}/markets").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )
    out = await client.iter_markets(series_ticker="KX")
    await client.aclose()
    tickers = [m.ticker for m in out]
    assert tickers == ["T1", "T2", "T3"]


@respx.mock
async def test_get_market(client: KalshiClient) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/markets/T1").mock(
        return_value=httpx.Response(
            200,
            json={"market": {"ticker": "T1", "title": "x", "status": "open"}},
        )
    )
    m = await client.get_market("T1")
    await client.aclose()
    assert m.ticker == "T1"


@respx.mock
async def test_get_orderbook(client: KalshiClient) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/markets/T1/orderbook").mock(
        return_value=httpx.Response(
            200,
            json={"orderbook": {"yes": [[50, 100]], "no": [[49, 200]]}},
        )
    )
    ob = await client.get_orderbook("T1")
    await client.aclose()
    assert ob.yes == [[50, 100]]


@respx.mock
async def test_get_balance(client: KalshiClient) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/portfolio/balance").mock(
        return_value=httpx.Response(200, json={"balance": 50000})
    )
    b = await client.get_balance()
    await client.aclose()
    assert b.balance == 50000


@respx.mock
async def test_5xx_retried_then_succeeds(client: KalshiClient) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/markets/T1").mock(
        side_effect=[
            httpx.Response(503, text="retry me"),
            httpx.Response(
                200,
                json={"market": {"ticker": "T1", "title": "x", "status": "open"}},
            ),
        ]
    )
    m = await client.get_market("T1")
    await client.aclose()
    assert m.ticker == "T1"


@respx.mock
async def test_5xx_persists_raises_transient(client: KalshiClient) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/markets/T1").mock(return_value=httpx.Response(503, text="boom"))
    with pytest.raises(TransientError):
        await client.get_market("T1")
    await client.aclose()


@respx.mock
async def test_4xx_raises_validation(client: KalshiClient) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/markets/T1").mock(
        return_value=httpx.Response(400, json={"error": "bad request"})
    )
    with pytest.raises(ValidationError):
        await client.get_market("T1")
    await client.aclose()


@respx.mock
async def test_state_error_keywords(client: KalshiClient) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/portfolio/balance").mock(
        return_value=httpx.Response(403, json={"error": "account_suspended"})
    )
    with pytest.raises(StateError):
        await client.get_balance()
    await client.aclose()


@respx.mock
async def test_schema_mismatch_raises_validation(client: KalshiClient) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/markets/T1").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"}),
    )
    with pytest.raises(ValidationError):
        await client.get_market("T1")
    await client.aclose()


@respx.mock
async def test_429_treated_as_transient(client: KalshiClient) -> None:
    respx.get(f"{DEFAULT_BASE_URL}/markets/T1").mock(
        return_value=httpx.Response(429, text="slow down")
    )
    with pytest.raises(TransientError):
        await client.get_market("T1")
    await client.aclose()


@respx.mock
async def test_signed_headers_present_on_request(client: KalshiClient) -> None:
    route = respx.get(f"{DEFAULT_BASE_URL}/portfolio/balance").mock(
        return_value=httpx.Response(200, json={"balance": 0})
    )
    await client.get_balance()
    await client.aclose()
    sent = route.calls[0].request
    assert sent.headers["KALSHI-ACCESS-KEY"] == "key-1"
    assert sent.headers["KALSHI-ACCESS-TIMESTAMP"]
    assert sent.headers["KALSHI-ACCESS-SIGNATURE"]
