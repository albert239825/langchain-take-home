# PR message for ingest optimization

## Section 0 - baseline & constraints

Must keep: POST /runs (batch create), GET /runs/{id} (fetch single)
Must keep: entire batch written to object storage

### Benchmarks
GET 10kb: ~102 ms (baseline)
GET 100kb: ~108 ms (baseline)
POST 50×100kb: ~549 ms (baseline)
POST 500×10kb: ~1899 ms (baseline)

Benchmark artifact: `.benchmarks/Darwin-CPython-3.11-64bit/0001_initial_baseline.json`

Intuition: lot of overhead per call, evident by the fact that GET 10kb vs GET 100kb
are close to performance and more POST calls are severely worse despite the same
amount of total data being written

### Data Model & Hybrid Storage Architecture

#### Core Data Model
Each **Run** represents a LangChain execution trace:
- **Lightweight metadata**: `id`, `trace_id`, `name` (stored in PostgreSQL)
- **Heavy payload fields**: `inputs`, `outputs`, `metadata` (JSON dictionaries, potentially 100KB+ each)

#### Storage Strategy: Two-Tier Hybrid System

**Tier 1: PostgreSQL (Index)**
- Stores lightweight metadata and **S3 reference strings**
- S3 references encode: `s3://bucket/key#start_byte:end_byte/field_name`
- Example: `s3://runs/batches/xyz.json#1024:5120/inputs`
- Keeps database lean and queries fast

**Tier 2: S3/MinIO (Data Lake)**
- Stores actual run data as batch JSON files
- One batch file contains multiple runs
- Accessed via HTTP Range requests for efficient partial reads

### Mapping out the dataflow
POST /runs
Request → FastAPI (in-memory parse) → List[Run] Pydantic models
  ↓
Convert to dicts → Serialize entire batch to JSON bytes
  ↓
1 S3 PUT (entire batch)
  ↓
For each run (loop):
  - Serialize each field (inputs/outputs/metadata) individually
  - Use string.find() to locate field in batch JSON
  - Calculate byte offsets
  - 1 DB INSERT per run (N total inserts)

GET /runs/{id}
Request with run_id
  ↓
1 DB SELECT query → returns S3 references (strings with byte offsets)
  ↓
Parse S3 references (extract bucket, key, byte ranges)
  ↓
3 parallel S3 GET requests (with Range: bytes=start-end):
  ├─ inputs field bytes
  ├─ outputs field bytes
  └─ metadata field bytes
  ↓
Parse each JSON fragment (orjson.loads)
  ↓
Assemble response dict

## Section 1: POST /runs bottlenecks (ranked) 

1) **`batch_data.find(field_json_data)` inside loop (Very High impact)**
   - Called **3×N** times; each call scans the **entire batch JSON blob**.
   - Roughly **O(3 × N × batch_size_bytes)** → explodes for `N=500`.
   - Also brittle: identical JSON fragments can match earlier occurrences → wrong offsets.

2) **One DB round-trip per run: `INSERT ... RETURNING` in a loop (Very High impact)**
   - **N inserts = N network round trips** (500 in the 500-run benchmark).
   - `RETURNING id` is unnecessary (we already have `run.id`) and adds overhead.
   - Fix: batch insert / COPY to collapse to ~1 DB operation.

3) **Repeated per-field serialization: `orjson.dumps(inputs/outputs/metadata)` (High impact)**
   - After serializing the whole batch once, we **re-serialize 3 big fields per run** (3×N extra work).
   - Adds CPU + allocation churn; ~15MB extra JSON serialization in both provided benchmarks.

4) **Pydantic `model_dump()` + building `run_dicts` (Medium impact)**
   - Materializes large nested dicts for every run before serialization.
   - Real cost for 100KB fields, but typically secondary to (1)-(3).

5) **S3 `put_object` of full batch (Medium, mostly unavoidable)**
   - Required by constraints (must write full batch). Can optimize around it (streaming/tempfile), but not eliminate.

6) **Misc overheads (Low)**
   - String formatting of refs, list appends, loop/indexing, etc. Not worth focusing on early.

