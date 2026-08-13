<#
================================================================================
  deploy-gcloud.ps1
  One-command Cloud Run deploy for FLNSMDFR2.0 (hvac-estimator)
  Companion to: gcloud-terminal-guide.txt, docs/GCP_INTAKE_WORKER.md

  Run from the repo root (C:\Users\glmos\Dev\FLNSMDFR2.0)
  in PowerShell / Windows Terminal with the Google Cloud SDK installed.

  WHAT IT DOES (idempotent - safe to re-run):
    1. Verifies gcloud auth + sets project
    2. Enables required APIs (incl. Cloud Scheduler when deploying intake)
    3. Ensures Artifact Registry repo exists
    4. Verifies Anthropic/Gemini (+ optional Dropbox) secrets
    5. Grants secretAccessor to the runtime service account
    6. Grants Cloud Build's SA the roles it needs to deploy
    7. Submits the build with a unique _TAG
    8. When -DropboxProjectsRoot is set: creates/updates intake Job + Scheduler
    9. Prints the service URL (and Job name) on success

  USAGE:
    .\deploy-gcloud.ps1                       # staging UI only
    .\deploy-gcloud.ps1 -Environment production
    .\deploy-gcloud.ps1 -SetupOnly            # preflight, no build
    .\deploy-gcloud.ps1 -DropboxProjectsRoot "/Estimating/Projects"
    .\deploy-gcloud.ps1 -Environment production -DropboxProjectsRoot "/Estimating/Projects"
================================================================================
#>
#Requires -Version 5.1
param(
    [ValidateSet('staging', 'production')]
    [string]$Environment = 'staging',
    [string]$Project = 'insulation-estimator',
    [string]$Region  = 'us-east1',
    [string]$ArRepo  = 'hvac',
    [string]$ServiceName = 'hvac-estimator',
    [string]$RuntimeSA = 'hvac-estimator-sa',
    # Dropbox API path for the intake Job (e.g. /Estimating/Projects).
    # When set, also deploys Cloud Run Job + Scheduler after the UI build.
    [string]$DropboxProjectsRoot = '',
    [string]$PriceMarkup = '1.15',
    [string]$PricebookPath = '/app/pricebook_sample.json',
    [string]$SchedulerCron = '*/10 * * * *',
    # Preflight only - validate/fix project setup without submitting a build
    [switch]$SetupOnly
)

# 'Continue', not 'Stop': gcloud prints progress to stderr, and in PS 5.1
# stderr + 2>&1 under EAP=Stop throws NativeCommandError. We check
# $LASTEXITCODE explicitly instead.
$ErrorActionPreference = 'Continue'

function Write-Step { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "    [OK]   $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    [WARN] $m" -ForegroundColor Yellow }
function Write-Err  { param($m) Write-Host "    [FAIL] $m" -ForegroundColor Red }

