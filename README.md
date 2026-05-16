# Spruce Doc Annotator

An event-driven document annotation service for financial documents. Upload a PDF or spreadsheet and get back structured AI-extracted metadata — document type, summary, key entities, financial metrics, risk flags, and analyst follow-up questions.

---

## Quickstart

```bash
git clone <repo>
cd spruce-doc-annotator

# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your API key
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY

# 3. Start the API server
uvicorn app.main:app --reload

# 4. (Optional) Start the demo UI in a second terminal
streamlit run demo/app.py

# 5. Run tests
pytest tests/ -v
```

API docs available at `http://localhost:8000/docs` (FastAPI auto-generated OpenAPI).

---

## API

### `POST /upload`
Upload a document. Returns `job_id` immediately — processing is async.

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@sample_docs/sample_8k.pdf"
```

Response (202):
```json
{
  "job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "pending",
  "filename": "sample_8k.pdf",
  "message": "Document accepted. Poll /jobs/{job_id} for results."
}
```

### `GET /jobs/{job_id}`
Retrieve annotation results. Poll until `status` is `complete` or `failed`.

```bash
curl http://localhost:8000/jobs/3fa85f64-5717-4562-b3fc-2c963f66afa6
```

Response when complete:
```json
{
  "job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "complete",
  "filename": "sample_8k.pdf",
  "annotation": {
    "document_type": "8-K",
    "summary": "ACME Retail Holdings reported Q4 2024 results...",
    "key_entities": {
      "companies": ["ACME Retail Holdings"],
      "tickers": ["ACMR"],
      "people": [],
      "regulators": ["SEC"]
    },
    "financial_metrics": {
      "revenue": "$487.3M, +18% YoY",
      "eps": "$0.84 diluted",
      "guidance": "$2.15B-$2.22B FY2025"
    },
    "time_period": "Q4 2024 / FY2024",
    "sentiment": "positive",
    "risk_flags": ["Forward-looking statements disclaimer present"],
    "follow_up_questions": [
      "What is driving the gross margin expansion from 38.6% to 42.1%?",
      "How does same-store sales growth compare to peers?",
      "What is the capital allocation priority given the buyback authorization?"
    ],
    "confidence": "high",
    "_meta": {
      "model": "claude-haiku-4-5-20251001",
      "input_tokens": 1243,
      "output_tokens": 387
    }
  }
}
```

### `GET /jobs?limit=20`
List recent jobs with status.

### `GET /health`
Health check.

---

## Architecture

```
POST /upload
    │
    ├── Validate file type + size
    ├── SHA-256 hash → idempotency check
    ├── Write job (status: pending) → SQLite
    ├── Return job_id (202 Accepted)
    │
    └── executor.submit(run_annotation) ──► ThreadPoolExecutor
                                                │
                                    ┌───────────▼────────────┐
                                    │  worker.run_annotation  │
                                    │                         │
                                    │  1. Mark → processing   │
                                    │  2. Extract text        │
                                    │     (PDF/CSV/XLSX)      │
                                    │  3. Call Claude API     │
                                    │  4. Parse JSON output   │
                                    │  5. Persist annotation  │
                                    │  6. Mark → complete     │
                                    │     (or → failed)       │
                                    └─────────────────────────┘

GET /jobs/{id}
    └── Read AnnotationJob → return status + annotation JSON
```

### Job State Machine

```
pending → processing → complete
                    ↘ failed