## Section 2: Advance find() window for field offsets

### Motivation
- **Problem:** `batch_data.find(...)` scans the full batch for every field, and duplicates can match the wrong run.
- **Why it matters:** For large batches (`N=500`), it turns into a quadratic scan and incorrect offsets.
- **Evidence:**
  - `batch_data.find(...)` runs `3×N` times and scans the full batch blob → **O(N×batch_size)**
  - Identical JSON fragments (e.g., `{}`) can match earlier occurrences

### Change
- **Before:** serialize each field and search from byte 0 on every loop iteration.
- **After:** pre-serialize field blobs once, then search from an advancing `current_pos`.
- **Key idea:** keep a moving search window through `batch_data` to avoid rescans and ambiguity.

### Implementation notes
- **Files touched:** `ls_py_handler/api/routes/runs.py`
- **Schema changes (if any):** none
- **Correctness considerations:**
  - ensures identical field values resolve to the correct sequential occurrence
  - offsets now derived from the next match after the previous field, not from byte 0

### Results
- **Benchmarks (before → after):**
  - GET 10kb: 101.6 ms → 109.4 ms
  - GET 100kb: 108.3 ms → 105.7 ms
  - POST 50×100kb: 549.0 ms → 464.6 ms
  - POST 500×10kb: 1899.3 ms → 694.4 ms
- Benchmark artifact: `.benchmarks/Darwin-CPython-3.11-64bit/0002_advance_find_window.json`
- **Notes:** Significant improvement in POST 500×10kb (63% reduction in time) due to eliminating the O(N×batch_size) quadratic search. GET remains stable as expected.


## Section 3: Batch insert runs with COPY

### Motivation
- **Problem:** one `INSERT ... RETURNING` per run causes N round-trips and unnecessary `RETURNING` overhead.
- **Why it matters:** for N=500, the DB insert phase dominates wall time.
- **Evidence:**
  - `N` inserts → `N` network round-trips

### Change
- **Before:** `fetchval(INSERT ... RETURNING)` inside the run loop.
- **After:** collect all records in memory and bulk insert with `copy_records_to_table()`.
- **Key idea:** use PostgreSQL COPY to reduce inserts to a single round-trip.

### Implementation notes
- **Files touched:** `ls_py_handler/api/routes/runs.py`
- **Schema changes (if any):** none
- **Correctness considerations:**
  - preserves existing IDs by inserting the provided `run.id` values
  - avoids relying on `RETURNING` since IDs are already known

### Results
- **Benchmarks (before → after):**
  - GET 10kb: 109.4 ms → 107.6 ms
  - GET 100kb: 105.7 ms → 110.0 ms
  - POST 50×100kb: 464.6 ms → 434.7 ms
  - POST 500×10kb: 694.4 ms → 443.9 ms
- Benchmark artifact: `.benchmarks/Darwin-CPython-3.11-64bit/0003_batch_insert_copy.json`
- **Notes:** POST 500×10kb shows 36% reduction by eliminating 500 INSERT round-trips. Single COPY operation replaces N database queries.

## Section 4: Eliminate redundant per-field serialization

### Motivation
- **Problem:** inputs/outputs/metadata are serialized once in the full batch and then serialized again per run for offset lookup.
- **Why it matters:** for large batches this is 3×N extra JSON serialization work and allocation churn.
- **Evidence:**
  - `orjson.dumps(inputs/outputs/metadata)` runs `3×N` times after a full batch serialization.

### Change
- **Before:** serialize the entire batch, then re-serialize each field and search bytes to find offsets.
- **After:** build the batch JSON incrementally while tracking offsets, serializing each field only once.
- **Key idea:** compute offsets while writing, not by searching or re-serializing.

### Implementation notes
- **Files touched:** `ls_py_handler/api/routes/runs.py`, `ls_py_handler/utils/batch_serializer.py`
- **Schema changes (if any):** none
- **Correctness considerations:**
  - offsets come from the exact write position, avoiding ambiguity when JSON fragments repeat
  - JSON structure matches prior output ordering for `id`, `trace_id`, `name`, `inputs`, `outputs`, `metadata`

