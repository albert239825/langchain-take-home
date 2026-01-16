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

## Section 1: Bottleneck Areas

### POST /runs bottlenecks
