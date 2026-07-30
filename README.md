# Soccer Price Discrepancy Scanner

A read-only FastAPI scanner that compares executable Kalshi and Polymarket asks
independently against OddsPapi sportsbook implied probabilities for same-day,
pregame, regulation-time soccer moneyline, total, spread, and BTTS markets. It
never trades or bets.

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
`/connector-health`, and `/market-candidates`.

For PostgreSQL and Redis:

```bash
docker compose up -d postgres redis
alembic upgrade head
```

## Calculation and qualification

The scanner computes `implied_probability = 1 / decimal_odds`, without vig
removal, then `(implied_probability - executable_yes_ask) * 100`. The units are
percentage points and the threshold is inclusive. It never substitutes bid,
midpoint, last trade, an average provider price, or an average bookmaker price.
Kalshi's YES ask may be derived as `1 - best NO bid`, using that NO level's size.

Automatic opportunities require exact or approved-alias event matching, matching
selection and line, compatible regulation-only settlement, same local calendar
date in `CLIENT_TIMEZONE`, future kickoff with the configured buffer, fresh
quotes, spread strictly below the configured maximum, qualifying two-sided depth
inside the midpoint window, verified rolling 24-hour volume, and an
enabled/available bookmaker. Midpoint is used only for depth, never edge.

## Environment

Every variable is also present in `.env.example`.

| Variable | Meaning / default |
|---|---|
| `APP_ENV` | Environment name (`development`) |
| `APP_MODE` | Application orchestration mode (`mock` or `live`) |
| `MOCK_MODE` | Deterministic providers; no credentials (`true`) |
| `DATABASE_URL` | SQLAlchemy async PostgreSQL URL |
| `REDIS_URL` | Redis connection URL |
| `SPORTS_ODDS_API_KEY` | OddsPapi v5 key when its mode is live |
| `SPORTS_ODDS_BASE_URL` | Official localized OddsPapi v5 base URL |
| `KALSHI_API_KEY_ID` | Read-scoped Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | Kalshi signing key path, live mode only |
| `KALSHI_MODE` | `mock`, `live`, or `disabled` |
| `POLYMARKET_MODE` | `mock`, `live`, or `disabled`; no key required |
| `SPORTS_ODDS_MODE` | `mock`, `live`, or `disabled` |
| `LIVE_DRY_RUN` | Suppress all Telegram delivery (`true`) |
| `ALERTS_ENABLED` | Master opportunity-alert switch (`false`) |
| `TELEGRAM_ENABLED` | Telegram delivery switch (`false`) |
| `TELEGRAM_BOT_TOKEN` | Optional Telegram bot token |
| `TELEGRAM_CHAT_ID` | Optional Telegram destination |
| `ENABLED_SPORTS` | Comma-separated canonical sports; currently `soccer` |
| `ENABLED_BOOKMAKERS` | Six comma-separated canonical IDs |
| `CLIENT_TIMEZONE` | Calendar-day timezone (`America/New_York`) |
| `ENABLED_MARKET_TYPES` | `moneyline,total,spread,btts` |
| `MAX_BID_ASK_SPREAD_CENTS` | Strict spread ceiling (`5`) |
| `DEPTH_WINDOW_FROM_MIDPOINT_CENTS` | Two-sided depth window (`3`) |
| `MIN_DEPTH_WITHIN_WINDOW_USD` | Inclusive minimum executable depth (`2000`) |
| `MIN_TRAILING_24H_VOLUME_USD` | Inclusive verified rolling volume (`5000`) |
| `EDGE_THRESHOLD_PP` | Inclusive edge threshold (`3.0`) |
| `MAX_PREDICTION_PRICE_AGE_SECONDS` | Prediction quote maximum age (`20`) |
| `MAX_SPORTSBOOK_PRICE_AGE_SECONDS` | Sportsbook quote maximum age (`60`) |
| `MIN_KALSHI_ASK_SIZE` | Minimum Kalshi executable contracts (`100`) |
| `MIN_POLYMARKET_ASK_SIZE` | Minimum Polymarket executable size (`100`) |
| `MIN_MINUTES_BEFORE_KICKOFF` | Earliest pregame boundary (`10`) |
| `MAX_HOURS_BEFORE_KICKOFF` | Furthest pregame boundary (`72`) |
| `ALERT_COOLDOWN_MINUTES` | Duplicate alert cooldown (`10`) |
| `REALERT_EDGE_INCREASE_PP` | Early re-alert edge improvement (`1.0`) |
| `PRICE_POLL_INTERVAL_SECONDS` | Recurring scan interval (`30`) |
| `DISCOVERY_INTERVAL_SECONDS` | Full discovery interval (`300`) |
| `EVENT_MATCH_KICKOFF_TOLERANCE_MINUTES` | Cross-provider kickoff tolerance (`15`) |
| `ALERT_DEDUPE_TTL_SECONDS` | Unchanged-alert suppression (`900`) |
| `ALERT_EDGE_CHANGE_THRESHOLD` | Material edge change as probability (`0.01`) |

Secrets are never returned by `/settings` or printed by commands.

