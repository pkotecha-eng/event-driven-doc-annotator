import hashlib
import json
import logging
import os
import uuid
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

load_dotenv()

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any, Optional

from app.database import Base, engine, get_db, SessionLocal
from app.models import AnnotationJob, JobStatus
from app.worker import run_annotation

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create tables on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Spruce Doc Annotator",
    description="Event-driven document annotation service for financial documents",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Thread pool for background annotation work
executor = ThreadPoolExecutor(max_workers=int(os.getenv("WORKER_THREADS", "4")))

ALLOWED_EXTENSIONS = {".pdf", ".csv", ".xlsx", ".xls"}
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_BYTES", str(10 * 1024 * 1024)))  # 10MB default


# --- Response schemas ---

class UploadResponse(BaseModel):
    job_id: str
    status: str
    filename: str
    message: str


class AnnotationResponse(BaseModel):
    job_id: str
    status: str
    filename: str
    annotation: Optional[Any] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# --- Endpoints ---

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload", response_model=UploadResponse, status_code=202)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """
    Accept a document upload. Returns a job_id immediately.
    Processing happens asynchronously in the background.
    """
    from pathlib import Path
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{suffix}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}"
        )

    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max size is {MAX_FILE_SIZE // (1024*1024)}MB"
        )

    # Idempotency: check if we've seen this exact file before
    file_hash = hashlib.sha256(content).hexdigest()
    existing = db.query(AnnotationJob).filter(
        AnnotationJob.file_hash == file_hash,
        AnnotationJob.status.in_([JobStatus.complete, JobStatus.processing])
    ).first()

    if existing:
        return UploadResponse(
            job_id=existing.id,
            status=existing.status,
            filename=existing.filename,
            message="Duplicate file detected — returning existing job"
        )

    job_id = str(uuid.uuid4())
    job = AnnotationJob(
        id=job_id,
        filename=file.filename,
        file_hash=file_hash,
        status=JobStatus.pending
    )
    db.add(job)
    db.commit()

    # Submit to thread pool — fire and forget
    executor.submit(run_annotation, job_id, file.filename, content, SessionLocal)

    logger.info(f"Job {job_id} queued for {file.filename}")

    return UploadResponse(
        job_id=job_id,
        status=JobStatus.pending,
        filename=file.filename,
        message="Document accepted. Poll /jobs/{job_id} for results."
    )


@app.get("/jobs/{job_id}", response_model=AnnotationResponse)
def get_job(job_id: str, db: Session = Depends(get_db)):
    """
    Retrieve annotation results by job ID.
    Status will be: pending | processing | complete | failed
    """
    job = db.query(AnnotationJob).filter(AnnotationJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    annotation = json.loads(job.annotation) if job.annotation else None

    return AnnotationResponse(
        job_id=job.id,
        status=job.status,
        filename=job.filename,
        annotation=annotation,
        error=job.error,
        created_at=str(job.created_at) if job.created_at else None,
        updated_at=str(job.updated_at) if job.updated_at else None,
    )


@app.get("/jobs")
def list_jobs(limit: int = 20, db: Session = Depends(get_db)):
    """List recent annotation jobs."""
    jobs = db.query(AnnotationJob).order_by(AnnotationJob.created_at.desc()).limit(limit).all()
    return [
        {
            "job_id": j.id,
            "filename": j.filename,
            "status": j.status,
            "created_at": str(j.created_at) if j.created_at else None,
        }
        for j in jobs
    ]
