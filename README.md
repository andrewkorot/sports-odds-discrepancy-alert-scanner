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
`/bookmakers`, `/events`, `/event-matches`, `/opportunities`, `/settings`,
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
| `SPORTS_ODDS_API_KEY` | OddsPapi consumer v4 key when its mode is live |
| `SPORTS_ODDS_BASE_URL` | OddsPapi consumer v4 base URL |
| `ODDSPAPI_DISCOVERY_DUMP_PATH` | Latest raw OddsPapi fixture and bulk tournament-odds responses; blank disables export |
| `KALSHI_API_KEY_ID` | Read-scoped Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | Kalshi signing key path, live mode only |
| `KALSHI_MODE` | `mock`, `live`, or `disabled` |
| `POLYMARKET_MODE` | `mock`, `live`, or `disabled`; no key required |
| `SPORTS_ODDS_MODE` | `mock`, `live`, or `disabled` |
| `LIVE_DRY_RUN` | Suppress all Telegram delivery (`true`) |
| `ALERTS_ENABLED` | Master opportunity-alert switch (`false`) |
| `TELEGRAM_BOT_TOKEN` | Optional Telegram bot token |
| `TELEGRAM_CHAT_ID` | Optional Telegram destination |
| `TELEGRAM_CLIENT_BOT_TOKEN` | Client Telegram bot token; preferred dual-destination configuration |
| `TELEGRAM_CLIENT_CHAT_ID` | Client Telegram chat ID |
| `TELEGRAM_OWNER_BOT_TOKEN` | Owner Telegram bot token |
| `TELEGRAM_OWNER_CHAT_ID` | Owner Telegram chat ID |
| `ENABLED_SPORTS` | Comma-separated canonical sports; currently `soccer` |
| `ENABLED_BOOKMAKERS` | Six comma-separated canonical IDs |
| `CLIENT_TIMEZONE` | Presentation timezone for dashboard and Telegram only (`America/Los_Angeles`) |
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
| `MIN_MINUTES_BEFORE_KICKOFF` | Earliest pregame boundary. controls how close to kickoff an event can be before it stops qualifying as an opportunity. (`10`) |
| `DISCOVERY_CALENDAR_DAYS` | Number of complete UTC calendar days to discover (`1` = current UTC day) |
| `ALERT_COOLDOWN_MINUTES` | Duplicate alert cooldown (`10`) |
| `REALERT_EDGE_INCREASE_PP` | Early re-alert edge improvement (`1.0`) |
| `PRICE_POLL_INTERVAL_SECONDS` | Recurring scan interval (`30`) |
| `PROVIDER_REQUEST_CONCURRENCY` | Maximum simultaneous provider pricing requests (`8`) |
| `AUTO_START_STOP_ENABLED` | Enable daily automatic scan start/stop (`false`) |
| `SCAN_AUTO_START_TIME` | Daily automatic start in `CLIENT_TIMEZONE`, `HH:MM` (`06:00`) |
| `SCAN_AUTO_STOP_TIME` | Daily automatic stop in `CLIENT_TIMEZONE`, `HH:MM` (`23:00`) |
| `EVENT_MATCH_KICKOFF_TOLERANCE_MINUTES` | Cross-provider kickoff tolerance. controls the maximum kickoff-time difference allowed when determining whether two providers refer to the same event. (`15`) |
| `EVENT_MATCH_FUZZY_MIN_SCORE` | Minimum weighted score for a manual-review candidate (`80`) |
| `EVENT_MATCH_AMBIGUITY_MARGIN` | Minimum lead over the runner-up candidate (`5`) |

Secrets are never returned by `/settings` or printed by commands.

### Scanner run control

The dashboard provides manual **Start** and **Stop** controls. The same controls are
available through `POST /scanner/start` and `POST /scanner/stop`; current status is
returned by `GET /scanner/control` and `/health`. A stop request never cancels a scan
already in progress. That scan completes and its snapshot is persisted, but no next
scan starts.

Set `AUTO_START_STOP_ENABLED=true` to apply the daily start and stop times in
`CLIENT_TIMEZONE`. Overnight windows are supported (for example, start `20:00`, stop
`06:00`). A manual choice remains active until the next automatic schedule boundary.
In live mode the control state is retained in Redis across application restarts.

The System page also allows the operational thresholds, freshness/liquidity rules,
matching tolerances, polling interval, provider concurrency, and automatic schedule
to be edited without rebuilding or restarting the application. Updates use
`PATCH /settings`, are validated before being applied, and are stored in PostgreSQL
in live mode. Environment values provide the initial defaults until a user saves a
runtime value. Redis remains limited to short-lived alert deduplication and scanner
run-control state. Credentials, provider
URLs, Telegram destinations, and database/Redis connection details are never
runtime-editable or returned by this endpoint.

On the first deployment of this version, any existing legacy
`scanner:runtime-settings` Redis value is migrated automatically into PostgreSQL and
then removed from Redis.

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

