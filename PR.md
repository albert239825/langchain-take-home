# PR message for ingest optimization

## Section 0 - baseline & constraints

Must keep: POST /runs (batch create), GET /runs/{id} (fetch single)
Must keep: entire batch written to object storage

### Benchmarks
GET 10kb: ~102 ms
GET 100kb: ~113 ms
POST 50×100kb: ~550 ms
POST 500×10kb: ~1900 ms

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