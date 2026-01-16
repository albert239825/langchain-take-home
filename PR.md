# PR message for ingest optimization

## Section 0 - baseline & constraints

Must keep: POST /runs (batch create), GET /runs/{id} (fetch single)
Must keep: entire batch written to object storage

### Benchmarks
GET 10kb: ~102 ms (baseline)
GET 100kb: ~108 ms (baseline)
POST 50×100kb: ~549 ms (baseline)
POST 500×10kb: ~1899 ms (baseline)

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

## Section 2: Fixing looped batched_data.find()

## Feature: Advance find() window for field offsets

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
- **Notes:** Significant improvement in POST 500×10kb (63% reduction in time) due to eliminating the O(N×batch_size) quadratic search. GET remains stable as expected.




## Section 3: Batch insert runs with COPY

## Feature: Collapse N inserts into one COPY

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
- **Notes:** POST 500×10kb shows 36% reduction by eliminating 500 INSERT round-trips. Single COPY operation replaces N database queries.

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