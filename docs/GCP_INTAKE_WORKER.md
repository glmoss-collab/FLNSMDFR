# GCP Dropbox Intake Worker

Cloud Run **Job** + Cloud Scheduler poll that runs `dropbox_intake.py` against the
Dropbox API. The Streamlit UI service is unchanged.

## Architecture

```text
Cloud Scheduler (every 10 min)
        │
        ▼
Cloud Run Job  hvac-estimator-intake   (MODE=intake, timeout 3600s)
        │
        ├── Secret Manager: anthropic-api-key, dropbox-*
        ├── Firestore cache markers (CACHE_BACKEND=firestore)
        └── Dropbox API  →  Quote_*.txt + Estimate_Summary.md
```

## One-time secrets

Create at least one Dropbox auth path (access token **or** refresh + app key):

```powershell
# Access token (simplest for smoke)
"YOUR_DROPBOX_ACCESS_TOKEN" | gcloud secrets create dropbox-access-token --data-file=-

# Or refresh-token flow:
"YOUR_REFRESH" | gcloud secrets create dropbox-refresh-token --data-file=-
"YOUR_APP_KEY" | gcloud secrets create dropbox-app-key --data-file=-

# Optional (webhooks later)
"YOUR_APP_SECRET" | gcloud secrets create dropbox-app-secret --data-file=-

$SA = "hvac-estimator-sa@insulation-estimator.iam.gserviceaccount.com"
foreach ($s in @(
  "dropbox-access-token",
  "dropbox-refresh-token",
  "dropbox-app-key",
  "dropbox-app-secret"
)) {
  gcloud secrets describe $s 2>$null
  if ($LASTEXITCODE -eq 0) {
    gcloud secrets add-iam-policy-binding $s `
      --member="serviceAccount:$SA" `
      --role="roles/secretmanager.secretAccessor"
  }
}

gcloud services enable cloudscheduler.googleapis.com
```

`deploy-gcloud.ps1` also prompts for missing Dropbox secrets during preflight.

## Required Job env

| Variable | Value |
|----------|--------|
| `MODE` | `intake` |
| `DROPBOX_PROJECTS_ROOT` | Dropbox API path, e.g. `/Estimating/Projects` |
| `CACHE_BACKEND` | `firestore` |
| `PRICEBOOK_PATH` | `/app/pricebook_sample.json` (or mounted path) |
| `PRICE_MARKUP` | `1.15` |
| Do **not** set | `DROPBOX_LOCAL_ROOT` (forces API client) |

## Deploy

```powershell
cd C:\Users\glmos\Dev\FLNSMDFR2.0

# Preflight (APIs, secrets, SA) — no build
.\deploy-gcloud.ps1 -SetupOnly

# Staging UI + intake Job + Scheduler
.\deploy-gcloud.ps1 -Environment staging `
  -DropboxProjectsRoot "/Estimating/Projects"

# Production
.\deploy-gcloud.ps1 -Environment production `
  -DropboxProjectsRoot "/Estimating/Projects"
```

Cloud Build also deploys the Job when `_DROPBOX_PROJECTS_ROOT` is set:

```powershell
$tag = Get-Date -Format yyyyMMdd-HHmm
gcloud builds submit --config cloudbuild.yaml `
  --substitutions="_TAG=$tag,_ENVIRONMENT=staging,_DROPBOX_PROJECTS_ROOT=/Estimating/Projects"
```

## Smoke test

Local wiring (no GCP auth required):

```powershell
.\scripts\smoke-intake.ps1
```

After deploy + `gcloud auth login`:

```powershell
# Manual one-shot
gcloud run jobs execute hvac-estimator-intake-staging `
  --region us-east1 --wait

.\scripts\smoke-intake.ps1 -CheckGcp

# Logs
gcloud logging read `
  'resource.type="cloud_run_job" AND resource.labels.job_name="hvac-estimator-intake-staging"' `
  --limit 50 --format "table(timestamp,textPayload)"

# Confirm artifacts in Dropbox project folder:
#   Quote_<number>.txt
#   Estimate_Summary.md
```

Production job name: `hvac-estimator-intake` (no `-staging` suffix).

## Pass criteria

- Manual Job execute processes a known new Dropbox folder end-to-end
- Scheduler fires on interval; duplicate runs are no-ops (idempotent markers)
- Failures visible in Cloud Logging
- Streamlit UI (`hvac-estimator` / `-staging`) remains healthy

## Out of scope (this pass)

- Dropbox webhook HTTP server + public endpoint
- Mixing `skills/hvac_insulation_skill.py` — intake uses root `hvac_insulation_skill.py`
