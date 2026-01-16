# PR message for ingest optimization

## Section 0 - baseline & constraints

Must keep: POST /runs (batch create), GET /runs/{id} (fetch single)
Must keep: entire batch written to object storage

### Benchmarks
GET 10kb: ~102 ms
GET 100kb: ~113 ms
POST 50×100kb: ~550 ms
POST 500×10kb: ~1900 ms
