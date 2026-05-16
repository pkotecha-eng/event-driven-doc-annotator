from sqlalchemy import Column, String, Text, DateTime, Enum as SAEnum
from sqlalchemy.sql import func
from app.database import Base
import enum


class JobStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    complete = "complete"
    failed = "failed"


class AnnotationJob(Base):
    __tablename__ = "annotation_jobs"

    id = Column(String, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    file_hash = Column(String, nullable=True, index=True)  # idempotency
    status = Column(SAEnum(JobStatus), default=JobStatus.pending, nullable=False)
    annotation = Column(Text, nullable=True)   # JSON blob
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