function Invoke-Gcloud {
    # Runs gcloud, returns stdout, throws on non-zero exit.
    param([string[]]$GcloudArgs)
    $out = & gcloud @GcloudArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($GcloudArgs -join ' ')`n$out" }
    return $out
}

function Test-SecretExists {
    param([string]$Name)
    & gcloud secrets describe $Name 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Ensure-Secret {
    param(
        [string]$Name,
        [string]$Prompt,
        [switch]$Optional
    )
    if (Test-SecretExists $Name) {
        Write-Ok "$Name exists"
        return $true
    }
    Write-Warn "Secret '$Name' missing."
    if ($Optional -and -not $DropboxProjectsRoot) {
        Write-Warn "Skipping optional $Name (no -DropboxProjectsRoot)."
        return $false
    }
    $key = Read-Host $Prompt -AsSecureString
    $plain = [System.Net.NetworkCredential]::new('', $key).Password
    if (-not $plain) {
        if ($Optional) {
            Write-Warn "Empty value - skipped $Name"
            return $false
        }
        Write-Err "Empty key - skipping. Deploy WILL fail without it."
        return $false
    }
    $plain | & gcloud secrets create $Name --data-file=-
    if ($LASTEXITCODE -ne 0) { throw "Failed to create secret $Name" }
    $plain = $null
    Write-Ok "Created $Name"
    return $true
}

function Grant-SecretAccess {
    param([string]$Name, [string]$SaEmail)
    if (-not (Test-SecretExists $Name)) { return }
    Invoke-Gcloud @('secrets', 'add-iam-policy-binding', $Name,
        "--member=serviceAccount:$SaEmail",
        '--role=roles/secretmanager.secretAccessor') | Out-Null
    Write-Ok "$Name readable by $RuntimeSA"
}

# ---- 0. Sanity -----------------------------------------------------------------
Write-Step "Preflight: gcloud + repo root"
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Err "gcloud not found. Open a NEW terminal, or re-run setup-dev-env.ps1."
    exit 1
}
if (-not (Test-Path '.\cloudbuild.yaml')) {
    Write-Err "cloudbuild.yaml not found. cd to the repo root first:"
    Write-Host '    cd "C:\Users\glmos\Dev\FLNSMDFR2.0"'
    exit 1
}
$acct = (& gcloud config get-value account 2>$null)
if (-not $acct -or $acct -eq '(unset)') {
    Write-Err "Not logged in. Run: gcloud auth login"
    exit 1
}
Write-Ok "Authenticated as $acct"

Invoke-Gcloud @('config', 'set', 'project', $Project) | Out-Null
$projectNumber = (Invoke-Gcloud @('projects', 'describe', $Project, '--format=value(projectNumber)')).Trim()
$runtimeSaEmail = "$RuntimeSA@$Project.iam.gserviceaccount.com"
Write-Ok "Project: $Project ($projectNumber)"

$deployIntake = -not [string]::IsNullOrWhiteSpace($DropboxProjectsRoot)

# ---- 1. APIs --------------------------------------------------------------------
Write-Step "Enabling required APIs (no-op if already enabled)"
$apis = @(
    'run.googleapis.com',
    'cloudbuild.googleapis.com',
    'artifactregistry.googleapis.com',
    'secretmanager.googleapis.com',
    'firestore.googleapis.com',
    'logging.googleapis.com'
)
if ($deployIntake) {
    $apis += 'cloudscheduler.googleapis.com'
}
Invoke-Gcloud (@('services', 'enable') + $apis) | Out-Null
Write-Ok "APIs enabled"

# ---- 2. Artifact Registry repo ----------------------------------------------------
Write-Step "Checking Artifact Registry repo '$ArRepo' in $Region"
& gcloud artifacts repositories describe $ArRepo --location=$Region 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warn "Repo not found - creating it"
    Invoke-Gcloud @('artifacts', 'repositories', 'create', $ArRepo,
        '--repository-format=docker', "--location=$Region",
        '--description=HVAC estimator container images') | Out-Null
    Write-Ok "Created $Region-docker.pkg.dev/$Project/$ArRepo"
} else {
    Write-Ok "Repo exists"
}

# ---- 3. Runtime service account ----------------------------------------------------
Write-Step "Checking runtime service account $runtimeSaEmail"
& gcloud iam service-accounts describe $runtimeSaEmail 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warn "Not found - creating it"
    Invoke-Gcloud @('iam', 'service-accounts', 'create', $RuntimeSA,
        '--display-name=HVAC Estimator Cloud Run runtime') | Out-Null
}
Write-Ok "Runtime SA ready"

# ---- 4. Secrets ----------------------------------------------------------------------
Write-Step "Checking secrets (anthropic, gemini, dropbox)"
Ensure-Secret -Name 'anthropic-api-key' -Prompt 'Paste Anthropic API key (input hidden)' | Out-Null
Ensure-Secret -Name 'gemini-api-key' -Prompt 'Paste Gemini API key (input hidden)' | Out-Null

# Dropbox: access token OR refresh + app key (prompt when deploying intake)
$hasAccess = Ensure-Secret -Name 'dropbox-access-token' `
    -Prompt 'Paste Dropbox access token (or Enter to skip if using refresh token)' `
    -Optional
if (-not $hasAccess -and $deployIntake) {
    Ensure-Secret -Name 'dropbox-refresh-token' `
        -Prompt 'Paste Dropbox refresh token' `
        -Optional | Out-Null
    Ensure-Secret -Name 'dropbox-app-key' `
        -Prompt 'Paste Dropbox app key' `
        -Optional | Out-Null
}
Ensure-Secret -Name 'dropbox-app-secret' `
    -Prompt 'Paste Dropbox app secret (optional, for webhooks)' `
    -Optional | Out-Null

foreach ($secret in @(
    'anthropic-api-key', 'gemini-api-key',
    'dropbox-access-token', 'dropbox-refresh-token',
    'dropbox-app-key', 'dropbox-app-secret'
)) {
    Grant-SecretAccess -Name $secret -SaEmail $runtimeSaEmail
}

if ($deployIntake) {
    $hasDbx = (Test-SecretExists 'dropbox-access-token') -or (
        (Test-SecretExists 'dropbox-refresh-token') -and (Test-SecretExists 'dropbox-app-key')
    )
    if (-not $hasDbx) {
        Write-Err "Intake deploy needs dropbox-access-token OR (dropbox-refresh-token + dropbox-app-key)."
        exit 1
    }
}

# ---- 5. Cloud Build service account roles ----------------------------------------------
# Manual `gcloud builds submit` runs as the default compute SA on newer projects.
# It needs to push images, deploy Cloud Run, and act as the runtime SA.
Write-Step "Granting Cloud Build deploy roles (idempotent)"
$buildSA = "$projectNumber-compute@developer.gserviceaccount.com"
foreach ($role in @('roles/run.admin', 'roles/artifactregistry.writer', 'roles/logging.logWriter', 'roles/storage.objectViewer', 'roles/cloudscheduler.admin')) {
    Invoke-Gcloud @('projects', 'add-iam-policy-binding', $Project,
        "--member=serviceAccount:$buildSA", "--role=$role", '--condition=None') | Out-Null
}
# Allow the build SA to deploy services that run AS the runtime SA
Invoke-Gcloud @('iam', 'service-accounts', 'add-iam-policy-binding', $runtimeSaEmail,
    "--member=serviceAccount:$buildSA", '--role=roles/iam.serviceAccountUser') | Out-Null
# Smoke test calls staging with the build SA's metadata identity token (org policy
# blocks allUsers). run.admin alone does not include roles/run.invoker.
$stagingSvc = "$ServiceName-staging"
Invoke-Gcloud @('run', 'services', 'add-iam-policy-binding', $stagingSvc,
    "--region=$Region", "--member=serviceAccount:$buildSA", '--role=roles/run.invoker') | Out-Null
Write-Ok "Build SA ($buildSA) can push, deploy, act as $RuntimeSA, and invoke $stagingSvc"

if ($SetupOnly) {
    Write-Host "`n  Setup verified. Re-run without -SetupOnly to deploy." -ForegroundColor Green
    if ($deployIntake) {
        Write-Host "  Intake Job will use DROPBOX_PROJECTS_ROOT=$DropboxProjectsRoot" -ForegroundColor White
    }
    exit 0
}

