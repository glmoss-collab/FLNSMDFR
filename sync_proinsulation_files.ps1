$src = 'C:\Users\User\HVAC DATA\PROINSULATIONESTIMATION-main'
$dst = 'C:\Users\User\OneDrive\Documents\GitHub\FLNSMDFR'
$Overwrite = $false
$files = @{
  'gcp'     = @('cloud_config.py','gcs_storage.py','firestore_cache.py','secrets_manager.py','vertex_ai_client.py')
  'agents'  = @('claude_agent_tools.py','claude_estimation_agent.py','claude_workflow_enhancement.py','hvac_insulation_skill.py')
  'domain'   = @('hvac_insulation_estimator.py','guaranteed_insulation_scope.py','guaranteed_insulation_bid_package.py','pydantic_models.py','errors.py')
  'utils'   = @('utils_cache.py','utils_async.py','utils_tracking.py','utils_pdf.py')
  'apps'    = @('guaranteed_insulation_app.py')
  'config'  = @('requirements.txt','cloudbuild.yaml','Dockerfile','.gitignore')
  'data'    = @('pricebook_sample.json','measurements_template.csv')
  'docs'    = @('CLAUDE.md','CLAUDE_AGENTS_ARCHITECTURE.md','GCP_MIGRATION_GUIDE.md','HVAC_SKILL_README.md','Estimator_Agent_Workflow.md','AGENT_SETUP_GUIDE.md','GUARANTEED_INSULATION_README.md')
}

$added = @(); $skipped = @(); $overwritten = @(); $missing = @()

foreach ($group in $files.Keys) {
  Write-Host "`n[$group]" -ForegroundColor Cyan
  foreach ($f in $files[$group]) {
    $srcPath = Join-Path $src $f
    $dstPath = Join-Path $dst $f
    if (-not (Test-Path $srcPath)) { $missing += $f; Write-Host "  MISSING $f" -ForegroundColor Yellow; continue }
    if ((Test-Path $dstPath) -and -not $Overwrite) {
      $skipped += $f; Write-Host "  skip   $f (exists)" -ForegroundColor DarkGray
    } else {
      if (Test-Path $dstPath) { $overwritten += $f } else { $added += $f }
      Copy-Item $srcPath -Destination $dstPath -Force
      $tag = if ($overwritten -contains $f) { 'WROTE ' } else { 'added ' }
      Write-Host "  $tag $f" -ForegroundColor Green
    }
  }
}

$testSrc = Join-Path $src 'tests'
$testDst = Join-Path $dst 'tests'
if (Test-Path $testSrc) {
  if ((Test-Path $testDst) -and -not $Overwrite) {
    Write-Host "`n[tests/] skip (exists)" -ForegroundColor DarkGray
  } else {
    Copy-Item $testSrc -Destination $dst -Recurse -Force
    Write-Host "`n[tests/] copied" -ForegroundColor Green
  }
}

$docsDir = Join-Path $dst 'docs'
New-Item -ItemType Directory -Path $docsDir -Force | Out-Null
foreach ($d in $files['docs']) {
  $p = Join-Path $dst $d
  if (Test-Path $p) { Move-Item $p -Destination $docsDir -Force -ErrorAction SilentlyContinue }
}

Write-Host "`n=== Summary ===" -ForegroundColor Cyan
Write-Host "Added:       $($added.Count)" -ForegroundColor Green
Write-Host "Skipped:     $($skipped.Count) (existing files preserved)" -ForegroundColor DarkGray
Write-Host "Overwritten: $($overwritten.Count)" -ForegroundColor Yellow
Write-Host "Missing:     $($missing.Count)" -ForegroundColor Yellow
if ($skipped.Count -gt 0) { Write-Host "`nTo overwrite skipped files, re-run with `$Overwrite = `$true" -ForegroundColor DarkGray }
