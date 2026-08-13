# Weekend estimation intake launcher.
# Called by the "Guaranteed Weekend Estimate Intake" scheduled task
# (Saturday + Sunday). Safe to run by hand any time:
#   powershell -NoProfile -ExecutionPolicy Bypass -File run_weekend_intake.ps1

$ErrorActionPreference = "Stop"
$repo = "C:\Users\glmos\Dev\FLNSMDFR2.0"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$runner = Join-Path $repo "weekend_intake_runner.py"
$logDir = Join-Path $repo "logs"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("weekend_intake_{0:yyyy-MM-dd_HHmm}.log" -f (Get-Date))

# Load ANTHROPIC_API_KEY (and any other settings) from .env if present and
# not already set in the environment. Lines look like KEY=value.
$envFile = Join-Path $repo ".env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$') {
            $name = $Matches[1]
            $value = $Matches[2].Trim('"').Trim("'")
            if (-not [Environment]::GetEnvironmentVariable($name)) {
                [Environment]::SetEnvironmentVariable($name, $value)
            }
        }
    }
}

if (-not $env:ANTHROPIC_API_KEY) {
    "ERROR: ANTHROPIC_API_KEY is not set (env var or $repo\.env). Aborting." |
        Tee-Object -FilePath $log
    exit 3
}

"=== Weekend intake started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8
& $python $runner *>> $log
$code = $LASTEXITCODE
"=== Finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') exit=$code ===" | Out-File $log -Append -Encoding utf8

# Keep the last 30 logs.
Get-ChildItem $logDir -Filter "weekend_intake_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