# ---- 6. Build + deploy UI ---------------------------------------------------------------
Write-Step "Submitting build (environment: $Environment)"
$tag = Get-Date -Format 'yyyyMMdd-HHmm'
$sha = (& git rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -eq 0 -and $sha) { $tag = "$tag-$sha" }
Write-Host "    _TAG=$tag" -ForegroundColor White

$subs = "_TAG=$tag,_ENVIRONMENT=$Environment"
if ($deployIntake) {
    $subs += ",_DROPBOX_PROJECTS_ROOT=$DropboxProjectsRoot,_PRICE_MARKUP=$PriceMarkup,_PRICEBOOK_PATH=$PricebookPath,_SCHEDULER_CRON=$SchedulerCron"
}

& gcloud builds submit --config cloudbuild.yaml --substitutions=$subs
if ($LASTEXITCODE -ne 0) {
    Write-Err "Build failed. Get the full log with:"
    Write-Host '    gcloud builds list --limit=1'
    Write-Host '    gcloud builds log <BUILD_ID>'
    exit 1
}

# ---- 7. Result ------------------------------------------------------------------------
Write-Step "Deployed. Service URLs:"
$svc = if ($Environment -eq 'production') { $ServiceName } else { "$ServiceName-staging" }
$url = (Invoke-Gcloud @('run', 'services', 'describe', $svc, "--region=$Region", '--format=value(status.url)')).Trim()
Write-Ok "$svc -> $url"

if ($deployIntake) {
    $jobName = if ($Environment -eq 'production') { "$ServiceName-intake" } else { "$ServiceName-intake-staging" }
    Write-Ok "Intake Job: $jobName"
    Write-Host "`n  Smoke the intake Job with:" -ForegroundColor White
    Write-Host "    gcloud run jobs execute $jobName --region $Region --wait" -ForegroundColor White
}

Write-Host "`n  Tail UI logs with:" -ForegroundColor White
Write-Host "    gcloud beta run services logs tail $svc --region $Region" -ForegroundColor White
