# AI Business Operator - Clothing Retail (Phase 1)

Phase 1 is the **business backend only**: PostgreSQL schema + FastAPI APIs + deterministic demo data.
There are no agents yet; the layering is arranged so LangChain tools can call the service layer later.

```
api (FastAPI routers)  ->  services (business rules, transactions)  ->  repositories (SQL)  ->  models
```

`app/services/*` depend only on an `AsyncSession`, so a future LangChain tool can do
`async with SessionLocal() as s: await SaleService(s).summary(filters)` with no HTTP in between.
`app/ai/` holds the assistant: `tools/` (read-only sales and inventory tools), `agents/` (the tool-calling agent),
`actions/` (action planning and validation) and `graphs/` (the LangGraph workflow and its PostgreSQL checkpointer).

### AI chat and human approval

`POST /api/v1/ai/chat` answers questions as before. A request to change something (Phase 2: only `CREATE_CAMPAIGN`) is
planned, validated (discount 0-50%, existing product category, required fields) and paused for approval:
`status: WAITING_APPROVAL` with the `proposed_action` and a `thread_id`. Then
`POST /api/v1/ai/workflows/{thread_id}/resume` with `{"decision": "APPROVED" | "REJECTED"}` gives
`APPROVED_PENDING_EXECUTION` or `REJECTED`. Nothing is executed: no campaign is created yet.

Workflow state lives in PostgreSQL (LangGraph checkpoint tables, created on startup in the app database, not by
Alembic), so a waiting workflow survives a server restart. On Windows, psycopg's async mode needs the selector event
loop, hence `--loop asyncio:SelectorEventLoop` in the run command above; without it startup logs a clear error and the
`/ai` endpoints answer 503 while the rest of the API keeps working.

## Local setup (Windows / PowerShell)

PostgreSQL must already be running on `localhost:5432`. Nothing here installs or starts a server.

```powershell
# 1. virtual environment
python -m venv .venv

# 2. dependencies (app + test tools)
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 3. configuration - copy the template, then edit DATABASE_URL if your credentials differ
Copy-Item .env.example .env
#    DATABASE_URL=postgresql+asyncpg://postgres:root@localhost:5432/ai_business_operator

# 4. verify the PostgreSQL connection
.\.venv\Scripts\python.exe -m scripts.check_connection

# 5. create the application database if it is missing (never drops or alters an existing one)
.\.venv\Scripts\python.exe -m scripts.create_database

# 6. create the tables with Alembic (the app never creates tables at startup)
.\.venv\Scripts\python.exe -m alembic upgrade head

# 7. load demo data (idempotent - safe to repeat)
.\.venv\Scripts\python.exe -m scripts.seed_data

# 8. start the API
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000 --loop asyncio:SelectorEventLoop

# 9. open Swagger UI:  http://localhost:8000/docs      (ReDoc: /redoc)

# 10. run the tests (separate database - see below)
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing   # with coverage
```

(`.venv\Scripts\Activate.ps1` also works if you prefer an activated shell; if PowerShell blocks it, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or just use the full paths above.)

## Endpoints (`/api/v1`)

| Area | Endpoint | Notes |
|---|---|---|
| Products | `GET /products` | filters: `category`, `size`, `color` (exact, case-insensitive), `search` (name/SKU substring); paginated |
| | `GET /products/{id}`, `POST /products`, `PATCH /products/{id}` | `POST` also creates the inventory row (`initial_quantity`, `reorder_level`); SKU is immutable |
| Inventory | `GET /inventory` | same filters as products; paginated |
| | `GET /inventory/low-stock` | `quantity <= reorder_level`, largest shortfall first |
| | `GET /inventory/{product_id}`, `PATCH /inventory/{product_id}` | set `quantity` and/or `reorder_level` |
| Sales | `GET /sales` | filters: `date_from`, `date_to` (inclusive UTC days), `category`, `product_id`; newest first |
| | `POST /sales` | `{product_id, quantity}`; price comes from the product; stock is decremented in the same transaction |
| | `GET /sales/summary` | revenue, units, sale count, average sale value, per-category breakdown |
| | `GET /sales/trends` | `granularity=day\|week\|month`; bucketed in PostgreSQL, empty periods returned as zeros |
| Customers | `GET /customers`, `POST /customers` | filters: `marketing_consent`, `search` |
| Campaigns | `GET /campaigns`, `GET /campaigns/{id}`, `POST /campaigns` | creation always stores `status = "draft"`; nothing is sent |

