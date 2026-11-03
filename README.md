# FuelGuard

A backend service for a trucking company to manage drivers, trucks and fuel cards, record fuel
purchases, and flag risky transactions for review.

**Stack:** FastAPI · Pydantic v2 · PostgreSQL · SQLAlchemy 2 (async API, sync workers) · Alembic ·
Celery + Redis · OAuth2 / OpenID Connect (Keycloak) · Docker Compose · Pytest

- [Quick start](#quick-start)
- [Authentication and roles](#authentication-and-roles)
- [Try it](#try-it-end-to-end)
- [How a purchase works](#how-a-purchase-works)
- [Risk rules](#risk-rules)
- [Background processing](#background-processing)
- [Error handling and retries](#error-handling-and-retries)
- [Architecture](#architecture)
- [Database design](#database-design)
- [API overview](#api-overview)
- [Configuration](#configuration)
- [Testing](#testing)
- [Design decisions and assumptions](#design-decisions-and-assumptions)
- [Scaling and limitations](#scaling-and-limitations)

## What the brief asked for, and where it is

| Requirement | Where |
|---|---|
| Manage drivers and trucks, assign drivers to trucks | `/drivers`, `/trucks`, `/drivers/{id}/assign-truck` |
| Issue cards; activate, deactivate, block; configure limits | `/cards`, `/cards/{id}/status`, `/cards/{id}/limits` |
| Simulate a fuel card purchase, validate against status and limits | `POST /transactions` ([flow](#how-a-purchase-works)) |
| Flag limit, tank, unusually large and burst purchases; keep them for review | [Risk rules](#risk-rules), `/transactions/flagged`, `/transactions/{id}/review` |
| At least two background workflows, with the reasoning | [Four workflows](#background-processing) on Celery + Redis |
| Alembic migrations, Docker Compose, tests, logging, env config, OpenAPI | `alembic/`, `docker-compose.yml`, 545 tests, `/docs` |
| *(not in the brief)* Who may call what | [OAuth2 tokens from Keycloak, three roles](#authentication-and-roles) |

## Quick start

You need Docker with Compose. Nothing else is installed on your machine, and nothing is deployed
anywhere: everything runs locally and the ports are bound to `127.0.0.1` only.

```bash
cp .env.example .env        # the defaults work for local use; change the passwords if you like
docker compose up --build   # builds the image, starts everything, applies the migrations
```

| What | Address |
|---|---|
| Swagger UI (try every endpoint) | http://localhost:8010/docs |
| ReDoc / OpenAPI JSON | http://localhost:8010/redoc · http://localhost:8010/openapi.json |
| Health check (database, Redis and Keycloak; 503 if any is down) | http://localhost:8010/health |
| Keycloak admin console (user `admin`, password `KEYCLOAK_ADMIN_PASSWORD`) | http://localhost:8180 |
| PostgreSQL | `localhost:5442` (user, password and database from `.env`) |
| Redis | `localhost:6380` |

Six containers start: `api`, `worker` (Celery), `beat` (Celery scheduler), `db`, `redis` and `keycloak`
(the login server). The `api` container runs `alembic upgrade head` before it starts serving.

The first start downloads the Keycloak image (about 500 MB) and Keycloak needs roughly a minute to be ready;
`docker compose ps` shows `healthy` when it is. Until then you can open Swagger but cannot get a token.

To call protected endpoints in Swagger, click **Authorize** and log in; see
[Authentication and roles](#authentication-and-roles).

Stop with `Ctrl+C` (or `docker compose down`). Add `-v` to also delete the database volume.

## Authentication and roles

Requests are authenticated with **OAuth2 bearer tokens issued by Keycloak** (OpenID Connect). FuelGuard
never sees a password or a long-lived secret: a caller gets a short-lived token from Keycloak and sends it
as `Authorization: Bearer <token>`. The API only **verifies** tokens.

| Who | How it gets a token | Role | May call |
|---|---|---|---|
| **Manager** (a person) | logs in (authorization code flow with PKCE); demo user `manager` | `manager` | everything |
| **Auditor** (a person) | logs in; demo user `auditor` | `auditor` | every read endpoint (`GET`), nothing that changes data |
| **Card terminal** (a machine) | client credentials: client id `fuelguard-terminal` and its secret | `terminal` | only `POST /transactions` |

**Why three roles.** Terminals sit in public places, so a stolen terminal credential must be able to do almost
nothing: it can only send purchases, which are still checked and flagged by the risk rules. Managers change
data. Auditors can look at everything but change nothing. This is least privilege: each caller gets only what
it needs.

**What the API checks on every request**

| Check | Failure |
|---|---|
| Header is `Authorization: Bearer <token>` | 401 `Missing access token` |
| Signature is valid **RS256**, using a key from Keycloak's published key set, matched by key id. `none` and shared-secret algorithms are refused. | 401 `Invalid access token` |
| Not expired (tokens last 5 minutes; 10 seconds of clock difference tolerated) | 401 `Access token has expired` |
| Issued by the expected issuer, and meant for this API (audience `fuelguard-api`) | 401 `Invalid access token` |
| The token's role allows this endpoint | 403 `Requires one of the roles: ...` |
| Keycloak could not be reached to fetch the signing keys | 503 (not the caller's fault) |

The reason for a rejection is written to the log, never sent back to the caller. 401 responses carry
`WWW-Authenticate: Bearer`. The API keeps Keycloak's signing keys cached and re-reads them when it sees an
unknown key id, so a key rotation needs no restart.

**Logging in with Swagger** (http://localhost:8010/docs, **Authorize**):

- **ManagerLogin**: click Authorize, sign in on the Keycloak page as `manager` (password: `MANAGER_PASSWORD` in
  your `.env`) or `auditor` (`AUDITOR_PASSWORD`). You are sent back to Swagger with a token. The
  `client_id` is already filled in; leave `client_secret` empty.
- **TerminalClient**: enter client id `fuelguard-terminal` and the secret `TERMINAL_CLIENT_SECRET` from your
  `.env`. Swagger asks Keycloak for a token directly. You need this one to try `POST /transactions`.

**Getting tokens without a browser** (scripts, curl): see the commands at the start of the next section.

**Audit trail.** The card status history records the real username from the token (for example `manager`),
not a fixed word.

**Managing users.** Open the Keycloak admin console (http://localhost:8180) to add people, change their roles,
or reset passwords. The starting setup (clients, roles, demo users) is in `keycloak/fuelguard-realm.json`;
passwords and the terminal secret are filled in from `.env` when Keycloak imports it, so they are not in the repository.

## Try it end to end

Run these from the project folder after `docker compose up`, once Keycloak is `healthy`. On an empty
database every ID below is `1`.

```bash
# Get tokens from Keycloak. Each lasts 5 minutes; run these lines again when one expires.
KC=http://localhost:8180/realms/fuelguard/protocol/openid-connect/token
val() { grep "^$1=" .env | cut -d= -f2; }
tok() { python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])'; }
MT=$(curl -s $KC -d grant_type=password -d client_id=fuelguard-manager -d username=manager \
     --data-urlencode "password=$(val MANAGER_PASSWORD)" | tok)           # manager: full access
TT=$(curl -s $KC -d grant_type=client_credentials -d client_id=fuelguard-terminal \
     --data-urlencode "client_secret=$(val TERMINAL_CLIENT_SECRET)" | tok)  # card terminal
API=http://localhost:8010
J='Content-Type: application/json'

# 1. Set up a station, a driver with a truck (100-gallon tank), and a card with limits
curl -s -X POST $API/stations -H "Authorization: Bearer $MT" -H "$J" -d '{"name":"Shell Highway 10"}'
curl -s -X POST $API/drivers  -H "Authorization: Bearer $MT" -H "$J" -d '{"full_name":"John Smith","license_number":"TX-1234567"}'
curl -s -X POST $API/trucks   -H "Authorization: Bearer $MT" -H "$J" -d '{"plate_number":"TX-4821","tank_capacity_gal":"100"}'
curl -s -X POST $API/drivers/1/assign-truck -H "Authorization: Bearer $MT" -H "$J" -d '{"truck_id":1}'
curl -s -X POST $API/cards    -H "Authorization: Bearer $MT" -H "$J" \
  -d '{"card_number":"7001-2345-6789","driver_id":1,"per_transaction_limit":"300","daily_limit":"500"}'

# 2. A normal purchase, sent by the terminal: accepted, no flags
curl -s -X POST $API/transactions -H "Authorization: Bearer $TT" -H "$J" -H "Idempotency-Key: demo-0001" \
  -d '{"card_number":"7001-2345-6789","station_id":1,"gallons":"40","total_price":"150.00"}'

# 3. A suspicious purchase: 150 gallons into a 100-gallon tank for $450
curl -s -X POST $API/transactions -H "Authorization: Bearer $TT" -H "$J" -H "Idempotency-Key: demo-0002" \
  -d '{"card_number":"7001-2345-6789","station_id":1,"gallons":"150","total_price":"450.00"}'

# 4. A moment later the worker has analyzed it. Review what was flagged:
sleep 2
curl -s $API/transactions/flagged -H "Authorization: Bearer $MT"
#    -> exceeds_transaction_limit (450 > 300), exceeds_daily_limit (150 + 450 = 600 > 500),
#       exceeds_tank_capacity (150 > 100), unusually_large (150 > 80), each with the numbers behind it
curl -s $API/notifications -H "Authorization: Bearer $MT"

# 5. Block the card: the next purchase is declined at the pump, and the attempt is still stored
curl -s -X PATCH $API/cards/1/status -H "Authorization: Bearer $MT" -H "$J" -d '{"status":"blocked"}'
curl -s -X POST $API/transactions -H "Authorization: Bearer $TT" -H "$J" -H "Idempotency-Key: demo-0003" \
  -d '{"card_number":"7001-2345-6789","station_id":1,"gallons":"10","total_price":"35.00"}'
#    -> "authorized": false, "decline_reason": "card_blocked"

# 6. Build today's expense report on demand (it also runs every night)
curl -s -X POST "$API/reports/daily/run?report_date=$(date -u +%F)" -H "Authorization: Bearer $MT"
sleep 2; curl -s "$API/reports/daily?report_date=$(date -u +%F)" -H "Authorization: Bearer $MT"

# 7. Roles in action: the terminal token cannot read data (403), and without a token you get 401
curl -s $API/cards -H "Authorization: Bearer $TT"
curl -s $API/cards
```

## How a purchase works

`POST /transactions` is called by the card terminal (with a `terminal` token) with a unique
`Idempotency-Key` header and `card_number`, `station_id`, `gallons`, `total_price` and an optional
`truck_id`. The server sets the transaction time (UTC), so a terminal cannot backdate a purchase.

```mermaid
sequenceDiagram
    participant T as Card terminal
    participant A as API
    participant DB as PostgreSQL
    participant R as Redis queue
    participant W as Celery worker

    T->>A: POST /transactions (Idempotency-Key)
    A->>DB: card, existing key?
    alt key already used
        A-->>T: 200, the first result (409 if the body differs)
    else new purchase
        A->>A: instant checks: card status, truck
        A->>DB: save as pending or declined, COMMIT
        A->>R: queue analysis (after the commit)
        A-->>T: 201, authorized true or false
        R->>W: analyze_transaction
        W->>DB: lock row, run risk rules, save flags, mark approved
        W->>R: queue notification if flagged
    end
```

**Instant checks** (in the request, in this fixed order; the first failure is the decline reason):

| Order | Check | Result |
|---|---|---|
| 1 | Card is `inactive` | declined, `card_inactive` |
| 2 | Card is `blocked` | declined, `card_blocked` |
| 3 | Driver has no truck assigned | declined, `no_vehicle` |
| 4 | Request names a `truck_id` that is not the assigned truck | declined, `vehicle_mismatch` |
| | Otherwise | saved as `pending`, `authorized: true` |

Only these checks decline a purchase. Spending limits, tank size and unusual patterns never block the
driver at the pump; they **flag** the purchase for a manager instead. A declined attempt is stored
like any other transaction, so nothing is silently discarded.

A manager lets a driver use a different truck by reassigning the driver (`assign-truck`).

An unknown card or station returns 404 and stores nothing (there is no card to attach it to).

## Risk rules

The worker runs these on every accepted purchase. A purchase can get several flags. Flags keep the
numbers behind them in `details`, so a reviewer sees why. Every threshold is a setting.

| Flag | Fires when | Default |
|---|---|---|
| `exceeds_transaction_limit` | this purchase's price is above the card's per-transaction limit | |
| `exceeds_daily_limit` | spending on the card in the last **24 hours** (including this one) is above its daily limit | |
| `exceeds_weekly_limit` | spending in the last **7 days** is above the weekly limit | |
| `exceeds_monthly_limit` | spending in the last **30 days** is above the monthly limit | |
| `exceeds_tank_capacity` | gallons are more than the truck's tank | |
| `unusually_large` | gallons are above **2 ×** the driver's average purchase; until the driver has **5** past purchases, above **80 %** of the tank | 2.0 / 5 / 0.8 |
| `card_frequency` | **3 or more** attempts on the card within **30 minutes** | 3 / 30 |
| `truck_frequency` | the same for the truck, across all cards | 3 / 30 |

Details that matter:

- A limit is only checked if it is set. A card with no limits is never limit-flagged.
- Limit windows are **rolling** (the last 24 h, 7 d, 30 d), not calendar days.
- Spending and averages count accepted purchases only. **Declined attempts count for the two
  frequency flags only**, because repeated tries on a blocked card are exactly what a manager wants to know about.
- Each purchase is judged by the history **up to its own timestamp**. Later purchases never affect an
  earlier one, so the result does not depend on which job happens to finish first.
- A declined attempt can get `card_frequency` and `truck_frequency` too (checked by a separate
  quick job), so a thief who is never approved is still noticed.

Managers review flags with `GET /transactions/flagged` (filter by reason, card, driver, truck, date,
review status) and close them with `PATCH /transactions/{id}/review` (`reviewed` or `dismissed`, with a
note). Reviewing never deletes anything.

## Background processing

**Choice: Celery + Redis**, not FastAPI `BackgroundTasks`.

| | `BackgroundTasks` | Celery + Redis |
|---|---|---|
| Runs in | the API process | separate worker processes |
| If the process crashes or restarts | the work is lost | queued jobs survive in Redis |
| Retries with backoff | you write them | built in |
| Scheduled jobs (nightly report, recovery) | needs another tool | Celery beat |
| Scale | tied to the API | add worker containers |

The brief asks how errors and retries are handled, and the money-related checks must not silently
vanish when a process restarts. `BackgroundTasks` would give neither, and it could not run the
nightly report. The cost is two extra containers (worker, beat) and Redis.

| Workflow | Trigger | What it does |
|---|---|---|
| **Risk analysis** `analyze_transaction` | after each accepted purchase | runs all risk rules, saves flags, marks the purchase `approved` |
| **Declined-attempt check** `check_declined_attempt` | after each declined attempt | burst check, so repeated failed attempts are flagged |
| **Notification** `notify_suspicious_transaction` | after a transaction gets flags | writes a warning to the log and a row in `notifications` |
| **Daily expense report** `daily_report` | Celery beat, 00:10 UTC (or `POST /reports/daily/run`) | one row per card and day: gallons, spend, count, flagged count |
| **Recovery** `recover_stuck_transactions` | Celery beat, every 2 minutes | re-queues anything that never finished (see below) |

Why `pending` then `approved`: the driver at the pump gets a fast answer from the instant checks. The
heavier analysis (several queries over a card's history) runs in the background, normally well under
a second after the purchase. A manager can see unfinished ones at `GET /transactions/stuck`.

## Error handling and retries

The rule behind the design: **the purchase is saved first, and everything after that can fail and be
repeated without losing or duplicating data.**

| What goes wrong | What happens |
|---|---|
| Terminal retries a purchase (timeout, double tap) | Same `Idempotency-Key` + same body returns the first result (HTTP 200). Same key + different body is a 409. A unique constraint on the key means even simultaneous retries create exactly one row. |
| Redis is down when the purchase arrives | The purchase is already committed, so the request still succeeds (201). The failure to queue is logged, the transaction stays `pending`, and the recovery job queues it once Redis is back. |
| Database error inside a worker job (connection lost, restart) | The job retries automatically with exponential backoff and jitter (about 1 s, 2 s, 4 s, ... capped at 60 s), up to `ANALYSIS_MAX_RETRIES` (5) times. Each attempt is one database transaction, so a failure leaves no half-written flags. |
| Retries run out | An error is logged with the transaction ID. The transaction **stays `pending`**; it is never dropped or silently approved. It shows in `GET /transactions/stuck`, and the recovery job tries again later. |
| A worker dies in the middle of a job | The message is acknowledged only after the job finishes (`acks_late`) and is re-delivered if the worker is lost (`reject_on_worker_lost`). |
| The same job runs twice (retry, duplicate message, recovery) | Jobs are **idempotent**: the transaction row is locked, anything not `pending` is skipped, flags have a unique constraint per transaction and reason, and notifications are created once. Running twice changes nothing. |
| Notification could not be queued after the flags were saved | Logged. The recovery job also finds flagged transactions that have no notification and queues them. |
| A bug in a job (not a database error) | Not retried (retrying cannot fix it); logged by the task failure handler. |
| Someone needs to know whether the service can work right now | `GET /health` checks PostgreSQL, Redis and Keycloak (3 s timeout each) and returns 503 with the failing check named, for example `{"checks": {"database": "ok", "redis": "error", "identity_provider": "ok"}}`. It does not check that a worker is running; unfinished background work shows in `GET /transactions/stuck`. |
| A token is missing, expired, forged or for the wrong role | 401 or 403 in the standard error format; the precise reason goes to the log only. A terminal whose token expired asks Keycloak for a new one. |
| Keycloak is down | Requests with a token that needs a key the API has not cached get 503 (not 401, since the caller did nothing wrong). Keys already cached keep working for up to 5 minutes. `/health` reports it. |
| Unexpected exception in the API | Returned as a generic `500` in the standard error format. Details go to the log, never to the client. |

All API errors share one JSON shape, with a stable `code` for programs and a `message` for people:

```json
{ "error": { "code": "conflict", "message": "A card with this number already exists", "details": null } }
```

Codes: `validation_error` (422, with the failing fields in `details`), `not_found` (404), `conflict` (409),
`invalid_state` (422), `http_error` (401/403/503, also unknown paths), `internal_error` (500).

**Recovery job** (every 2 minutes): finds transactions still `pending` after 5 minutes, declined
attempts not yet checked, and flagged transactions without a notification, and queues the right job
again. Because the jobs are idempotent, re-queuing work that was only slow is harmless.

**Concurrency control.** Races are handled in the database, not in Python: the unique idempotency key,
a partial unique index (one *current* truck per driver), unique constraints on cards, licenses and plates, and
`SELECT ... FOR UPDATE` row locks when a card's status, a driver, or a transaction is changed.

## Architecture

```mermaid
flowchart LR
    T[Card terminal] -- 1. client credentials --> K[Keycloak<br/>OAuth2 / OIDC]
    M[Manager / Auditor<br/>Swagger] -- 1. log in --> K
    T -- 2. Bearer token --> API
    M -- 2. Bearer token --> API
    API -- fetches signing keys --> K
    API[FastAPI<br/>async SQLAlchemy] --> DB[(PostgreSQL)]
    API -- queue after commit --> R[(Redis)]
    R --> W[Celery worker<br/>sync SQLAlchemy]
    B[Celery beat] -- nightly report,<br/>recovery every 2 min --> R
    W --> DB
```

- **API** is async and stateless, so it can run as many copies as needed. It verifies tokens itself
  with Keycloak's public keys, so it does not call Keycloak on every request.
- **Workers** use a small synchronous database layer, because Celery tasks are synchronous. The two sides
  share only the models. The API never imports worker code: it sends tasks **by name**
  (`app/workers/names.py`), so the two can be deployed separately.
- **Risk rules are pure functions** (`app/services/risk_rules.py`): numbers in, flags out, no database.
  The worker gathers the numbers (`app/workers/risk_analysis.py`) and the rules decide. That keeps the
  rules easy to read and to unit test.

```
app/
  main.py                FastAPI app, routers, error handlers
  api/                   routers: drivers, trucks, cards, stations, transactions, notifications, reports
  schemas/               Pydantic v2 request and response models
  services/              business logic (async): fleet, cards, purchases, review, reports
                         risk_rules.py = the pure risk rules
  workers/               Celery app, tasks, risk_analysis.py (sync DB logic), dispatch.py (queueing)
  models/                SQLAlchemy models        db/   engines and sessions (async + sync)
  core/                  settings, logging, errors, token verification and roles (security.py)
alembic/                 migrations (the schema is created only through Alembic)
keycloak/                the login server's starting setup: clients, roles, demo users
tests/                   unit/  integration/  workers/
```

## Database design

```mermaid
erDiagram
    drivers ||--o{ driver_truck_assignments : has
    trucks  ||--o{ driver_truck_assignments : used_in
    drivers ||--o{ fuel_cards : holds
    fuel_cards ||--o{ card_status_history : logs
    fuel_cards ||--o{ fuel_transactions : makes
    drivers ||--o{ fuel_transactions : "snapshot"
    trucks  |o--o{ fuel_transactions : "snapshot"
    fuel_stations ||--o{ fuel_transactions : at
    fuel_transactions ||--o{ transaction_flags : "flagged by"
    fuel_transactions ||--o{ notifications : raises
    fuel_cards ||--o{ daily_reports : summarized_in
```

| Table | Notes |
|---|---|
| `drivers`, `trucks` | Soft delete (`is_active`), so history keeps pointing at them. Truck has `tank_capacity_gal`. |
| `driver_truck_assignments` | History of who drove what. `unassigned_at IS NULL` means current. A partial unique index allows one current truck per driver; several drivers may share a truck. |
| `fuel_cards` | Status `active` / `inactive` / `blocked`; four optional limits (per transaction, daily, weekly, monthly). |
| `card_status_history` | Who changed a card's status, and when (including automatic changes). |
| `fuel_transactions` | Card, driver, truck and station, gallons, price, server UTC time, status `pending` / `approved` / `declined`, decline reason, review fields. Unique `idempotency_key`. |
| `transaction_flags` | One row per reason per transaction (unique), with a JSON `details` of the numbers. |
| `notifications`, `daily_reports` | Alerts for managers; one report row per card and day (unique, so re-running updates it). |

Choices worth knowing:

- **Money and gallons are `Numeric`**, never float; all timestamps are timezone-aware UTC.
- **A transaction stores the driver and truck as they were at purchase time.** Reassigning a driver
  later does not rewrite history.
- **Enums are stored as text**, so adding a status or flag reason does not need a special migration.
- **Indexes** match the queries the rules run: `(card_id, transaction_time)`, `(truck_id, transaction_time)`,
  and a partial index on `pending` transactions for the recovery job.
- Check constraints keep bad data out even if the application has a bug (positive limits, gallons and tank size).

## API overview

Interactive documentation with request examples and error responses: **http://localhost:8010/docs**.
Lists accept `limit` (1–100, default 50) and `offset`.

Every `GET` needs the `manager` or `auditor` role. Every change (`POST`, `PATCH`, `DELETE`) needs `manager`,
except `POST /transactions`, which only a `terminal` token may call. `/health` needs no token.

| Area | Endpoints |
|---|---|
| Drivers | `POST/GET /drivers`, `GET/PATCH/DELETE /drivers/{id}`, `POST /drivers/{id}/assign-truck`, `POST /drivers/{id}/unassign-truck`, `GET /drivers/{id}/assignments` |
| Trucks | `POST/GET /trucks`, `GET/PATCH/DELETE /trucks/{id}` |
| Cards | `POST/GET /cards`, `GET /cards/{id}`, `PATCH /cards/{id}/status`, `PATCH /cards/{id}/limits`, `GET /cards/{id}/history` |
| Stations | `POST/GET /stations`, `GET /stations/{id}` |
| Purchase (terminal only) | `POST /transactions` |
| Review | `GET /transactions`, `GET /transactions/flagged`, `GET /transactions/stuck`, `GET /transactions/{id}`, `PATCH /transactions/{id}/review` |
| Alerts | `GET /notifications`, `PATCH /notifications/{id}/read` |
| Reports | `GET /reports/daily`, `POST /reports/daily/run` |
| Health (no token) | `GET /health` (200 when PostgreSQL, Redis and Keycloak answer, otherwise 503 with the failing check named) |

`DELETE` on a driver or truck is a soft delete. Deleting a driver also closes the driver's truck
assignment and switches the driver's active cards to `inactive` (blocked cards stay blocked), so a removed
driver cannot keep fuelling.

## Configuration

Everything comes from environment variables (`.env`, see `.env.example`). The app refuses to start if a
required setting is missing.

| Setting | Purpose | Default |
|---|---|---|
| `DATABASE_URL_ASYNC`, `DATABASE_URL_SYNC` | database for the API (asyncpg) and for workers and Alembic (psycopg2) | |
| `REDIS_URL` | Celery broker and result backend | |
| `OIDC_ISSUER` | Keycloak realm address; must equal the `iss` claim of the tokens | |
| `OIDC_JWKS_URL` | where the API fetches the token signing keys (Keycloak's address inside Docker) | issuer + `/protocol/openid-connect/certs` |
| `OIDC_AUDIENCE` | the audience tokens must carry | `fuelguard-api` |
| `KEYCLOAK_ADMIN_PASSWORD`, `MANAGER_PASSWORD`, `AUDITOR_PASSWORD`, `TERMINAL_CLIENT_SECRET` | read by Keycloak (not the API) when it imports the realm | |
| `LOG_LEVEL`, `LOG_FORMAT` | `text` or `json` log lines | `INFO`, `text` |
| `FREQUENCY_MAX_PURCHASES`, `FREQUENCY_WINDOW_MINUTES` | burst rule | 3, 30 |
| `LARGE_PURCHASE_MULTIPLIER`, `LARGE_PURCHASE_MIN_HISTORY`, `LARGE_PURCHASE_FALLBACK_TANK_RATIO` | unusually large rule | 2.0, 5, 0.8 |
| `ANALYSIS_MAX_RETRIES` | retries for background jobs | 5 |
| `STUCK_AFTER_MINUTES`, `RECOVERY_INTERVAL_SECONDS` | recovery job | 5, 120 |

## Testing

```bash
docker compose run --rm --build tests
```

**545 tests, all against a real PostgreSQL** (no SQLite, no mocked database), 100 % line coverage of `app/`.

| Folder | Tests | What |
|---|---|---|
| `tests/unit` | 105 | the risk rules (including exact boundaries), **token verification** (valid, expired, wrong issuer or audience, forged signature, `none` and algorithm-confusion attacks, Keycloak down), roles, schema validation, settings, logging |
| `tests/integration` | 348 | every endpoint through the real app: a **role matrix** (every endpoint x every role, built from the OpenAPI spec so a new endpoint cannot be left open), validation, error format, purchase decisions and their priority, idempotency, review, reports, health check, audit trail, parallel requests, migrations |
| `tests/workers` | 92 | the analysis against backdated history, burst checks, notifications, reports, recovery, and **retries** (flaky database, giving up, no half-written data) |

How the tests stay safe and honest:

- They use their own database, `fuelguard_test`, built from the **Alembic migrations** (so the migrations are
  tested too, including a downgrade and upgrade). The tests refuse to run against any database whose name
  does not end in `_test`.
- Redis is never touched: queueing is replaced by a list that tests can inspect.
- Keycloak is not needed: tokens are signed with a throwaway RSA key that the API is told to trust, which also
  lets the tests forge every kind of bad token. Keycloak itself (realm import, token contents, key rotation,
  CORS and redirect rules) was checked by hand against the running stack and is not part of the automated suite.
- Race conditions are tested with real parallel requests (simultaneous retries of one purchase, competing
  status changes and assignments).
- Backdated history (needed for the 24 h / 7 d / 30 d windows) is inserted directly into the database,
  because the API correctly refuses to accept a client-chosen time.
- Coverage alone does not prove a test can fail, so key rules were also broken on purpose (for example a
  limit check changed from `>` to `>=`, or a window from 7 to 8 days) to confirm the suite catches each one.

To run a part, for example `docker compose run --rm tests pytest tests/unit -q`. For a coverage report:
`docker compose run --rm tests pytest --cov=app`.

## Design decisions and assumptions

The brief leaves some things open. These are the choices made, and why.

| Question | Decision | Why |
|---|---|---|
| Reject or flag a risky purchase? | Decline only on card status and truck problems; **flag** everything else | A driver should not be stranded at the pump by a limit rule, but a manager must still see it. Both "validate" and "keep for review" are honored. |
| Can a blocked card be unblocked? | Yes, any status can change to any other | A block can be lifted when a fraud report is withdrawn. Every change is logged. |
| Which limits? | Per transaction, daily, weekly, monthly, all optional | Covers one big purchase and slow overspending. |
| Calendar days or rolling windows? | Rolling (24 h / 7 d / 30 d) | No loophole at midnight. |
| How is the truck chosen? | From the driver's current assignment; an optional `truck_id` must match it | Prevents using a card on the wrong vehicle. A manager reassigns to allow a change. |
| Who sets the transaction time? | The server | A terminal cannot backdate to dodge a window. |
| Unusually large | Compared with the driver's own average, with a tank-share fallback for new drivers | A fixed number would be wrong for both small vans and big trucks. |
| Authentication | OAuth2 bearer tokens from Keycloak; roles `manager`, `auditor`, `terminal` | The brief did not specify authentication. Short-lived tokens, a separate role for each kind of caller, and a real username in the audit trail. The API only verifies tokens, because writing our own token server is easy to get wrong. |
| Roles or scopes? | Keycloak realm roles | Simple to manage in Keycloak; one role per kind of caller is enough here. Scopes would be the next step for finer control. |
| Async or sync database code? | Async in the API, sync in the workers | The API benefits from async; Celery tasks are synchronous. They share only the models. |
| Microservices? | No: one codebase, one database | Right-sized for this scope. The API and workers are already separate processes that talk through a queue by task name, so they could be split later. |
| No frontend | The API and its Swagger UI are the interface | The brief is backend-only. |

## Scaling and limitations

**Scaling.** The API and workers are stateless, so they scale out by running more containers behind a load
balancer (for example `docker compose up --scale worker=4`; the API needs a load balancer and a port change to
run several copies). Jobs are idempotent and rows are locked, so many workers cannot duplicate work. The
database is the part that grows hardest: next steps would be read replicas for reports and partitioning
`fuel_transactions` by date. As delivered, Compose runs everything on one machine.

**Known limitations.**

- **Keycloak runs in development mode** with its built-in database. Users and changes live inside the container
  and are lost when it is recreated (the realm is then imported again from `keycloak/fuelguard-realm.json`).
  A real deployment would run Keycloak with its own PostgreSQL and HTTPS.
- **Demo credentials.** The passwords come from `.env`; the values in `.env.example` are placeholders, so change
  them. The `fuelguard-manager` client also allows the password grant so scripts can get a token without a
  browser. That grant is discouraged in OAuth 2.1; turn it off in the realm file for production.
- **Tokens cannot be revoked instantly.** A token is valid until it expires (5 minutes), and the API caches
  Keycloak's signing keys for up to 5 minutes, so tokens from a just-replaced key can still be accepted briefly.
- **Terminals renew their own tokens.** The client credentials flow gives no refresh token; a terminal asks for a
  new token when the old one expires.
- **The audit trail covers card changes only.** Reviews and notification reads do not yet record who did them.
- **A job that fails for a non-temporary reason** is not retried by Celery, but the recovery job will
  re-queue it every 2 minutes and it will fail again, with a log line each time. It is visible in
  `/transactions/stuck`, but there is no alerting or dead-letter queue.
- **Notifications are a log line plus a database row.** Email, chat or a webhook would be the next step.
- **No rate limiting, metrics or tracing.**
- **Reports are by UTC day.** Per-timezone reports would need a company time zone setting.
- **Tank check is simple.** It compares one purchase with the tank size; the current fuel level is not known.
- **Old data is never archived** (flags, notifications, history grow without limit).
