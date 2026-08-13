# FLNSMDFR — gcloud Shell Action Items

**Project:** `insulation-estimator` · **Region:** `us-east1` · **Account:** glmoss@guaranteedinsulation.com
**Service:** `hvac-estimator` · **AR repo:** `hvac` · **Service account:** `hvac-estimator-sa`

All commands are PowerShell-ready (backtick line continuations). Run them in order; each phase is safe to re-run.

> **Region note:** cloudbuild.yaml defaults `_REGION: us-central1`. Every `gcloud builds submit` below overrides it to `us-east1`. To make it permanent, edit the substitution in cloudbuild.yaml (Phase 7, item 7.0).

---

## Phase 0 — Sanity check (do first)

- [ ] **0.1 Confirm active project/account/region**
  ```powershell
  gcloud config list
  ```
  Expect: `project = insulation-estimator`, `region = us-east1`, your @guaranteedinsulation.com account. Already verified 2026-06-03 ✅

- [ ] **0.2 Refresh application-default credentials** (local runs of firestore_cache.py / gcs_storage.py use these)
  ```powershell
  gcloud auth application-default login
  ```

- [ ] **0.3 Get your project number** (needed for the Cloud Build service account in Phase 5)
  ```powershell
  gcloud projects describe insulation-estimator --format="value(projectNumber)"
  ```

## Phase 1 — Enable APIs (one-time)

- [ ] **1.1 Enable everything the stack needs in one shot**
  ```powershell
  gcloud services enable `
    run.googleapis.com `
    cloudbuild.googleapis.com `
    artifactregistry.googleapis.com `
    firestore.googleapis.com `
    secretmanager.googleapis.com `
    storage.googleapis.com `
    bigquery.googleapis.com `
    logging.googleapis.com
  ```

- [ ] **1.2 Verify**
  ```powershell
  gcloud services list --enabled --filter="name:(run OR firestore OR secretmanager OR artifactregistry OR cloudbuild)"
  ```

## Phase 2 — Service account + IAM

