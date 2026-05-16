"""
Tests for the document annotation service.
Uses TestClient (no real server needed) and mocks the LLM worker.
"""
import json
import io
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Use in-memory SQLite for tests
TEST_DB_URL = "sqlite:///./test_annotations.db"

from app.database import Base, get_db
from app.main import app

# Override DB for tests
test_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def client():
    return TestClient(app)


def make_pdf_bytes():
    """Minimal valid PDF bytes."""
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\nxref\n0 1\n0000000000 65535 f\ntrailer\n<< /Size 1 /Root 1 0 R >>\nstartxref\n9\n%%EOF"


def make_csv_bytes():
    return b"ticker,company,revenue\nACMR,ACM Research,412\nHALO,Halozyme,891\n"


# --- Health check ---

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# --- Upload endpoint ---

def test_upload_csv_returns_job_id(client):
    with patch("app.main.executor") as mock_executor:
        mock_executor.submit = MagicMock()
        resp = client.post(
            "/upload",
            files={"file": ("test.csv", make_csv_bytes(), "text/csv")}
        )
    assert resp.status_code == 202
    data = resp.json()
    assert "job_id" in data
    assert data["status"] == "pending"
    assert data["filename"] == "test.csv"


def test_upload_pdf_returns_job_id(client):
    with patch("app.main.executor") as mock_executor:
        mock_executor.submit = MagicMock()
        resp = client.post(
            "/upload",
            files={"file": ("test.pdf", make_pdf_bytes(), "application/pdf")}
        )
    assert resp.status_code == 202
    data = resp.json()
    assert "job_id" in data
    assert data["status"] == "pending"


def test_upload_unsupported_type_rejected(client):
    resp = client.post(
        "/upload",
        files={"file": ("test.txt", b"hello world", "text/plain")}
    )
    assert resp.status_code == 415


def test_upload_idempotency(client):
    """Uploading the same file twice returns the same job."""
    with patch("app.main.executor") as mock_executor:
        mock_executor.submit = MagicMock()
        content = make_csv_bytes()

        resp1 = client.post("/upload", files={"file": ("data.csv", content, "text/csv")})
        assert resp1.status_code == 202
        job_id_1 = resp1.json()["job_id"]

        # Manually mark the first job complete so idempotency check fires
        db = TestSessionLocal()
        from app.models import AnnotationJob, JobStatus
        job = db.query(AnnotationJob).filter(AnnotationJob.id == job_id_1).first()
        job.status = JobStatus.complete
        job.annotation = json.dumps({"document_type": "spreadsheet", "summary": "test"})
        db.commit()
        db.close()

        resp2 = client.post("/upload", files={"file": ("data.csv", content, "text/csv")})
        assert resp2.status_code == 202
        job_id_2 = resp2.json()["job_id"]

        assert job_id_1 == job_id_2
        assert "Duplicate" in resp2.json()["message"]


# --- Job retrieval endpoint ---

def test_get_job_not_found(client):
    resp = client.get("/jobs/nonexistent-id")
    assert resp.status_code == 404


def test_get_job_pending(client):
    with patch("app.main.executor") as mock_executor:
        mock_executor.submit = MagicMock()
        upload = client.post("/upload", files={"file": ("test.csv", make_csv_bytes(), "text/csv")})
        job_id = upload.json()["job_id"]

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["job_id"] == job_id
    assert data["annotation"] is None


def test_get_job_complete(client):
    """Simulate a completed job and verify annotation is returned."""
    with patch("app.main.executor") as mock_executor:
        mock_executor.submit = MagicMock()
        upload = client.post("/upload", files={"file": ("test.csv", make_csv_bytes(), "text/csv")})
        job_id = upload.json()["job_id"]

    # Simulate worker completing the job
    db = TestSessionLocal()
    from app.models import AnnotationJob, JobStatus
    job = db.query(AnnotationJob).filter(AnnotationJob.id == job_id).first()
    job.status = JobStatus.complete
    job.annotation = json.dumps({
        "document_type": "screening_data",
        "summary": "Russell 2000 screening data with 10 tickers.",
        "key_entities": {"tickers": ["ACMR", "HALO"], "companies": ["ACM Research", "Halozyme"]},
        "sentiment": "neutral",
        "confidence": "high"
    })
    db.commit()
    db.close()

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "complete"
    assert data["annotation"]["document_type"] == "screening_data"
    assert "ACMR" in data["annotation"]["key_entities"]["tickers"]


def test_list_jobs(client):
    with patch("app.main.executor") as mock_executor:
        mock_executor.submit = MagicMock()
        client.post("/upload", files={"file": ("a.csv", make_csv_bytes(), "text/csv")})
        client.post("/upload", files={"file": ("b.csv", b"x,y\n1,2\n", "text/csv")})

    resp = client.get("/jobs?limit=10")
    assert resp.status_code == 200
    jobs = resp.json()
    assert len(jobs) == 2
