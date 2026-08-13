#!/usr/bin/env python3
"""
Weekend Estimation Intake Runner
================================

Scheduled entry point for the automated Dropbox scan. Designed to run on
weekends (Windows Task Scheduler, Saturday + Sunday) so bid lists that land
Thursday/Friday are estimated before Monday without burning weekday API usage.

Folder conventions follow the Stiles SOP
(``Stiles/automation/STILES_INTAKE_SOP.md``):

* Job folders are named ``MM.DD.YY - JOB NAME - CITY, ST`` (date = bid due
  date) and may live under any month folder (``Stiles/August 2026/`` or
  ``Stiles/BIDS 2026/August 2026/``) -- discovery walks the tree and matches
  the date-prefix pattern wherever it appears.
* PDFs sit in ``Drawings/`` / ``Specs/`` / ``Addendum/`` sub-folders; the
  storage layer lists them recursively.
* ``26sent`` (already quoted), ``_Past Due``, and ``automation`` are skipped.
* Only jobs whose due date is recent/upcoming are processed, so the BIDS
  2025/2026 archives (thousands of PDFs) are never fed to the API.

For every new/changed job folder it:
  1. Runs the estimation pipeline (dropbox_intake applies the Guaranteed
     Insulation Inc. scope filter -- external HVAC/mechanical only).
  2. Writes Quote, Bid Package, and Estimate_Summary.md into a
     "Draft Estimate" sub-folder inside that job folder.
  3. Drops a "_NEW ESTIMATES <date>.md" report at the Dropbox root listing
     everything processed this run, so Monday morning starts with one file.

Idempotent: unchanged job folders are skipped (content-hash markers), so
running Saturday AND Sunday only re-processes folders whose PDFs changed
(e.g. an addendum landed).

CLI
---
    python weekend_intake_runner.py                # scan default root
    python weekend_intake_runner.py --list         # dry run: show what would process
    python weekend_intake_runner.py --root <PATH>  # override root
    python weekend_intake_runner.py --force        # reprocess even unchanged folders
    python weekend_intake_runner.py --grace-days 14 --max-projects 30
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from dropbox_intake import OUTPUT_SUBFOLDER, DropboxEstimationIntake

logger = logging.getLogger(__name__)

DEFAULT_ROOT = (
    os.getenv("DROPBOX_LOCAL_ROOT")
    or r"C:\Users\glmos\Guaranteed Group Dropbox\Guaranteed Group Team Folder\Stiles"
)
REPORT_FILENAME_TEMPLATE = "_NEW ESTIMATES {date}.md"

# Job folders per the Stiles SOP: "MM.DD.YY - JOB NAME - CITY, ST".
# Tolerates the known typo variant with no space before the dash.
PROJECT_DIR_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})\s*-")

# Directory names never descended into (case-insensitive).
EXCLUDE_DIR_NAMES = {"26sent", "_past due", "automation", OUTPUT_SUBFOLDER.lower()}

# Cost fuses: never process a job whose due date is older than grace_days,
# and never process more than max_projects in one run.
DEFAULT_GRACE_DAYS = 7
DEFAULT_MAX_PROJECTS = 20
MAX_SCAN_DEPTH = 4


def _parse_due_date(folder_name: str) -> Optional[date]:
    """Parse the MM.DD.YY bid-due-date prefix from a job folder name."""
    match = PROJECT_DIR_RE.match(folder_name)
    if not match:
        return None
    month, day, year = (int(g) for g in match.groups())
    try:
        return date(2000 + year, month, day)
    except ValueError:
        return None


def discover_job_folders(
    root: str,
    grace_days: int = DEFAULT_GRACE_DAYS,
    today: Optional[date] = None,
) -> Dict[str, List[str]]:
    """Walk ``root`` for SOP-named job folders with a current due date.

    Returns ``{"eligible": [...], "stale": [...]}`` where ``stale`` are
    SOP-named folders skipped because their due date is more than
    ``grace_days`` in the past (archives, past-due jobs).
    """
    today = today or date.today()
    cutoff = today - timedelta(days=grace_days)
    eligible: List[str] = []
    stale: List[str] = []

    root = os.path.abspath(root)
    root_depth = root.rstrip("\\/").count(os.sep)

    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in EXCLUDE_DIR_NAMES]
        if dirpath.rstrip("\\/").count(os.sep) - root_depth >= MAX_SCAN_DEPTH:
            dirnames[:] = []
            continue
        matched = [d for d in dirnames if PROJECT_DIR_RE.match(d)]
        for d in matched:
            due = _parse_due_date(d)
            full = os.path.join(dirpath, d)
            if due is not None and due >= cutoff:
                eligible.append(full)
            else:
                stale.append(full)
        # Job folders are leaves for our purposes -- don't walk inside them.
        dirnames[:] = [d for d in dirnames if d not in matched]

    return {"eligible": sorted(eligible), "stale": sorted(stale)}


def render_report(
    results: List[Dict[str, Any]],
    root: str,
    run_started: datetime,
    skipped_over_cap: int = 0,
) -> str:
    """Render the Monday-morning report for everything processed this run."""
    processed = [r for r in results if r.get("success") and not r.get("skipped")]
    failed = [r for r in results if not r.get("success")]

    lines: List[str] = []
    lines.append(f"# New Estimates - {run_started.strftime('%A %Y-%m-%d')}")
    lines.append("")
    lines.append(
        f"_Automated weekend scan at {run_started.strftime('%Y-%m-%d %H:%M')}. "
        f'Each job below has a new "{OUTPUT_SUBFOLDER}" folder with the quote, '
        f"bid package, and estimate summary. All figures are drafts - review "
        f"before sending._"
    )
    lines.append("")
    grand_total = sum(
        (r.get("quote") or {}).get("total") or 0.0 for r in processed
    )
    lines.append(
        f"**{len(processed)} job(s) estimated"
        + (f" - combined draft total ${grand_total:,.2f}" if grand_total else "")
        + (f", {len(failed)} failed" if failed else "")
        + ".**"
    )
    if skipped_over_cap:
        lines.append("")
        lines.append(
            f"_{skipped_over_cap} additional job(s) exceeded this run's "
            f"--max-projects cap and will process on the next run._"
        )
    lines.append("")

    for r in processed:
        quote = r.get("quote") or {}
        name = r.get("project_name") or os.path.basename(str(r.get("folder", "")))
        lines.append(f"## {name}")
        due = _parse_due_date(name)
        if due:
            lines.append(f"- **Bid due:** {due.strftime('%A %m/%d/%Y')}")
        if quote.get("total") is not None:
            lines.append(f"- **Draft total:** ${quote['total']:,.2f}")
        if quote.get("quote_number"):
            lines.append(f"- **Quote number:** {quote['quote_number']}")
        lines.append(
            f"- **In-scope items:** {quote.get('spec_count', 0)} spec(s), "
            f"{quote.get('measurement_count', 0)} measurement(s)"
            + (
                f" (of {quote.get('spec_count_extracted', 0)}/"
                f"{quote.get('measurement_count_extracted', 0)} extracted)"
                if quote.get("spec_count_extracted") is not None
                else ""
            )
        )
        files_processed = r.get("files_processed") or []
        if files_processed:
            shown = ", ".join(files_processed[:8])
            more = f" (+{len(files_processed) - 8} more)" if len(files_processed) > 8 else ""
            lines.append(f"- **Source PDFs:** {shown}{more}")
        lines.append(f"- **Outputs:** {os.path.join(str(r.get('folder', '')), OUTPUT_SUBFOLDER)}")
        lines.append("")

    if failed:
        lines.append("## Needs attention (failed)")
        for r in failed:
            lines.append(
                f"- {r.get('folder')}: {r.get('error') or r.get('reason') or 'unknown error'}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def run(
    root: str,
    force: bool = False,
    write_report: bool = True,
    pricebook_path: Optional[str] = None,
    markup: float = 1.0,
    grace_days: int = DEFAULT_GRACE_DAYS,
    max_projects: int = DEFAULT_MAX_PROJECTS,
) -> int:
    run_started = datetime.now()
    intake = DropboxEstimationIntake(
        root_path=root,
        pricebook_path=pricebook_path,
        markup=markup,
    )

    discovered = discover_job_folders(root, grace_days=grace_days)
    candidates = discovered["eligible"]
    logger.info(
        "Discovered %d current job folder(s) (%d stale/archived skipped)",
        len(candidates),
        len(discovered["stale"]),
    )

    skipped_over_cap = 0
    if len(candidates) > max_projects:
        skipped_over_cap = len(candidates) - max_projects
        logger.warning(
            "Capping run at %d of %d jobs (--max-projects); %d deferred to next run",
            max_projects,
            len(candidates),
            skipped_over_cap,
        )
        candidates = candidates[:max_projects]

    results: List[Dict[str, Any]] = []
    for folder in candidates:
        try:
            results.append(intake.process_project(folder, force=force))
        except Exception as exc:
            logger.exception("Failed to process job %s", folder)
            results.append({"success": False, "folder": folder, "error": str(exc)})

    processed = [r for r in results if r.get("success") and not r.get("skipped")]
    failed = [r for r in results if not r.get("success")]
    logger.info(
        "Scan complete: %d estimated, %d unchanged, %d failed",
        len(processed),
        len(results) - len(processed) - len(failed),
        len(failed),
    )

    # Only drop a report when something actually happened, so quiet weekends
    # don't litter the root folder.
    if write_report and (processed or failed):
        report_name = REPORT_FILENAME_TEMPLATE.format(date=run_started.strftime("%Y-%m-%d"))
        report_path = intake.dropbox.join_path(root, report_name)
        intake.dropbox.upload_file(
            report_path,
            render_report(results, root, run_started, skipped_over_cap).encode("utf-8"),
        )
        logger.info("Report written: %s", report_path)

    print(json.dumps({"results": results}, indent=2, default=str))
    return 0 if not failed else 1


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weekend Dropbox estimation intake")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="Dropbox projects root folder.")
    parser.add_argument("--list", action="store_true", help="Show what would process, then exit.")
    parser.add_argument("--force", action="store_true", help="Reprocess even unchanged folders.")
    parser.add_argument("--no-report", action="store_true", help="Skip the root report file.")
    parser.add_argument("--pricebook", default=os.getenv("PRICEBOOK_PATH"), help="JSON pricebook path.")
    parser.add_argument(
        "--markup",
        type=float,
        default=float(os.getenv("PRICE_MARKUP", "1.0")),
        help="Markup multiplier applied to unit prices.",
    )
    parser.add_argument(
        "--grace-days",
        type=int,
        default=int(os.getenv("INTAKE_GRACE_DAYS", str(DEFAULT_GRACE_DAYS))),
        help="Process jobs whose bid due date is at most this many days past.",
    )
    parser.add_argument(
        "--max-projects",
        type=int,
        default=int(os.getenv("INTAKE_MAX_PROJECTS", str(DEFAULT_MAX_PROJECTS))),
        help="Hard cap on jobs processed per run (API cost fuse).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _build_arg_parser().parse_args(argv)

    if not os.path.isdir(args.root):
        logger.error("Root folder does not exist or is not synced: %s", args.root)
        return 2

    if args.list:
        discovered = discover_job_folders(args.root, grace_days=args.grace_days)
        intake = DropboxEstimationIntake(root_path=args.root)
        would_process = []
        for folder in discovered["eligible"]:
            files = intake.dropbox.list_pdf_files(folder)
            from dropbox_intake import _project_signature
            sig = _project_signature(folder, files) if files else None
            is_new = bool(files) and (
                args.force or not intake._already_processed(folder, sig)
            )
            would_process.append(
                {"folder": folder, "pdf_count": len(files), "would_process": is_new}
            )
        print(
            json.dumps(
                {
                    "eligible": would_process,
                    "stale_skipped": len(discovered["stale"]),
                },
                indent=2,
            )
        )
        return 0

    return run(
        root=args.root,
        force=args.force,
        write_report=not args.no_report,
        pricebook_path=args.pricebook,
        markup=args.markup,
        grace_days=args.grace_days,
        max_projects=args.max_projects,
    )


if __name__ == "__main__":
    sys.exit(main())
