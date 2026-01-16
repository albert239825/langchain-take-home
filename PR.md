# PR message for ingest optimization

## Section 0 - baseline & constraints

Must keep: POST /runs (batch create), GET /runs/{id} (fetch single)
Must keep: entire batch written to object storage

### Benchmarks
GET 10kb: ~102 ms
GET 100kb: ~113 ms
POST 50×100kb: ~550 ms
POST 500×10kb: ~1900 ms

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