Sport is a first-class field on canonical events, prediction quotes,
sportsbook quotes, opportunities, API filters, alerts, and the dashboard.
`ENABLED_SPORTS=soccer` keeps the current release soccer-only. Future
baseball or basketball connectors can add their canonical names without
changing the comparison pipeline, but each sport still needs its own market
and settlement mappings before it should be enabled in live mode.

## OddsPapi status

The REST client uses the official consumer v4 contract:
`https://api.oddspapi.io/v4`, query parameter `apiKey`, and catalog endpoint
`GET /bookmakers`. Provider slugs remain separate from canonical IDs.

Run credentialed coverage verification:

```bash
MOCK_MODE=false python -m app.scripts.verify_oddspapi_bookmakers
```

Coverage is deliberately **unverified** until this succeeds against the actual
account catalog. Unknown provider names are counted and never silently converted
to another bookmaker. Missing bookmakers get no zero, null-as-zero, fallback, or
fabricated quote.

The read-only connectors implement the documented transport contracts. Their
provider-to-canonical soccer market mapping still requires validation with
sanitized real responses before live opportunities can be enabled.

## Development

```bash
make format
make lint
make typecheck
make test
```

The PostgreSQL schema covers bookmakers and aliases, competitions and aliases,
teams and aliases, canonical/provider events and markets, mappings, both quote
types, order-book snapshots and levels, candidate qualification audits,
opportunities, alert history, system settings, and connector health.
Redis is intended for current scan state and short-lived alert deduplication;
mock mode uses deterministic in-process state.

`GET /opportunities` supports `market_type`, bookmaker, prediction market,
competition, minimum edge, and active-only filtering. `GET /market-candidates`
exposes accepted and rejected evaluations and supports market type, acceptance,
rejection reason, and provider filters. Candidate detail includes the full
liquidity qualification and ordered rejection reasons.

## Read-only provider architecture

The recurring orchestrator keeps connectors limited to discovery and public
market-data retrieval:

```text
Kalshi REST ─┐
             ├─ typed provider records → normalization/matching → qualification
Polymarket ──┤                                           │
OddsPapi ────┘                                           ├─ REST API
                                                         └─ Telegram output gate
```

`KalshiConnector` supports signed, read-only event/market discovery, order-book
snapshots, pagination, and recent public trades. `PolymarketConnector` uses the
public Gamma, CLOB, and Data APIs and never accepts a wallet, signer, or API key.
`SportsOddsConnector` retrieves OddsPapi soccer fixtures, bookmaker coverage,
and current fixture odds. No connector contains order, balance, position,
portfolio, wallet, transaction-signing, or sportsbook-betting methods.

Provider modes are independent and accept `mock`, `live`, or `disabled`:

```env
KALSHI_MODE=mock
POLYMARKET_MODE=live
SPORTS_ODDS_MODE=live
```

A failed provider updates only its own sanitized health record. REST polling is
the supported live synchronization path. WebSocket flags default to false;
sequence-aware snapshot recovery is not yet implemented, so WebSocket
availability never blocks REST mode.

The application scans once at startup and then every
`PRICE_POLL_INTERVAL_SECONDS`. Telegram is output-only and delivery occurs only
when all three conditions are true:

```env
LIVE_DRY_RUN=false
ALERTS_ENABLED=true
TELEGRAM_ENABLED=true
```

The default live dry-run retrieves data and exposes results without Telegram
delivery.

Additional monitoring endpoints:

- `GET /health/providers` — per-provider mode, connectivity, timestamps,
  latency, counters, staleness, and sanitized error
- `GET /markets` — current normalized prediction quotes
- `GET /matches` — current canonical match status

## Docker modes

```bash
# Mock
docker compose --env-file .env.mock.example up --build

# Live dry-run (copy first and fill only required credentials)
cp .env.live.example .env.live
docker compose --env-file .env.live up --build

docker compose exec api alembic upgrade head
docker compose exec api pytest -v
docker compose logs -f api
open http://localhost:8000/
curl http://localhost:8000/health/providers
curl http://localhost:8000/opportunities
```

The responsive monitoring dashboard is served at `http://localhost:8000/`. It
shows live opportunity totals, the strongest discrepancies, provider health,
candidate rejection reasons, and filterable opportunity and audit tables. The
dashboard refreshes automatically every 30 seconds and can also be refreshed
manually.

Kalshi requires a read-scoped API key ID and RSA private key for its documented
market-data endpoints. Polymarket public market data requires no credentials.
OddsPapi requires `SPORTS_ODDS_API_KEY`.

## Host development with Docker infrastructure

To run the scanner and Alembic directly on the host while keeping only
PostgreSQL and Redis in Docker:

```bash
docker compose up -d postgres redis
cp .env.local-live.example .env
# Fill SPORTS_ODDS_API_KEY and any enabled Kalshi credentials.
alembic upgrade head
uvicorn app.main:app --reload
```

Host processes must connect to `localhost:5432` and `localhost:6379`.
The hostnames `postgres` and `redis` are Compose service names and resolve only
inside the Compose network.