```

Every state transition is persisted. If the worker crashes mid-flight, the job stays in `processing` — visible to an operator, recoverable via retry logic (see Production section).

---

## Design Decisions

### Why async job queue instead of synchronous processing?

LLM inference takes 5–30 seconds depending on document size and model. HTTP request timeouts (default 30s on many proxies/clients) make synchronous processing unreliable. The async job pattern — accept immediately, return ID, poll for result — is the standard solution. It also naturally decouples upload throughput from processing throughput.

### Why `ThreadPoolExecutor` instead of Celery/Redis/Temporal?

For an 8-hour challenge, Celery adds meaningful setup overhead (broker, worker process, result backend) without changing the observable behavior. `ThreadPoolExecutor` gives true async processing with zero infrastructure dependencies. The abstraction boundary is clean: `executor.submit(run_annotation, ...)` can be swapped for a Celery task `.delay()` call in one line. I called this out explicitly rather than hiding it.

In production, I'd use Temporal for durable execution (see Production section).

### Why SQLite over Postgres?

SQLite means zero infrastructure — `git clone` + `pip install` + `uvicorn` and it runs. The reviewer can verify behavior in 2 minutes without standing up a database. SQLAlchemy abstracts the driver, so the production swap is a one-line `DATABASE_URL` change. The schema is designed for Postgres from the start: proper indexes, composite unique keys, enum types, timestamps.

### Why Claude Haiku as default model?

Haiku is fast (~2-3s), cheap (~$0.25/MTok input), and produces clean structured JSON. For a document annotation workload — well-defined schema, no multi-step reasoning — Haiku is the right tier. Sonnet or Opus would be overkill and ~10x more expensive. The model is configurable via `ANTHROPIC_MODEL` env var for cases where richer reasoning is needed.

### Why a single annotation agent?

The task is document → structured metadata. There's no multi-step reasoning chain, no tool use, no inter-agent coordination required. A single well-prompted call with structured output is faster, cheaper, and more debuggable than a multi-agent pipeline. I'd add agents when the task requires it — e.g., an 8-K agent that cross-references the company's prior filings, or a regulatory delta agent that compares against a policy database.

### Idempotency

Duplicate uploads are detected via SHA-256 hash of file content. If a `complete` or `processing` job exists for the same file, the existing job ID is returned rather than spawning a new worker. This prevents redundant LLM spend and duplicate records.

### Text extraction design

- **PDFs**: `pypdf` extracts text per-page, capped at 15,000 characters to prevent token blowout on large filings
- **CSV**: First 50 rows extracted with column count and row count metadata surfaced to the LLM
- **XLSX**: Per-sheet extraction, first 50 rows per sheet
- All extractors return an `extract_error` field rather than throwing — the worker logs the warning and continues with whatever text is available

### Why a unified annotation schema for both PDFs and spreadsheets?

All documents — regardless of format — return the same JSON schema: 
document type, summary, key entities, financial metrics, risk flags, 
follow-up questions, confidence. 

This is intentional. Whatever downstream agent consumes this output — 
an 8-K impact note drafter, a regulatory delta router, a Monday morning 
screening orchestrator — it gets a consistent interface regardless of 
source format. PDFs and CSVs differ in how fields are populated (a 
screening CSV synthesizes metrics across rows; an 8-K extracts point 
values from prose) but the contract is identical. That's what makes 
this composable into a shared orchestration layer.

---

## Sample Documents

`sample_docs/` contains two representative files:

- `sample_8k.pdf` — Form 8-K earnings release for a fictional retailer (ACMR), including Q4 financials, guidance, and forward-looking statement boilerplate
- `sample_screening_data.csv` — Russell 2000-style screening output with 10 tickers, financial metrics, analyst ratings, and research notes

These mirror the document types Spruce House's existing pipelines produce and consume.

> **Note on real 8-K documents**: SEC filings on EDGAR are submitted as HTML, 
> not PDF. In production, documents would be fetched directly from EDGAR and 
> parsed as HTML (via BeautifulSoup), or retrieved as pre-converted PDFs via 
> sec-api.io. The sample PDF here simulates the document structure for demo purposes.

---

## Test Coverage

```bash
pytest tests/ -v
```

9 tests covering:
- Health check
- CSV and PDF upload returning `job_id`
- Unsupported file type rejection (415)
- File size limit enforcement
- Idempotency (duplicate upload returns same job)
- Job not found (404)
- Pending job returns null annotation
- Completed job returns full annotation structure
- Job list endpoint

All tests use `TestClient` with an in-memory SQLite DB and mock the LLM worker — no API key required to run tests.

---

## What I'd Improve With Another Day

1. **Retry logic**: Jobs stuck in `processing` after a crash should be automatically retried. A background sweep task checking for stale `processing` jobs (updated_at > N minutes ago) would handle this.

2. **Webhook / push callback**: Instead of polling, accept an optional `callback_url` on upload. POST the annotation result when complete. Reduces client complexity.

3. **Per-document chunking**: Long PDFs (10-K, 100+ pages) exceed comfortable context windows. A chunking strategy — extract, summarize per-section, then synthesize — would handle large documents more accurately.

4. **Structured output via tool use**: Rather than prompting for JSON and parsing, use Claude's tool use / structured output feature for guaranteed schema compliance.

5. **File storage**: Currently content is passed in memory to the worker. For production, upload to S3/GCS first, store the object key in the job record, and have the worker fetch it. This decouples upload from processing and enables reprocessing.

6. **Auth**: No authentication on any endpoint. Production needs API key or JWT middleware.

---

## Production Readiness

### Failure handling

| Failure mode | Current behavior | Production fix |
|---|---|---|
| LLM API timeout | Job marked `failed`, error stored | Retry with exponential backoff (3 attempts) |
| LLM returns invalid JSON | Exception caught, job `failed` | Retry with stricter prompt; fallback to partial extraction |
| Worker process crash mid-job | Job stays `processing` | Stale job sweep + automatic requeue |
| File extraction fails | Error field set, empty text sent to LLM | Fail fast with descriptive error; don't burn tokens on empty input |
| DB write fails | Unhandled exception | Wrap in retry; alert on repeated failure |

### Idempotency

SHA-256 content hash prevents duplicate processing. In production, hash check should happen before writing the job record (currently there's a small TOCTOU window). Wrap in a DB transaction with unique constraint on `file_hash` for status in (`complete`, `processing`).

### Observability

What I'd instrument:

- **Structured logging**: Every state transition logged with `job_id`, `filename`, `status`, `duration_ms`
- **Token tracking**: Input/output token counts already stored in `_meta`. Aggregate to track cost per document type over time.
- **Job queue depth**: Alert if `pending` jobs older than 60s exist — worker may be backed up or crashed
- **Error rate**: Alert if `failed` / `total` > threshold in rolling window
- **Latency p50/p95**: Track time from upload to `complete` per document type

Stack: structured JSON logs → CloudWatch / Datadog. Job state events → a lightweight events table for audit trail.

### Cost

At current Haiku pricing (~$0.25/MTok input, ~$1.25/MTok output):
- Typical 8-K PDF: ~1,200 input tokens, ~400 output tokens → ~$0.0008/document
- Typical screening CSV: ~800 input tokens, ~350 output tokens → ~$0.0006/document
- At 1,000 documents/day → ~$0.80/day

Cost controls:
- Text truncation at 15,000 chars prevents runaway token usage on large documents
- Model tier configurable — Haiku for standard annotation, Sonnet for complex reasoning tasks
- Token counts stored per job → cost attribution by document type, user, or pipeline

### Scaling

Current architecture scales to ~50 concurrent workers on a single machine (ThreadPoolExecutor). Beyond that:

- **Job queue**: Replace `ThreadPoolExecutor` with Celery + Redis or **Temporal** (preferred for hedge fund use case — durable execution, built-in retry, human-in-the-loop checkpoints, audit trail)
- **DB**: Swap `DATABASE_URL` to Postgres — zero code changes required
- **API**: FastAPI is production-grade as-is; deploy behind gunicorn with multiple workers
- **Storage**: S3 for document storage, presigned URLs for retrieval

### Why this architecture fits the Spruce House context

This service is designed to be a composable building block, not a standalone tool. The job state machine, clean API surface, and structured annotation schema are all designed to plug into a shared orchestration layer. The annotation output (document type, entities, risk flags, follow-up questions) is exactly the kind of structured context that feeds downstream agents — an 8-K impact note drafter, a regulatory delta router, or a Monday morning screening orchestrator.

---

## Stack

| Layer | Choice | Rationale |
|---|---|---|
| API | FastAPI | Async-native, auto OpenAPI docs, Python-native for AI stack |
| Background processing | ThreadPoolExecutor | Zero infra, swappable for Celery/Temporal |
| Database | SQLite + SQLAlchemy | Zero setup, Postgres-ready schema |
| PDF extraction | pypdf | Lightweight, no external dependencies |
| Spreadsheet extraction | openpyxl | Standard xlsx support |
| LLM | Claude Haiku | Fast, cheap, clean JSON output |
| Demo UI | Streamlit | Fastest path to interactive demo |
| Tests | pytest + httpx | Standard, no real server or API key needed |
