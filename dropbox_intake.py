#!/usr/bin/env python3
"""
Dropbox Estimation Intake
=========================

Adds estimation intelligence to a Dropbox intake folder: when a new project
folder of PDFs lands in Dropbox, this module runs the HVAC insulation
estimation pipeline over it and writes a **quote** and an **estimate summary**
back into that same project folder.

Design
------
* **Reuses the existing skill.** Document understanding (project info, specs,
  measurements) is delegated to :class:`hvac_insulation_skill.HVACInsulationSkill`
  so there is a single estimation brain. Pricing/quote rendering reuses the
  deterministic engines in :mod:`hvac_insulation_estimator`.
* **Idempotent.** Each project folder is fingerprinted by its PDF contents
  (Dropbox ``content_hash``); a folder is only (re)processed when its
  fingerprint changes. Markers are stored in the same cache backend the rest of
  the app uses (Firestore in prod, file cache locally).
* **Trigger-agnostic.** The default driver is scheduled polling
  (:meth:`process_new_projects`). :meth:`handle_webhook` lets a Dropbox webhook
  call the very same scan once a real-time endpoint is available.
* **Works with a locally-synced Dropbox folder.** When the Dropbox desktop app
  syncs a team folder to disk, point ``--root`` (or ``DROPBOX_LOCAL_ROOT``) at
  that path and the pipeline reads/writes the local filesystem directly --
  Dropbox sync then pushes the generated quote + summary back to the cloud. No
  API token or webhook is needed in this mode. See
  :func:`dropbox_storage.make_intake_client`.

CLI
---
    # Locally-synced Dropbox folder (each sub-folder is one project):
    python dropbox_intake.py --root "C:\\Users\\glmos\\Guaranteed Group Dropbox\\Guaranteed Group Team Folder\\Stiles"

    python dropbox_intake.py --root <ROOT> --list   # dry run, list new only
    python dropbox_intake.py --project <FOLDER>      # process one folder
    python dropbox_intake.py --root <ROOT> --force   # ignore cache markers
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from claude_agent_tools import (
    _to_insulation_spec,
    _to_measurement_item,
    cross_reference_data,
    validate_specifications,
)
from guaranteed_insulation_bid_package import generate_bid_package_text
from guaranteed_insulation_scope import (
    filter_measurements_to_scope,
    filter_specs_to_scope,
    get_scope_exclusion_summary,
)
from hvac_insulation_estimator import PricingEngine, QuoteGenerator

logger = logging.getLogger(__name__)

CACHE_CATEGORY = "dropbox_intake"
# Marker TTL: long enough to dedupe re-scans, short enough that a stale marker
# self-heals within a day.
MARKER_TTL_SECONDS = 7 * 24 * 3600

# Output artifact names written back into each project folder. Artifacts land
# in a sub-folder so estimators immediately see which projects have an
# unreviewed, auto-generated estimate waiting.
OUTPUT_SUBFOLDER = "Draft Estimate"
QUOTE_FILENAME_TEMPLATE = "Quote_{quote_number}.txt"
BID_PACKAGE_FILENAME_TEMPLATE = "Bid_Package_{quote_number}.txt"
SUMMARY_FILENAME = "Estimate_Summary.md"

# A callable that turns a list of local PDF paths into estimation session data
# (the dict shape produced by HVACInsulationSkill.get_session_data()).
Estimator = Callable[[List[str]], Dict[str, Any]]


def _get_intake_cache() -> Optional[Any]:
    """Return a cache backend for intake idempotency markers.

    Honors ``CACHE_BACKEND=firestore`` (matches cloudbuild.yaml) when available;
    otherwise falls back to the file cache. Returns None if none can be built.
    """
    backend = os.getenv("CACHE_BACKEND", "file").lower()
    if backend == "firestore":
        try:
            from firestore_cache import FirestoreCache
            return FirestoreCache()
        except ImportError:
            logger.info("Firestore cache unavailable; falling back to file cache")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Firestore cache init failed (%s); using file cache", exc)
    try:
        from utils_cache import get_cache
        return get_cache()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("File cache init failed: %s", exc)
        return None


def _project_signature(folder_path: str, files: List[Any]) -> str:
    """Fingerprint a project folder from its PDF contents.

    ``files`` are :class:`dropbox_storage.DropboxFile` (or anything exposing
    ``name`` and ``content_hash``). Two scans produce the same signature iff the
    set of files and their contents are unchanged.
    """
    parts = sorted(f"{f.name}:{f.content_hash}" for f in files)
    raw = folder_path + "|" + "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _default_estimator_factory(api_key: Optional[str]) -> Estimator:
    """Build the default estimator backed by HVACInsulationSkill.

    Imported lazily so this module (and ``build_quote_artifacts``) can be used
    without the Anthropic SDK / API key present.
    """
    from hvac_insulation_skill import HVACInsulationSkill

    skill = HVACInsulationSkill(api_key=api_key)

    def _estimate(local_paths: List[str]) -> Dict[str, Any]:
        skill.reset_session()
        file_list = "\n".join(f"- {p}" for p in local_paths)
        skill.run(
            "The following PDF documents all belong to a single HVAC insulation "
            "project. Perform a complete estimation:\n"
            f"{file_list}\n\n"
            "Steps: extract project information from the cover/spec document, "
            "extract insulation specifications, extract measurements from any "
            "mechanical drawings, validate the specifications, cross-reference "
            "specs against measurements, calculate pricing, and generate a quote."
        )
        return skill.get_session_data()

    return _estimate


class DropboxEstimationIntake:
    """Estimate new Dropbox project folders and write quotes back to Dropbox."""

    def __init__(
        self,
        *,
        dropbox_client: Optional[Any] = None,
        root_path: Optional[str] = None,
        estimator: Optional[Estimator] = None,
        cache: Optional[Any] = None,
        pricebook_path: Optional[str] = None,
        markup: float = 1.0,
        api_key: Optional[str] = None,
        output_subfolder: str = OUTPUT_SUBFOLDER,
    ):
        self._dropbox = dropbox_client
        self.root_path = root_path or os.getenv("DROPBOX_PROJECTS_ROOT", "/")
        self._estimator = estimator
        self._cache = cache
        self.pricebook_path = pricebook_path or os.getenv("PRICEBOOK_PATH")
        self.markup = markup
        self._api_key = api_key
        self.output_subfolder = output_subfolder

    # -- lazy dependencies -------------------------------------------------

    @property
    def dropbox(self) -> Any:
        if self._dropbox is None:
            from dropbox_storage import make_intake_client
            self._dropbox = make_intake_client(self.root_path)
        return self._dropbox

    @property
    def estimator(self) -> Estimator:
        if self._estimator is None:
            self._estimator = _default_estimator_factory(self._api_key)
        return self._estimator

    @property
    def cache(self) -> Optional[Any]:
        if self._cache is None:
            self._cache = _get_intake_cache()
        return self._cache

    # -- discovery ---------------------------------------------------------

    def _marker_key(self, folder_path: str) -> str:
        return hashlib.sha256(folder_path.encode("utf-8")).hexdigest()

    def _already_processed(self, folder_path: str, signature: str) -> bool:
        if self.cache is None:
            return False
        marker = self.cache.get(self._marker_key(folder_path), category=CACHE_CATEGORY)
        return marker == signature

    def discover_new_projects(self, force: bool = False) -> List[str]:
        """Return project folders whose contents are new or changed."""
        new_projects: List[str] = []
        for folder in self.dropbox.list_project_folders(self.root_path):
            files = self.dropbox.list_pdf_files(folder)
            if not files:
                continue
            signature = _project_signature(folder, files)
            if force or not self._already_processed(folder, signature):
                new_projects.append(folder)
        return new_projects

    # -- processing --------------------------------------------------------

    def process_project(self, folder_path: str, force: bool = False) -> Dict[str, Any]:
        """Estimate one project folder and write quote + summary back to it."""
        files = self.dropbox.list_pdf_files(folder_path)
        if not files:
            return {
                "success": False,
                "folder": folder_path,
                "skipped": True,
                "reason": "no PDF files in folder",
            }

        signature = _project_signature(folder_path, files)
        if not force and self._already_processed(folder_path, signature):
            return {
                "success": True,
                "folder": folder_path,
                "skipped": True,
                "reason": "already processed (unchanged)",
            }

        project_name = os.path.basename(folder_path.rstrip("/\\")) or folder_path

        with tempfile.TemporaryDirectory(prefix="dbx_intake_") as tmpdir:
            local_paths: List[str] = []
            for f in files:
                # f.name may be a relative path (e.g. "Drawings/M-101.pdf").
                local_path = os.path.join(tmpdir, *f.name.split("/"))
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "wb") as fh:
                    fh.write(self.dropbox.download_file(f.path))
                local_paths.append(local_path)

            session_data = self.estimator(local_paths)

        artifacts = build_quote_artifacts(
            project_name=project_name,
            session_data=session_data,
            pricebook_path=self.pricebook_path,
            markup=self.markup,
        )

        output_folder = (
            self.dropbox.join_path(folder_path, self.output_subfolder)
            if self.output_subfolder
            else folder_path
        )
        uploaded: List[str] = []
        for name, data in artifacts["files"].items():
            dest = self.dropbox.join_path(output_folder, name)
            uploaded.append(self.dropbox.upload_file(dest, data))

        if self.cache is not None:
            try:
                self.cache.set(
                    self._marker_key(folder_path),
                    signature,
                    category=CACHE_CATEGORY,
                    ttl=MARKER_TTL_SECONDS,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to write intake marker for %s: %s", folder_path, exc)

        return {
            "success": True,
            "folder": folder_path,
            "project_name": project_name,
            "uploaded": uploaded,
            "quote": artifacts["quote"],
            "files_processed": [f.name for f in files],
        }

    def process_new_projects(self, force: bool = False) -> List[Dict[str, Any]]:
        """Scan the root folder and process every new/changed project."""
        results: List[Dict[str, Any]] = []
        for folder in self.discover_new_projects(force=force):
            try:
                results.append(self.process_project(folder, force=force))
            except Exception as exc:
                logger.exception("Failed to process project %s", folder)
                results.append({"success": False, "folder": folder, "error": str(exc)})
        return results

    # -- webhook -----------------------------------------------------------

    def handle_webhook(
        self, body: bytes, signature: str, force: bool = False
    ) -> Dict[str, Any]:
        """Verify a Dropbox webhook and trigger a scan of new projects.

        The webhook payload only tells us *that* something changed, so we fall
        back to the same idempotent scan used by polling.
        """
        if not self.dropbox.verify_webhook_signature(body, signature):
            return {"success": False, "error": "invalid webhook signature"}
        results = self.process_new_projects(force=force)
        return {"success": True, "processed": results}


def build_quote_artifacts(
    project_name: str,
    session_data: Dict[str, Any],
    pricebook_path: Optional[str] = None,
    markup: float = 1.0,
) -> Dict[str, Any]:
    """Render quote + estimate-summary artifacts from estimation session data.

    Pure/deterministic (no network) so it is unit-testable in isolation.

    Returns a dict with:
        * ``files``: ``{filename: bytes}`` to upload (quote text + summary md)
        * ``quote``: a structured quote dict (totals, counts) for logging/UI
    """
    specs = session_data.get("specifications") or []
    measurements = session_data.get("measurements") or []
    project_info = session_data.get("project_info") or {}

    # Guaranteed Insulation Inc. scope: price external HVAC/mechanical
    # insulation only (no duct liner, waste plumbing, sprinkler, etc.).
    spec_objs_all = [_to_insulation_spec(s) for s in specs]
    meas_objs_all = [_to_measurement_item(m) for m in measurements]
    spec_objs = filter_specs_to_scope(spec_objs_all)
    meas_objs = filter_measurements_to_scope(meas_objs_all)
    scope_summary = get_scope_exclusion_summary(
        len(spec_objs_all), len(spec_objs), len(meas_objs_all), len(meas_objs)
    )

    files: Dict[str, bytes] = {}
    quote_summary: Dict[str, Any] = {
        "project_name": project_name,
        "spec_count": len(spec_objs),
        "spec_count_extracted": len(spec_objs_all),
        "measurement_count": len(meas_objs),
        "measurement_count_extracted": len(meas_objs_all),
        "scope_summary": scope_summary,
    }

    quote_obj = None
    quote_error: Optional[str] = None
    try:
        engine = PricingEngine(price_book_path=pricebook_path, markup=markup)
        materials = engine.calculate_materials(meas_objs, spec_objs)
        labor_hours, labor_cost = engine.calculate_labor(materials)
        quote_obj = QuoteGenerator().generate_quote(
            project_name=project_name,
            measurements=meas_objs,
            materials=materials,
            labor_hours=labor_hours,
            labor_cost=labor_cost,
            specs=spec_objs,
        )
    except Exception as exc:  # pragma: no cover - defensive
        quote_error = str(exc)
        logger.exception("Quote generation failed for %s", project_name)

    if quote_obj is not None:
        quote_filename = QUOTE_FILENAME_TEMPLATE.format(quote_number=quote_obj.quote_number)
        with tempfile.TemporaryDirectory(prefix="dbx_quote_") as tmpdir:
            quote_path = Path(tmpdir) / quote_filename
            QuoteGenerator().export_quote_to_file(quote_obj, quote_path)
            files[quote_filename] = quote_path.read_bytes()

        bid_filename = BID_PACKAGE_FILENAME_TEMPLATE.format(quote_number=quote_obj.quote_number)
        files[bid_filename] = generate_bid_package_text(
            quote_obj, scope_exclusion_summary=scope_summary
        ).encode("utf-8")

        quote_summary.update(
            {
                "quote_number": quote_obj.quote_number,
                "date": quote_obj.date,
                "subtotal": round(quote_obj.subtotal, 2),
                "contingency_percent": quote_obj.contingency_percent,
                "total": round(quote_obj.total, 2),
                "labor_hours": round(quote_obj.labor_hours, 2),
                "material_count": len(quote_obj.materials),
            }
        )

    summary_md = _render_summary_markdown(
        project_name=project_name,
        project_info=project_info,
        specs=specs,
        measurements=measurements,
        quote=quote_obj,
        quote_error=quote_error,
        scope_summary=scope_summary,
        in_scope_counts=(len(spec_objs), len(meas_objs)),
    )
    files[SUMMARY_FILENAME] = summary_md.encode("utf-8")

    return {"files": files, "quote": quote_summary}


def _render_summary_markdown(
    project_name: str,
    project_info: Dict[str, Any],
    specs: List[Dict[str, Any]],
    measurements: List[Dict[str, Any]],
    quote: Any,
    quote_error: Optional[str],
    scope_summary: Optional[str] = None,
    in_scope_counts: Optional[Tuple[int, int]] = None,
) -> str:
    """Build a concise, human-readable estimate summary in Markdown."""
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: List[str] = []
    lines.append(f"# Estimate Summary - {project_name}")
    lines.append("")
    lines.append(f"_Auto-generated by Dropbox estimation intake on {generated}._")
    lines.append("")

    # Project information
    lines.append("## Project")
    info_fields = [
        ("Project name", project_info.get("project_name") or project_name),
        ("Project number", project_info.get("project_number")),
        ("Client", project_info.get("client")),
        ("Location", project_info.get("location")),
        ("Architect", project_info.get("architect")),
        ("Engineer", project_info.get("engineer")),
        ("Building type", project_info.get("building_type")),
    ]
    for label, value in info_fields:
        if value:
            lines.append(f"- **{label}:** {value}")
    lines.append("")

    # Cost summary
    lines.append("## Cost Summary")
    if quote is not None:
        lines.append(f"- **Quote number:** {quote.quote_number}")
        lines.append(f"- **Date:** {quote.date}")
        lines.append(f"- **Material + labor subtotal:** ${quote.subtotal:,.2f}")
        lines.append(
            f"- **Contingency ({quote.contingency_percent:g}%):** "
            f"${quote.subtotal * quote.contingency_percent / 100:,.2f}"
        )
        lines.append(f"- **Total:** **${quote.total:,.2f}**")
        lines.append(
            f"- **Labor:** {quote.labor_hours:,.1f} hrs @ ${quote.labor_rate:,.2f}/hr"
        )
    elif quote_error:
        lines.append(f"- Quote could not be generated: {quote_error}")
    else:
        lines.append("- No quote generated.")
    lines.append("")

    # Scope counts
    lines.append("## Scope")
    lines.append(f"- **Specifications extracted:** {len(specs)}")
    lines.append(f"- **Measurement items extracted:** {len(measurements)}")
    if in_scope_counts is not None:
        specs_in, meas_in = in_scope_counts
        lines.append(f"- **In Guaranteed Insulation scope:** {specs_in} spec(s), {meas_in} measurement(s)")
    if quote is not None:
        lines.append(f"- **Consolidated material line items:** {len(quote.materials)}")
    if scope_summary:
        lines.append(f"- {scope_summary}")
    lines.append("")

    # Validation highlights
    validation = validate_specifications(specs)
    lines.append("## Specification QA")
    lines.append(f"- **Status:** {validation['status'].upper()}")
    lines.append(
        f"- {len(validation['errors'])} error(s), "
        f"{len(validation['warnings'])} warning(s), "
        f"{len(validation['recommendations'])} recommendation(s)"
    )
    for issue in validation["errors"][:5]:
        lines.append(f"  - ERROR: {issue.get('message')}")
    for issue in validation["warnings"][:5]:
        lines.append(f"  - WARNING: {issue.get('message')}")
    lines.append("")

    # Cross-reference
    if specs and measurements:
        xref = cross_reference_data(specs, measurements)
        lines.append("## Spec / Measurement Coverage")
        lines.append(f"- {xref['summary']}")
        for item in xref["unmatched_measurements"][:5]:
            lines.append(
                f"  - Unmatched measurement: {item.get('item_id')} "
                f"({item.get('system_type')})"
            )
        lines.append("")

    # Top materials by cost
    if quote is not None and quote.materials:
        top = sorted(quote.materials, key=lambda m: m.total_price, reverse=True)[:5]
        lines.append("## Top Material Costs")
        for m in top:
            lines.append(
                f"- {m.description}: {m.quantity:,.1f} {m.unit} = ${m.total_price:,.2f}"
            )
        lines.append("")

    # Notes
    if quote is not None and quote.notes:
        lines.append("## Notes")
        for note in quote.notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dropbox HVAC estimation intake")
    parser.add_argument(
        "--root",
        default=os.getenv("DROPBOX_LOCAL_ROOT") or os.getenv("DROPBOX_PROJECTS_ROOT", "/"),
        help=(
            "Root folder containing one sub-folder per project. May be a local "
            "path to a synced Dropbox folder or a Dropbox API path."
        ),
    )
    parser.add_argument(
        "--project",
        help="Process a single project folder path instead of scanning root.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List new/changed projects without processing them.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore idempotency markers and reprocess.",
    )
    parser.add_argument(
        "--pricebook",
        default=os.getenv("PRICEBOOK_PATH"),
        help="Optional path to a JSON pricebook.",
    )
    parser.add_argument(
        "--markup",
        type=float,
        default=float(os.getenv("PRICE_MARKUP", "1.0")),
        help="Markup multiplier applied to unit prices (default 1.0).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _build_arg_parser().parse_args(argv)

    intake = DropboxEstimationIntake(
        root_path=args.root,
        pricebook_path=args.pricebook,
        markup=args.markup,
    )

    if args.list:
        projects = intake.discover_new_projects(force=args.force)
        print(json.dumps({"new_projects": projects}, indent=2))
        return 0

    if args.project:
        result = intake.process_project(args.project, force=args.force)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("success") else 1

    results = intake.process_new_projects(force=args.force)
    print(json.dumps({"results": results}, indent=2, default=str))
    return 0 if all(r.get("success") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
