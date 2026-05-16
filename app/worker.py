import json
import logging
import os
import re

from anthropic import Anthropic
from app.extractor import extract_text

logger = logging.getLogger(__name__)

client = Anthropic()

ANNOTATION_PROMPT = """You are a financial document analyst. Analyze the following document and return a JSON object with these fields:

{{
  "document_type": "one of: 8-K, 10-K, 10-Q, earnings_call, regulatory_filing, screening_data, spreadsheet, research_note, unknown",
  "summary": "2-4 sentence summary of the document's core content",
  "key_entities": {{
    "companies": ["list of company names mentioned"],
    "tickers": ["list of stock tickers mentioned"],
    "people": ["list of named individuals"],
    "regulators": ["list of regulatory bodies mentioned"]
  }},
  "financial_metrics": {{
    "revenue": "if mentioned",
    "eps": "if mentioned",
    "guidance": "forward guidance if mentioned"
  }},
  "time_period": "reporting period or date range if identifiable",
  "sentiment": "one of: positive, negative, neutral, mixed",
  "risk_flags": ["list of any regulatory risks, material events, or red flags"],
  "follow_up_questions": ["1-3 questions a financial analyst might want to investigate next"],
  "confidence": "one of: high, medium, low — based on document clarity and completeness"
}}

Return ONLY the JSON object. No markdown, no explanation.

Document filename: {filename}
Document format: {doc_format}
{extra_meta}

Document content:
{text}
"""


def run_annotation(job_id: str, filename: str, content: bytes, db_session_factory):
    """
    Background worker: extract text, run LLM annotation, persist result.
    Called in a background thread — creates its own DB session.
    """
    db = db_session_factory()
    try:
        from app.models import AnnotationJob, JobStatus

        # Mark as processing
        job = db.query(AnnotationJob).filter(AnnotationJob.id == job_id).first()
        if not job:
            logger.error(f"Job {job_id} not found in DB")
            return

        job.status = JobStatus.processing
        db.commit()

        # Extract text
        extracted = extract_text(filename, content)
        text = extracted.get("text", "")
        doc_format = extracted.get("doc_format", "unknown")

        extra_meta_parts = []
        if extracted.get("page_count"):
            extra_meta_parts.append(f"Pages: {extracted['page_count']}")
        if extracted.get("row_count"):
            extra_meta_parts.append(f"Rows: {extracted['row_count']}, Columns: {extracted['col_count']}")
        if extracted.get("sheet_count"):
            extra_meta_parts.append(f"Sheets: {extracted['sheet_count']}")
        if extracted.get("extract_error"):
            extra_meta_parts.append(f"Extraction warning: {extracted['extract_error']}")
        extra_meta = "\n".join(extra_meta_parts)

        if not text.strip():
            raise ValueError("Could not extract text from document")

        # Call Claude
        prompt = ANNOTATION_PROMPT.format(
            filename=filename,
            doc_format=doc_format,
            extra_meta=extra_meta,
            text=text
        )

        response = client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE)
      
        raw = raw.strip()

        annotation = json.loads(raw)

        # Enrich with extraction metadata
        annotation["_meta"] = {
            "doc_format": doc_format,
            "filename": filename,
            "page_count": extracted.get("page_count"),
            "row_count": extracted.get("row_count"),
            "sheet_count": extracted.get("sheet_count"),
            "model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

        job.annotation = json.dumps(annotation)
        job.status = JobStatus.complete
        db.commit()
        logger.info(f"Job {job_id} complete — {response.usage.input_tokens} in / {response.usage.output_tokens} out tokens")

    except Exception as e:
        logger.exception(f"Job {job_id} failed: {e}")
        try:
            from app.models import AnnotationJob, JobStatus
            job = db.query(AnnotationJob).filter(AnnotationJob.id == job_id).first()
            if job:
                job.status = JobStatus.failed
                job.error = str(e)
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
