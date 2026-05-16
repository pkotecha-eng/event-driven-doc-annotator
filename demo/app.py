import time
import streamlit as st
import requests
import json

API_BASE = "http://localhost:8000"

st.set_page_config(
    page_title="Spruce Doc Annotator",
    page_icon="📄",
    layout="wide"
)

st.title("📄 Spruce Doc Annotator")
st.caption("Event-driven document annotation service — upload a financial document and get structured AI annotations.")

# --- Upload section ---
st.header("Upload Document")
uploaded_file = st.file_uploader(
    "Choose a PDF or spreadsheet",
    type=["pdf", "csv", "xlsx", "xls"],
    help="Supported: PDF, CSV, XLSX, XLS — max 10MB"
)

if uploaded_file:
    st.info(f"**{uploaded_file.name}** ({uploaded_file.size:,} bytes)")

    if st.button("🚀 Submit for Annotation", type="primary"):
        with st.spinner("Submitting..."):
            try:
                resp = requests.post(
                    f"{API_BASE}/upload",
                    files={"file": (uploaded_file.name, uploaded_file.getvalue(), uploaded_file.type or "application/octet-stream")}
                )
                resp.raise_for_status()
                data = resp.json()
                st.session_state["job_id"] = data["job_id"]
                st.session_state["filename"] = data["filename"]
                st.success(f"✅ Job submitted! **Job ID:** `{data['job_id']}`")
                if "Duplicate" in data.get("message", ""):
                    st.warning("ℹ️ Duplicate file detected — returning existing job results.")
            except requests.exceptions.ConnectionError:
                st.error("❌ Cannot connect to API. Make sure the server is running: `uvicorn app.main:app --reload`")
            except Exception as e:
                st.error(f"❌ Upload failed: {e}")

st.divider()

# --- Poll section ---
st.header("Retrieve Annotation Results")

job_id_input = st.text_input(
    "Job ID",
    value=st.session_state.get("job_id", ""),
    placeholder="Paste a job ID or submit a document above"
)

col1, col2 = st.columns([1, 3])
with col1:
    check = st.button("🔍 Check Status")
with col2:
    auto_poll = st.checkbox("Auto-poll until complete", value=True)

if check and job_id_input:
    max_attempts = 30 if auto_poll else 1
    for attempt in range(max_attempts):
        try:
            resp = requests.get(f"{API_BASE}/jobs/{job_id_input}")
            if resp.status_code == 404:
                st.error(f"Job `{job_id_input}` not found.")
                break
            resp.raise_for_status()
            data = resp.json()
            status = data["status"]

            status_colors = {
                "pending": "🟡",
                "processing": "🔵",
                "complete": "🟢",
                "failed": "🔴"
            }
            st.markdown(f"**Status:** {status_colors.get(status, '⚪')} `{status}`")

            if status == "complete" and data.get("annotation"):
                ann = data["annotation"]
                meta = ann.pop("_meta", {})

                col_a, col_b = st.columns(2)
                with col_a:
                    st.metric("Document Type", ann.get("document_type", "—"))
                    st.metric("Sentiment", ann.get("sentiment", "—"))
                    st.metric("Time Period", ann.get("time_period", "—"))
                with col_b:
                    st.metric("Confidence", ann.get("confidence", "—"))
                    st.metric("Model", meta.get("model", "—"))
                    token_str = f"{meta.get('input_tokens', 0):,} in / {meta.get('output_tokens', 0):,} out"
                    st.metric("Tokens", token_str)

                st.subheader("Summary")
                st.write(ann.get("summary", "—"))

                col_e, col_f = st.columns(2)
                with col_e:
                    st.subheader("Key Entities")
                    entities = ann.get("key_entities", {})
                    for k, v in entities.items():
                        if v:
                            st.markdown(f"**{k.title()}:** {', '.join(v) if isinstance(v, list) else v}")

                with col_f:
                    st.subheader("Financial Metrics")
                    metrics = ann.get("financial_metrics", {})
                    for k, v in metrics.items():
                        if v:
                            st.markdown(f"**{k.replace('_', ' ').title()}:** {v}")

                if ann.get("risk_flags"):
                    st.subheader("⚠️ Risk Flags")
                    for flag in ann["risk_flags"]:
                        st.warning(flag)

                if ann.get("follow_up_questions"):
                    st.subheader("🔎 Follow-up Questions")
                    for q in ann["follow_up_questions"]:
                        st.markdown(f"- {q}")

                st.subheader("Raw JSON")
                st.json(ann)
                break

            elif status == "failed":
                st.error(f"Job failed: {data.get('error', 'Unknown error')}")
                break
            elif auto_poll and status in ("pending", "processing"):
                st.info(f"Status: {status} — checking again in 2 seconds... (attempt {attempt+1}/{max_attempts})")
                time.sleep(2)
                continue
            else:
                break

        except requests.exceptions.ConnectionError:
            st.error("❌ Cannot connect to API.")
            break
        except Exception as e:
            st.error(f"Error: {e}")
            break

st.divider()

# --- Recent jobs ---
st.header("Recent Jobs")
if st.button("🔄 Refresh"):
    try:
        resp = requests.get(f"{API_BASE}/jobs?limit=10")
        resp.raise_for_status()
        jobs = resp.json()
        if jobs:
            st.dataframe(
                jobs,
                use_container_width=True,
                column_config={
                    "job_id": st.column_config.TextColumn("Job ID", width="medium"),
                    "filename": st.column_config.TextColumn("Filename"),
                    "status": st.column_config.TextColumn("Status"),
                    "created_at": st.column_config.TextColumn("Created At"),
                }
            )
        else:
            st.info("No jobs yet.")
    except requests.exceptions.ConnectionError:
        st.error("❌ Cannot connect to API.")