Conventions
- Lists return `{items, total, page, page_size}` (`page` >= 1, `page_size` 1-200, default 50).
- Errors always look like `{"error": {"code", "message", "details"}}`: `validation_error` / `invalid_request` (422),
  `not_found` (404), `conflict` and `insufficient_stock` (409), `internal_error` (500, no internals leaked).
- Money is `Decimal` end to end (`NUMERIC(12,2)`), serialized as exact strings such as `"89.00"`. Timestamps are UTC.

### How overselling is prevented
`POST /sales` runs in one transaction: it `SELECT ... FOR UPDATE`s the product's inventory row, checks stock, decrements,
inserts the sale, then commits. Concurrent sales of the same product queue on that row lock, so stock can never go
negative (a `CHECK (quantity >= 0)` is the last line of defence). Any failure rolls back the decrement and the sale together.

## Demo data

`python -m scripts.seed_data` inserts 150 SKUs (+ inventory), ~2,000 sales over the 90 days before `--as-of`
(default: today, UTC), 100 fictional customers (`@example.com`, phones in the reserved fictional 555-01xx range) and
5 draft campaigns. IDs are derived deterministically and inserts use `ON CONFLICT DO NOTHING`, so re-running adds nothing.
The sales history is dated relative to `--as-of`; to refresh it for a new day run
`python -m scripts.seed_data --reset-demo` (removes only the demo products, their sales/inventory, and the demo
customers/campaigns - rows you created yourself are left alone, unless you sold demo products, whose sales are removed).

Planted scenarios for later AI agents to find:
1. **Jeans sales fall** over the latest 14 days (about -58% units vs the 14 days before).
2. **Popular jeans sizes (32, 34) run out**: 8 SKUs at 0 and 4 nearly gone - the 12 items `GET /inventory/low-stock` returns.
3. **Linen Summer Shirt is overstocked** (320-520 units vs a reorder level of 15) and slow-moving.
4. **T-shirt sales stay flat.**
5. **Overall revenue declines** over the latest two weeks, driven by jeans.

Inventory quantities are scenario "current state" values; they are not derived by replaying the sales history.

## Database and migrations
- Tables: `products`, `inventory`, `sales`, `customers`, `campaigns` (UUID primary keys, `TIMESTAMPTZ`, CHECK constraints
  for prices/quantities/discount range/campaign status, FKs, and indexes for the filters and date queries).
- Migrations live in `alembic/versions`. After changing a model: `python -m alembic revision --autogenerate -m "message"`
  then `python -m alembic upgrade head`.

## Tests
`pytest` creates and migrates `ai_business_operator_test` (or `TEST_DATABASE_URL`) on its own, truncates it between tests,
and **refuses to run against any database whose name does not end in `_test`** - the application database is never touched.
Covered: CRUD, validation, stock rules, 25-way concurrent sales, rollback on mid-transaction failure, revenue maths,
date-bucketed trends, low-stock queries, seed idempotency and the planted scenarios.

## Layout
```
app/
  main.py            FastAPI app factory
  core/              config (.env), async engine/session, error envelope
  models/            SQLAlchemy 2.0 models
  schemas/           Pydantic v2 request/response models
  repositories/      SQL queries (parameterized; aggregation in PostgreSQL)
  services/          business logic + transactions (reusable by LangChain tools)
  api/v1/            routers
  ai/                placeholders for future agents / tools / graphs
alembic/             migrations
scripts/             check_connection, create_database, seed_data
tests/
```
