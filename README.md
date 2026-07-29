# Soccer Price Discrepancy Scanner

A read-only FastAPI MVP that compares executable Kalshi and Polymarket YES asks
independently against OddsPapi sportsbook implied probabilities for pregame,
90-minute soccer match-winner markets. It never trades or bets.

## Quick start

Python 3.12 is required.

```bash
cp .env.example .env
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
python -m app.scripts.seed_mock_data
uvicorn app.main:app --reload
```

Mock mode requires no external credentials. Visit `/docs`, `/health`,
`/bookmakers`, `/events`, `/opportunities`, `/settings`, and
`/connector-health`.

For PostgreSQL and Redis:

```bash
docker compose up -d postgres redis
alembic upgrade head
```

## Calculation and qualification

The scanner computes `implied_probability = 1 / decimal_odds`, without vig
removal, then `(executable_yes_ask - implied_probability) * 100`. The units are
percentage points and the threshold is inclusive. It never substitutes bid,
midpoint, last trade, an average provider price, or an average bookmaker price.
Kalshi's YES ask may be derived as `1 - best NO bid`, using that NO level's size.

Automatic opportunities require exact or approved-alias event matching, matching
home/away order, kickoff within ten minutes, compatible regulation-only
settlement, open pregame markets, configured time window, fresh quotes, minimum
ask size, and an enabled/available bookmaker.

## Environment

Every variable is also present in `.env.example`.

| Variable | Meaning / default |
|---|---|
| `APP_ENV` | Environment name (`development`) |
| `MOCK_MODE` | Deterministic providers; no credentials (`true`) |
| `DATABASE_URL` | SQLAlchemy async PostgreSQL URL |
| `REDIS_URL` | Redis connection URL |
| `ODDSPAPI_API_KEY` | OddsPapi v5 key, live mode only |
| `ODDSPAPI_BASE_URL` | Official localized v5 base URL |
| `KALSHI_API_KEY` | Kalshi key, live mode only |
| `KALSHI_PRIVATE_KEY_PATH` | Kalshi signing key path, live mode only |
| `POLYMARKET_API_KEY` | Polymarket key, live mode only |
| `TELEGRAM_BOT_TOKEN` | Optional Telegram bot token |
| `TELEGRAM_CHAT_ID` | Optional Telegram destination |
| `ENABLED_BOOKMAKERS` | Six comma-separated canonical IDs |
| `EDGE_THRESHOLD_PP` | Inclusive edge threshold (`3.0`) |
| `MAX_PREDICTION_PRICE_AGE_SECONDS` | Prediction quote maximum age (`20`) |
| `MAX_SPORTSBOOK_PRICE_AGE_SECONDS` | Sportsbook quote maximum age (`60`) |
| `MIN_KALSHI_ASK_SIZE` | Minimum Kalshi executable contracts (`100`) |
| `MIN_POLYMARKET_ASK_SIZE` | Minimum Polymarket executable size (`100`) |
| `MIN_MINUTES_BEFORE_KICKOFF` | Earliest pregame boundary (`10`) |
| `MAX_HOURS_BEFORE_KICKOFF` | Furthest pregame boundary (`72`) |
| `ALERT_COOLDOWN_MINUTES` | Duplicate alert cooldown (`10`) |
| `REALERT_EDGE_INCREASE_PP` | Early re-alert edge improvement (`1.0`) |
| `ODDSPAPI_POLL_INTERVAL_SECONDS` | Future REST poll interval (`30`) |

Secrets are never returned by `/settings` or printed by commands.

## OddsPapi status

The REST client uses the current official v5 contract:
`https://v5.oddspapi.io/en`, query parameter `apiKey`, and catalog endpoint
`GET /bookmakers`. Provider slugs remain separate from canonical IDs.

Run credentialed coverage verification:

```bash
MOCK_MODE=false python -m app.scripts.verify_oddspapi_bookmakers
```

Coverage is deliberately **unverified** until this succeeds against the actual
account catalog. Unknown provider names are counted and never silently converted
to another bookmaker. Missing bookmakers get no zero, null-as-zero, fallback, or
fabricated quote.

TODO: validate credentialed soccer fixture, regular-time 1X2 market, and current
odds payloads before implementing their live normalized mapper. Live Kalshi and
Polymarket transports are likewise future work; typed boundaries and mocks are
included now. Mock mode is the only scanning mode in this milestone.

## Development

```bash
make format
make lint
make typecheck
make test
```

The PostgreSQL schema covers bookmakers and aliases, competitions and aliases,
teams and aliases, canonical/provider events and markets, mappings, both quote
types, opportunities, alert history, system settings, and connector health.
Redis is intended for current scan state and short-lived alert deduplication;
mock mode uses deterministic in-process state.
