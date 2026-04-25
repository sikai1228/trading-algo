# Test fixtures

## `kxtrumpmeet_26apr_response.json`

**Currently a representative synthetic response.** The fixture mirrors
the documented Kalshi `GET /events/{event_ticker}` shape and contains
five sample markets (Putin, Xi, Thune, Netanyahu, Machado) — enough
to exercise the integration path end-to-end including the Unicode
case (María Corina Machado).

To upgrade to a higher-fidelity test, replace this file with the
actual API response captured during the user's manual verification
run:

```bash
# After loading credentials into your env / config:
uv run python -c "
import asyncio, json
from trumpbot.config import load_config
from trumpbot.kalshi.auth import load_private_key
from trumpbot.kalshi.client import KalshiClient
from trumpbot.platform_paths import current_platform_paths, resolve_path

cfg = load_config(current_platform_paths().config_yaml_path)
key = load_private_key(
    resolve_path(cfg.kalshi.private_key_path, current_platform_paths().private_key_path),
    passphrase=cfg.kalshi.private_key_passphrase.encode() if cfg.kalshi.private_key_passphrase else None,
)
async def go():
    async with KalshiClient(api_key_id=cfg.kalshi.api_key_id, private_key=key) as c:
        resp = await c.get_event('KXTRUMPMEET-26APR')
        print(json.dumps(resp.model_dump(mode='json'), indent=2))
asyncio.run(go())
" > tests/fixtures/kxtrumpmeet_26apr_response.json
```

The integration test in `tests/test_market_discovery_service.py`
asserts five markets exist in the synthetic file. After replacing
with a real response, update the assertions in that file to match
the actual market count and tickers.