### Results
- **Benchmarks (before → after):**
  - GET 10kb: 107.6 ms → 109.8 ms
  - GET 100kb: 110.0 ms → 117.8 ms
  - POST 50×100kb: 434.7 ms → 282.5 ms
  - POST 500×10kb: 443.9 ms → 325.4 ms
- Benchmark artifact: `.benchmarks/Darwin-CPython-3.11-64bit/0004_eliminate_redundant_serialization.json`
- **Notes:** POST latency drops by roughly 25–35%

## Section 5: GET /runs/{id} bottlenecks (ranked) 

1) **3 separate S3 Range GETs per request (Very High impact)**
   - Always issues **3 `get_object` calls** (inputs/outputs/metadata), even when they live in the same batch object.
   - Benchmarks show **10KB vs 100KB are close** → fixed per-request overhead dominates → reducing calls (3→1) is the biggest win.

2) **Potential lack of S3 client/session reuse (High→Medium impact)**
   - If `get_s3_client` creates a new client/session per request, you pay extra connection/pool overhead.
   - Reusing a long-lived aiobotocore session/client can materially cut GET latency.

3) **Decode/allocate 3× per request: `stream.read()` + `orjson.loads()` (Medium impact)**
   - Reads full fragment into memory and decodes JSON **three times**.
   - Likely secondary here (since payload size barely changes timings), but becomes a win if combined into one fetch + one decode.

4) **Reference parsing + per-request helper function definitions (Low impact)**
   - `split()` parsing + nested function creation + `dict(row)` conversion are minor compared to network/I/O.

## Section 6: Single Range GET per run

### Motivation
- **Problem:** GET `/runs/{id}` performs three S3 Range GETs (inputs/outputs/metadata) for every request.
- **Why it matters:** Fixed per-request S3 overhead dominates latency; eliminating two calls is the largest win.
- **Evidence:**
  - GET 10KB vs 100KB are similar → overhead is in request count, not payload size.

### Change
- **Before:** store three field-level S3 ranges in Postgres and fetch them in parallel.
- **After:** store a single run-level byte range (`start_offset`, `end_offset`) and fetch once.
- **Key idea:** record run offsets while building the batch JSON, then Range GET the full run object.

### Implementation notes
- **Files touched:** `ls_py_handler/utils/batch_serializer.py`, `ls_py_handler/api/routes/runs.py`
- **Schema changes (if any):** add `s3_key`, `start_offset`, `end_offset`; remove `inputs`, `outputs`, `metadata`
- **Correctness considerations:**
  - offsets are exact by construction (no substring search ambiguity)
  - batch format remains a JSON array

### Results
- **Benchmarks (before → after):**
  - GET 10kb: 109.8 ms → 107.1 ms
  - GET 100kb: 117.8 ms → 111.4 ms
  - POST 50×100kb: 282.5 ms → 284.1 ms
  - POST 500×10kb: 325.4 ms → 297.3 ms
- Benchmark artifact: `.benchmarks/Darwin-CPython-3.11-64bit/0005_single_range_get.json`
- **Notes:** Improvement is modest because the prior 3 Range GETs were done in parallel (critical path ≈ slowest request, not sum). Still reduces per-request S3 operations and JSON parses (3→1) and simplifies code.

## Section 7: Reuse S3 client across requests

### Motivation
- **Problem:** `get_s3_client()` creates a new aiobotocore session and client per request, then immediately closes them.
- **Why it matters:** Connection setup overhead affects both POST and GET latency.
- **Evidence:**
  - New TCP connection + SSL handshake + HTTP connection pool creation happens on every request

### Change
- **Before:** `get_s3_client()` yields a new client from a new session in an async context manager (per-request).
- **After:** Lifespan context manager creates one session/client at startup, stores in `app.state`, lightweight dependency retrieves it.
- **Key idea:** Reuse a single aiobotocore client throughout the application lifecycle.