The first live normalization path deliberately accepts only soccer pregame,
full-time `1x2` selections from the OddsPapi market catalog. Prediction-market
titles must identify the matching home team, away team, or draw and must not
contain qualification, extra-time, penalty, first-half, handicap, total,
double-chance, or draw-no-bet language. Ambiguous markets are skipped rather
than guessed. Totals, spreads, and BTTS remain available in deterministic mock
mode but are not enabled in the first live path.

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
In live mode PostgreSQL stores normalized events, provider events and markets,
quotes, order books and levels, candidate decisions, opportunities, connector
health, and alert history. Redis stores active alert keys, cooldown state, edge
state, and disappearance/reappearance state. Mock mode remains deterministic
and uses in-process state.

`GET /opportunities` supports `market_type`, bookmaker, prediction market,
competition, minimum edge, and active-only filtering. `GET /market-candidates`
exposes accepted and rejected evaluations and supports market type, acceptance,
rejection reason, and provider filters. Candidate detail includes the full
liquidity qualification and ordered rejection reasons.

`GET /event-matches` exposes the event-matching audit and can filter by
`matched`, `provider`, and `confidence`. Exact and explicitly approved alias
matches may proceed automatically. Similar-name fuzzy candidates are labeled
`manual_review` and never produce prices, opportunities, or alerts. Fallback
matching uses RapidFuzz with weighted home/away participants, competition,
kickoff, and sport scores. Team qualifiers such as women, U21/U23, reserves,
II, B team, academy, and esports are preserved and conflicting qualifiers are
hard rejections. Competition country, gender, age group, and league-level
metadata are also checked when providers supply them. Close runner-up scores
fail the ambiguity margin and remain manual review. Persisted provider-event
mappings are reused on later scans only after the current event still passes
identity, competition, kickoff, sport, qualifier, and settlement safeguards.

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
snapshots, pagination, and recent public trades. Soccer discovery uses Kalshi's
official `Sports` category plus `Soccer` series tag and admits only series whose
product-metadata scope is `Game`. Participants are extracted from structured
metadata, explicit event/subtitle matchups, or exactly two market YES outcome
names. Because Kalshi does not guarantee home/away ordering, the pair remains
unordered until one unique OddsPapi fixture supplies canonical orientation;
ambiguous and fuzzy candidates require manual review. `PolymarketConnector` uses the
public Gamma, CLOB, and Data APIs and never accepts a wallet, signer, or API key.
Its soccer discovery uses Gamma's `tag_slug=soccer` and documented
`start_time_min`/`start_time_max` window. Fixture kickoff comes from
event-level `startTime`, then nested market `eventStartTime`, with end-time
fields used only as fallbacks; Gamma `startDate` is never treated as kickoff.
Ordered Gamma teams and series metadata supply home/away and competition, and
only open, order-book-enabled `moneyline` markets enter the scanner.
`SportsOddsConnector` retrieves OddsPapi soccer fixtures, bookmaker coverage,
and current fixture odds. No connector contains order, balance, position,
portfolio, wallet, transaction-signing, or sportsbook-betting methods.

Provider modes are independent and accept `mock`, `live`, or `disabled`:

```env
KALSHI_MODE=mock
POLYMARKET_MODE=live
SPORTS_ODDS_MODE=live
```

A failed provider updates only its own sanitized health record. This project
uses REST polling exclusively for Kalshi, Polymarket, and OddsPapi.

The application scans whole UTC calendar days, beginning at the current UTC
day's midnight. `DISCOVERY_CALENDAR_DAYS=1` scans the current UTC day, `2`
scans the current and next UTC days, and `3` scans three UTC days. The ending
midnight is exclusive. `CLIENT_TIMEZONE` affects dashboard and alert display,
not discovery boundaries. Scans run once at startup and then every
`PRICE_POLL_INTERVAL_SECONDS`. Telegram is output-only and delivery occurs only
when both conditions are true:

```env
LIVE_DRY_RUN=false
ALERTS_ENABLED=true
TELEGRAM_CLIENT_BOT_TOKEN=client-bot-token
TELEGRAM_CLIENT_CHAT_ID=client-chat-id
TELEGRAM_OWNER_BOT_TOKEN=owner-bot-token
TELEGRAM_OWNER_CHAT_ID=owner-chat-id
```

The default live dry-run retrieves data and exposes results without Telegram
delivery. A live polling cycle performs:

```text
OddsPapi fixtures and market catalog
→ fixture odds for configured bookmakers
→ Kalshi/Polymarket event and market discovery
→ executable order books and verified/reconstructed volume
→ deterministic event and settlement matching
→ independent edge and qualification calculation
→ atomic PostgreSQL persistence
→ Redis disappearance/cooldown/re-alert gate
→ optional Telegram delivery and alert history
```

One failed prediction provider or one missing bookmaker does not fabricate data
and does not prevent other available providers or bookmakers from being
processed. OddsPapi is the required event/odds anchor for a comparison cycle.

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
