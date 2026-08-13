# Local Auto-Intake Test Runbook

Verify Dropbox estimation intake on a synced local folder before relying on the GCP worker.

## Prerequisites

- Repo venv with `requirements.txt` installed
- Poppler on `PATH` (for `pdf2image`)
- `ANTHROPIC_API_KEY` set
- Dropbox desktop sync of a projects root (one subfolder per project, PDFs inside)

Optional:

- `PRICEBOOK_PATH=pricebook_sample.json`
- `PRICE_MARKUP=1.15`

## Fixture layout

```text
<DROPBOX_LOCAL_ROOT>/
  _test_project_alpha/
    specs.pdf          # searchable text; Section 23 07 preferred
    drawings.pdf       # scale visible if possible
```

## Environment (PowerShell)

```powershell
cd C:\Users\glmos\Dev\FLNSMDFR2.0
$env:DROPBOX_LOCAL_ROOT = "C:\Users\glmos\...\Projects"   # synced root
$env:ANTHROPIC_API_KEY = "sk-..."                          # required for agent loop
$env:CACHE_BACKEND = "file"
```

## Dry run → single project → full scan

```powershell
# 1. List only — no Claude calls
python dropbox_intake.py --root $env:DROPBOX_LOCAL_ROOT --list

# 2. One project (forces reprocess if needed)
python dropbox_intake.py --project "$env:DROPBOX_LOCAL_ROOT\_test_project_alpha" `
  --force --markup 1.15 --pricebook pricebook_sample.json

# 3. Scan all new/changed folders
python dropbox_intake.py --root $env:DROPBOX_LOCAL_ROOT `
  --markup 1.15 --pricebook pricebook_sample.json

# 4. Idempotency — unchanged folders should be skipped
python dropbox_intake.py --root $env:DROPBOX_LOCAL_ROOT --list
```

## Pass criteria

- Exit code `0` and JSON `success: true` for processed projects
- Artifacts in the project folder: `Quote_<number>.txt`, `Estimate_Summary.md`
- Re-run without `--force` skips unchanged folders
- Unit tests: `python -m pytest tests/test_dropbox_intake.py -q`

## Optional local schedule (interim)

Windows Task Scheduler every N minutes until the GCP worker is live:

```powershell
cd C:\Users\glmos\Dev\FLNSMDFR2.0
.\.venv\Scripts\python.exe dropbox_intake.py --root $env:DROPBOX_LOCAL_ROOT --markup 1.15
```

## Related

- GCP worker: [GCP_INTAKE_WORKER.md](GCP_INTAKE_WORKER.md)
- Skill overview: [HVAC_SKILL_README.md](HVAC_SKILL_README.md)