### Implementation notes
- **Files touched:** `ls_py_handler/main.py`, `ls_py_handler/api/routes/runs.py`
- **Schema changes (if any):** none
- **Correctness considerations:**
  - Client properly closed on shutdown via `async with` in lifespan
  - Migrates from deprecated `@app.on_event("startup")` to modern `lifespan` pattern

### Results
- **Benchmarks (before → after):**
  - GET 10kb: 100.4 ms → 27.2 ms (73% faster)
  - GET 100kb: 106.1 ms → 32.3 ms (70% faster)
  - POST 50×100kb: 278.2 ms → 218.4 ms (21% faster)
  - POST 500×10kb: 302.7 ms → 227.7 ms (25% faster)
- Benchmark artifact: `.benchmarks/Darwin-CPython-3.11-64bit/0006_baseline.json`
- **Notes:** Dramatic improvements for GET requests (70%+ reduction) due to eliminating per-request connection setup. POST requests show 20-25% improvements. The shared connection pool and HTTP keep-alive provide substantial latency reduction across all endpoints.

## Section 8: Test infrastructure for long-lived S3 client

### Motivation
- **Problem:** The lifespan context manager only runs when FastAPI is started by an ASGI server, not during test client creation.
- **Why it matters:** Tests using `AsyncClient(app=app)` never initialize `app.state.s3_client`, causing `AttributeError`.
- **Evidence:**
  - `AsyncClient` does not trigger lifespan handlers automatically
  - Tests were failing with "'State' object has no attribute 's3_client'"

### Change
- **Before:** Each test file created its own inline `AsyncClient(app=app)` instance.
- **After:** Shared `client` fixture in `conftest.py` explicitly enters the lifespan context before creating the test client.
- **Key idea:** Manually manage the lifespan context in test fixtures to ensure `app.state` is properly initialized.

### Implementation notes
- **Files touched:** `tests/conftest.py` (new), `tests/test_runs.py`, `tests/benchmarks/test_run_performance.py`
- **Test fixture changes:**
  - Created `tests/conftest.py` with shared async client fixture
  - Fixture uses `async with lifespan(app):` to initialize app state before client creation
  - Uses `ASGITransport` for proper ASGI handling
- **Benchmark test adaptations:**
  - Simplified `aio_benchmark` to use existing event loop instead of creating new ones
  - Removed `asyncio.new_event_loop()` calls that caused "Future attached to different loop" errors
  - Changed benchmark tests from `async def` to `def` to avoid "event loop already running" errors
  - Used `loop.run_until_complete()` for GET test setup instead of `asyncio.run()`

### Correctness considerations
- Tests now properly initialize the long-lived S3 client exactly as production does
- Event loop remains consistent across lifespan context and test execution
- Benchmark measurements remain valid (same HTTP path, no artificial overhead)
- All tests pass with the shared client fixture

## Section x: feature fix
## Feature: <short name>  (e.g., “Eliminate O(N×batch_size) scans in POST”)

### Motivation
- **Problem:** <1 sentence describing what’s slow/brittle>
- **Why it matters:** <1 sentence tying to benchmarks / scaling / call count>
- **Evidence:** <1–2 bullets with concrete “counts” or complexity>
  - e.g., `batch_data.find(...)` runs `3×N` times and scans the full batch blob → **O(N×batch_size)**

### Change
- **Before:** <1–2 bullets describing old behavior>
- **After:** <1–2 bullets describing new behavior>
- **Key idea:** <one crisp statement, e.g. “compute offsets while writing, not by searching bytes”>

### Implementation notes
- **Files touched:** `<path1>`, `<path2>`
- **Schema changes (if any):** <new columns/tables + what they store>
- **Correctness considerations:** <1–2 bullets>
  - e.g., “avoids ambiguous matches when identical JSON fragments appear”
  - e.g., “offsets now derived from write position, not string search”

### Results
- **Benchmarks (before → after):**
  - GET 10kb: `<before>` → `<after>`
  - GET 100kb: `<before>` → `<after>`
  - POST 50×100kb: `<before>` → `<after>`
  - POST 500×10kb: `<before>` → `<after>`
- **Notes:** <1 line interpreting change; optional>