- [ ] **2.1 Create the runtime service account** (production deploy in cloudbuild.yaml expects this exact name)
  ```powershell
  gcloud iam service-accounts create hvac-estimator-sa `
    --display-name="HVAC Estimator Cloud Run runtime"
  ```

- [ ] **2.2 Grant the roles the app needs** (Firestore cache, GCS uploads, Secret Manager, BigQuery sink, logging)
  ```powershell
  $SA = "hvac-estimator-sa@insulation-estimator.iam.gserviceaccount.com"
  foreach ($role in @(
    "roles/datastore.user",
    "roles/storage.objectAdmin",
    "roles/secretmanager.secretAccessor",
    "roles/bigquery.dataEditor",
    "roles/logging.logWriter"
  )) {
    gcloud projects add-iam-policy-binding insulation-estimator `
      --member="serviceAccount:$SA" --role=$role --condition=None
  }
  ```

## Phase 3 — Secrets

- [ ] **3.1 Create both API-key secrets** (names must match cloudbuild.yaml's `--set-secrets` exactly)
  ```powershell
  # Paste the real keys when prompted; avoids keys landing in shell history
  $anthropic = Read-Host "Anthropic API key" -AsSecureString
  [System.Net.NetworkCredential]::new("", $anthropic).Password | gcloud secrets create anthropic-api-key --data-file=-

  $gemini = Read-Host "Gemini API key" -AsSecureString
  [System.Net.NetworkCredential]::new("", $gemini).Password | gcloud secrets create gemini-api-key --data-file=-
  ```

- [ ] **3.2 Grant the runtime SA access to both**
  ```powershell
  foreach ($s in @("anthropic-api-key","gemini-api-key")) {
    gcloud secrets add-iam-policy-binding $s `
      --member="serviceAccount:hvac-estimator-sa@insulation-estimator.iam.gserviceaccount.com" `
      --role="roles/secretmanager.secretAccessor"
  }
  ```

- [ ] **3.3 To rotate a key later** (Cloud Run picks up `:latest` on next revision)
  ```powershell
  "NEW_KEY_HERE" | gcloud secrets versions add anthropic-api-key --data-file=-
  ```

- [ ] **3.4 Dropbox secrets for the intake Job** (at least access token **or** refresh + app key)
  ```powershell
  "YOUR_TOKEN" | gcloud secrets create dropbox-access-token --data-file=-
  # Optional alternatives / webhook:
  # "REFRESH" | gcloud secrets create dropbox-refresh-token --data-file=-
  # "APP_KEY" | gcloud secrets create dropbox-app-key --data-file=-
  # "APP_SECRET" | gcloud secrets create dropbox-app-secret --data-file=-

  $SA = "hvac-estimator-sa@insulation-estimator.iam.gserviceaccount.com"
  foreach ($s in @("dropbox-access-token","dropbox-refresh-token","dropbox-app-key","dropbox-app-secret")) {
    gcloud secrets describe $s 2>$null
    if ($LASTEXITCODE -eq 0) {
      gcloud secrets add-iam-policy-binding $s `
        --member="serviceAccount:$SA" `
        --role="roles/secretmanager.secretAccessor"
    }
  }
  ```
  Full worker runbook: [`GCP_INTAKE_WORKER.md`](GCP_INTAKE_WORKER.md).

## Phase 4 — Storage, Firestore, BigQuery

- [ ] **4.1 Buckets** — cloudbuild.yaml expects `$PROJECT_ID-hvac-uploads` (prod) and `...-staging`
  ```powershell
  gcloud storage buckets create gs://insulation-estimator-hvac-uploads --location=us-east1 --uniform-bucket-level-access
  gcloud storage buckets create gs://insulation-estimator-hvac-uploads-staging --location=us-east1 --uniform-bucket-level-access
  ```

- [ ] **4.2 Auto-delete staging uploads after 30 days** (keeps storage costs near zero)
  ```powershell
  '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' | Out-File -Encoding ascii lifecycle.json
  gcloud storage buckets update gs://insulation-estimator-hvac-uploads-staging --lifecycle-file=lifecycle.json
  Remove-Item lifecycle.json
  ```

- [ ] **4.3 Firestore database** (native mode; one per project — skip if it exists)
  ```powershell
  gcloud firestore databases create --location=us-east1
  ```

- [ ] **4.4 BigQuery dataset for the estimate sink** (`data/bigquery_sink.py` takes full table IDs — create the dataset + tables it writes to)
  ```powershell
  bq mk --location=us-east1 --dataset insulation-estimator:hvac_estimates
  ```
  Tables (`specifications`, `measurements`, `quotes`) can be auto-created on first insert or defined with schemas later.

## Phase 5 — Cloud Build permissions (one-time)

- [ ] **5.1 Grant the Cloud Build SA deploy rights** (use project number from 0.3)
  ```powershell
  $PN = gcloud projects describe insulation-estimator --format="value(projectNumber)"
  $CB = "serviceAccount:$PN@cloudbuild.gserviceaccount.com"
  foreach ($role in @(
    "roles/run.admin",
    "roles/iam.serviceAccountUser",
    "roles/artifactregistry.writer",
    "roles/secretmanager.secretAccessor",
    "roles/storage.admin"
  )) {
    gcloud projects add-iam-policy-binding insulation-estimator --member=$CB --role=$role --condition=None
  }
  ```

## Phase 6 — Artifact Registry

- [ ] **6.1 Create the docker repo in us-east1**
  ```powershell
  gcloud artifacts repositories create hvac `
    --repository-format=docker `
    --location=us-east1 `
    --description="HVAC estimator container images"
  ```

- [ ] **6.2 Let local Docker push to it** (only needed for manual pushes outside Cloud Build)
  ```powershell
  gcloud auth configure-docker us-east1-docker.pkg.dev
  ```

## Phase 7 — Build & deploy

- [x] **7.0 cloudbuild.yaml fixed (2026-06-03)** — `_REGION` now defaults to us-east1; bash variables in the smoke-test step escaped (`$$SERVICE_URL`); `$COMMIT_SHA` replaced with a `_TAG` substitution since manual submits don't populate git variables.

- [ ] **7.1 Deploy to staging** (runs pytest → build → push → deploy → smoke test)
  ```powershell
  cd C:\Users\glmos\Dev\FLNSMDFR2.0\FLNSMDFR-main
  $tag = Get-Date -Format yyyyMMdd-HHmm
  gcloud builds submit --config cloudbuild.yaml --substitutions=_TAG=$tag
  ```

- [ ] **7.2 Verify staging**
  ```powershell
  $URL = gcloud run services describe hvac-estimator-staging --region us-east1 --format "value(status.url)"
  curl.exe -s -o NUL -w "%{http_code}" "$URL/_stcore/health"   # expect 200
  ```

- [ ] **7.3 Promote to production** (deploys with `--no-allow-unauthenticated` under hvac-estimator-sa)
  ```powershell
  $tag = Get-Date -Format yyyyMMdd-HHmm
  gcloud builds submit --config cloudbuild.yaml `
    --substitutions=_ENVIRONMENT=production,_TAG=$tag
  ```

- [ ] **7.4 Grant yourself (and the office) access to the private prod service**
  ```powershell
  gcloud run services add-iam-policy-binding hvac-estimator `
    --region us-east1 `
    --member="user:glmoss@guaranteedinsulation.com" `
    --role="roles/run.invoker"
  ```

## Phase 8 — Dropbox intake Job + Scheduler

See [`GCP_INTAKE_WORKER.md`](GCP_INTAKE_WORKER.md) for the full worker runbook.

- [ ] **8.1 Create Dropbox secrets** (Phase 3.4) and set the projects root path
- [ ] **8.2 Deploy UI + intake Job**
  ```powershell
  cd C:\Users\glmos\Dev\FLNSMDFR2.0
  .\deploy-gcloud.ps1 -Environment staging -DropboxProjectsRoot "/Estimating/Projects"
  ```
- [ ] **8.3 Manual Job smoke**
  ```powershell
  gcloud run jobs execute hvac-estimator-intake-staging --region us-east1 --wait
  .\scripts\smoke-intake.ps1 -CheckGcp
  ```
- [ ] **8.4 Confirm** `Quote_*.txt` + `Estimate_Summary.md` in the Dropbox project folder

## Phase 9 — Day-2 operations (keep handy)

```powershell
# Tail live logs
gcloud beta run services logs tail hvac-estimator --region us-east1

# Recent errors only
gcloud logging read 'resource.type="cloud_run_revision" severity>=ERROR' --limit 20 --format "table(timestamp,textPayload)"

# List revisions / traffic
gcloud run revisions list --service hvac-estimator --region us-east1

# Roll back to a previous revision
gcloud run services update-traffic hvac-estimator --region us-east1 --to-revisions REVISION_NAME=100

# Build history
gcloud builds list --limit 5

# What's this month costing? (links billing in console — no CLI equivalent)
start https://console.cloud.google.com/billing
```

---

**Order matters:** Phases 1–6 are one-time setup and must precede the first 7.1. Intake automation is Phase 8 (requires 3.4 Dropbox secrets). Day-to-day UI deploy is 7.1 → 7.2 → 7.3; intake is 8.2 → 8.3.
**Known deltas from repo docs:** GCP_MIGRATION_GUIDE.md uses `gsutil` and `us-central1`; this doc uses the current `gcloud storage` commands and `us-east1` per your config. IAP / VPC Service Controls from the guide's security section are omitted — add them when you put the prod URL in front of other users.
