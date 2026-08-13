<#
.SYNOPSIS
  Smoke checks for local auto-intake + GCP intake Job wiring.

.DESCRIPTION
  1. Runs dropbox_intake unit tests
  2. Dry-runs --list against a temp local fixture (no Anthropic call)
  3. Validates Dockerfile / entrypoint MODE wiring
  4. With -CheckGcp: describes the Cloud Run Job and Scheduler (must already be deployed)

.EXAMPLE
  .\scripts\smoke-intake.ps1
  .\scripts\smoke-intake.ps1 -CheckGcp -Environment staging
#>
#Requires -Version 5.1
param(
    [ValidateSet('staging', 'production')]
    [string]$Environment = 'staging',
    [string]$Project = 'insulation-estimator',
    [string]$Region = 'us-east1',
    [string]$ServiceName = 'hvac-estimator',
    [switch]$CheckGcp
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Write-Step { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "    [OK]   $m" -ForegroundColor Green }
function Write-Fail { param($m) Write-Host "    [FAIL] $m" -ForegroundColor Red; $script:failed = $true }
$script:failed = $false

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

# ---- 1. Unit tests ---------------------------------------------------------------
Write-Step "pytest tests/test_dropbox_intake.py"
& $py -m pytest tests/test_dropbox_intake.py -q --tb=line
if ($LASTEXITCODE -ne 0) { Write-Fail "pytest failed" } else { Write-Ok "13 intake tests passed (or suite green)" }

# ---- 2. Local --list dry run -----------------------------------------------------
Write-Step "Local --list against temp fixture"
$tmp = Join-Path $env:TEMP ("intake-smoke-" + [guid]::NewGuid().ToString('N'))
$proj = Join-Path $tmp '_test_project_alpha'
New-Item -ItemType Directory -Path $proj -Force | Out-Null
# Minimal PDF header so list_pdf_files finds a file
[IO.File]::WriteAllBytes((Join-Path $proj 'specs.pdf'), [Text.Encoding]::ASCII.GetBytes("%PDF-1.4`n%%EOF`n"))

$env:DROPBOX_LOCAL_ROOT = $tmp
$env:CACHE_BACKEND = 'file'
& $py dropbox_intake.py --root $tmp --list
if ($LASTEXITCODE -ne 0) {
    Write-Fail "dropbox_intake --list failed"
} else {
    Write-Ok "--list succeeded against $tmp"
}
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
Remove-Item Env:DROPBOX_LOCAL_ROOT -ErrorAction SilentlyContinue

# ---- 3. Entrypoint / Dockerfile wiring -------------------------------------------
Write-Step "Validate MODE=intake entrypoint wiring"
$ep = Get-Content (Join-Path $root 'docker-entrypoint.sh') -Raw
$df = Get-Content (Join-Path $root 'Dockerfile') -Raw
if ($ep -notmatch 'MODE=intake|intake\)') { Write-Fail "docker-entrypoint.sh missing intake mode" } else { Write-Ok "entrypoint has intake mode" }
if ($df -notmatch 'ENTRYPOINT.*docker-entrypoint') { Write-Fail "Dockerfile missing ENTRYPOINT" } else { Write-Ok "Dockerfile ENTRYPOINT set" }
if ($df -notmatch 'MODE=ui') { Write-Fail "Dockerfile missing default MODE=ui" } else { Write-Ok "Dockerfile default MODE=ui" }

$cb = Get-Content (Join-Path $root 'cloudbuild.yaml') -Raw
if ($cb -notmatch 'deploy-intake-job') { Write-Fail "cloudbuild.yaml missing deploy-intake-job step" } else { Write-Ok "cloudbuild has intake Job step" }
if ($cb -notmatch 'task-timeout 3600') { Write-Fail "cloudbuild Job timeout not 3600s" } else { Write-Ok "Job timeout 3600s" }

# ---- 4. Optional GCP describe ----------------------------------------------------
if ($CheckGcp) {
    Write-Step "GCP Job / Scheduler describe (environment=$Environment)"
    if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
        Write-Fail "gcloud not found"
    } else {
        & gcloud config set project $Project 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "gcloud auth expired or project unavailable. Run: gcloud auth login"
            Write-Host "    Then deploy: .\deploy-gcloud.ps1 -DropboxProjectsRoot '/Estimating/Projects'" -ForegroundColor White
            Write-Host "    Then smoke:  gcloud run jobs execute hvac-estimator-intake-staging --region $Region --wait" -ForegroundColor White
        } else {
        $job = if ($Environment -eq 'production') { "$ServiceName-intake" } else { "$ServiceName-intake-staging" }
        $sched = if ($Environment -eq 'production') { "$ServiceName-intake-poll" } else { "$ServiceName-intake-poll-staging" }

        & gcloud run jobs describe $job --region=$Region 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "Job $job not found. Deploy with: .\deploy-gcloud.ps1 -DropboxProjectsRoot '/your/path'"
        } else {
            Write-Ok "Job $job exists"
            Write-Host "    Execute: gcloud run jobs execute $job --region $Region --wait" -ForegroundColor White
        }

        & gcloud scheduler jobs describe $sched --location=$Region 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "Scheduler $sched not found"
        } else {
            Write-Ok "Scheduler $sched exists"
        }
        }
    }
} else {
    Write-Step "Skipping GCP checks (pass -CheckGcp after deploy)"
    Write-Host "    After deploy: .\scripts\smoke-intake.ps1 -CheckGcp" -ForegroundColor White
    Write-Host "    Then: gcloud run jobs execute hvac-estimator-intake-staging --region us-east1 --wait" -ForegroundColor White
}

# ---- Result ----------------------------------------------------------------------
Write-Host ""
if ($script:failed) {
    Write-Fail "Smoke checks reported failures"
    exit 1
}
Write-Ok "All local smoke checks passed"
exit 0
