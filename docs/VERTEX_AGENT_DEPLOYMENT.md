# Vertex Agent Deployment Runbook

## Architecture
- Cloud Run serves `agent_api.py` (FastAPI + Vertex agent orchestrator).
- Vertex tool calls invoke deterministic functions in `vertex_agent_tools.py`.
- Files use `gcs_storage.py`, sessions use Firestore, secrets use Secret Manager.
- Telemetry is sent to BigQuery table `agent_telemetry.events` when available.

## Staged Promotion Workflow
1. Deploy staging revision with `cloudbuild.yaml` using `_ENVIRONMENT=staging`.
2. Run offline evals in CI:
   - `python scripts/run_offline_eval.py`
3. Execute smoke tests:
   - `/health`
   - `/agent/chat`
   - `/agent/jobs/estimate`
4. Promote to production with `_ENVIRONMENT=production`.
5. Monitor Cloud Run errors and BigQuery telemetry.

## Cloud Run Runtime
- Entrypoint: `uvicorn agent_api:app --host 0.0.0.0 --port ${PORT}`
- Required env vars:
  - `GCP_PROJECT`
  - `GCP_REGION` (optional, defaults `us-central1`)
  - `STORAGE_BACKEND` (`gcs` in cloud)
  - `GCS_BUCKET`
  - `CACHE_BACKEND` (`firestore` in cloud)
  - `USE_VERTEX_AI=true`

## Suggested BigQuery Table
```sql
CREATE TABLE IF NOT EXISTS `PROJECT_ID.agent_telemetry.events` (
  event_type STRING,
  payload_json STRING,
  created_at TIMESTAMP
);
```
