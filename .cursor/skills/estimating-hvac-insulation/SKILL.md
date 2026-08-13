---
name: estimating-hvac-insulation
description: >-
  Estimate HVAC insulation projects from mechanical drawings and specification
  PDFs — extract project info, specs, and takeoff measurements, validate against
  ASHRAE-style standards, price materials and labor, and generate a quote. Use
  when working on HVAC/mechanical insulation estimating, takeoffs, quotes, or
  when analyzing construction spec/drawing PDFs in this repo (claude_agent_tools.py,
  hvac_insulation_estimator.py, hvac_insulation_skill.py, dropbox_intake.py).
---

# Estimating HVAC Insulation

Produce a mechanical-insulation estimate (specs + takeoff → priced quote) from a
project's PDFs using the tooling already in this repo. Prefer the real code below
as the source of truth; it is the estimation brain, not these notes.

## Estimation sequence

Run the tools in this order. Each step feeds the next.

```
1. extract_project_info   — project metadata from the cover/spec pages
2. extract_specifications — insulation specs from the spec PDF (section 23 07 00)
3. extract_measurements   — takeoff items (lengths, sizes, fittings) from drawings
4. validate_specifications — ASHRAE-style sanity checks on the specs
5. cross_reference_data   — match specs <-> measurements, flag gaps
6. calculate_pricing      — materials + labor + contingency + total
7. generate_quote         — full ProjectQuote (number, totals, notes, material list)
```

## How to run it

There are three real entry points. Pick by situation.

### A. Full agent loop (one PDF or project) — needs `ANTHROPIC_API_KEY`

`hvac_insulation_skill.py` wraps the 7 tools in a Claude tool-use loop. Use it
when you want the agent to decide which tools to call and in what order.

```python
from hvac_insulation_skill import HVACInsulationSkill, quick_estimate

# Simplest: one call, cached, accepts a local path or a gs:// URI.
result = quick_estimate("path/to/project.pdf")
print(result["session_data"]["quote"])

# Or drive it yourself:
skill = HVACInsulationSkill(api_key=None)            # reads ANTHROPIC_API_KEY
result = skill.analyze_project("path/to/project.pdf")  # extract→...→quote
data = skill.get_session_data()                       # {project_info, specifications,
                                                      #  measurements, pricing, quote}
```

`HVACInsulationSkill.run(user_message, max_iterations=10)` returns a dict with
`success`, `response`, `session_data`, `tool_calls`, `iterations`. The system
prompt lives in `prompts/hvac_skill.py` (`get_prompt("hvac_skill.system")`) —
reuse its domain framing. `call_tool_directly(tool_name, **kwargs)` runs a
single tool without the loop. Pass `use_vertex_ai=True` (constructor/tools) to
route through Vertex AI instead of the direct Anthropic API.

> Note: there are two `HVACInsulationSkill` classes mid-reorg. The root
> `hvac_insulation_skill.py` is the one wired into `dropbox_intake.py` and the
> entry points above. `skills/hvac_insulation_skill.py` is a divergent variant
> (`use_vertex_ai`, `generate_bid_package`); don't mix their APIs.

### B. Deterministic tools directly (no API key for steps 2–7)

The functions in `claude_agent_tools.py` can be called without the agent loop.
`extract_project_info` calls Claude (needs the key); `extract_specifications`
(regex over `pdfplumber` text) and `extract_measurements` (OpenCV line detection)
are local and key-free. The downstream four tools are pure Python on dict lists.

```python
from claude_agent_tools import (
    extract_specifications, extract_measurements,
    validate_specifications, cross_reference_data,
    calculate_pricing, generate_quote,
)

specs = extract_specifications("specs.pdf").data or []      # ToolResponse.data is a list
meas  = extract_measurements("drawings.pdf").data or []     # ToolResponse.data is a list

validate_specifications(specs)                # {status, errors, warnings, recommendations}
cross_reference_data(specs, meas)             # {unmatched_measurements, unused_specs, ...}

pricing = calculate_pricing(meas, specs, markup=1.15, contingency_percent=10.0)
quote   = generate_quote("Acme HQ", meas, specs, markup=1.15)
print(pricing["total"], quote["quote_number"], quote["total"])
```

Argument order matters: `calculate_pricing(measurements, specifications, ...)`
and `generate_quote(project_name, measurements, specifications, ...)` both take
**measurements first**. The `extract_*` tools return a `ToolResponse` (use
`.data`, `.success`, `.error`); the downstream tools return flat dicts with a
top-level `total`.

### C. Auto-intake for synced folders — `dropbox_intake.py`

For "new projects uploaded to the synced folder", point the intake at a locally
synced Dropbox folder where each sub-folder is one project. It runs the full
pipeline and writes `Quote_<number>.txt` + `Estimate_Summary.md` back into the
folder. Idempotent (skips unchanged folders unless `--force`).

```bash
python dropbox_intake.py --root "<synced project root>"   # process all new/changed
python dropbox_intake.py --root "<root>" --list           # dry run, list new only
python dropbox_intake.py --project "<one folder>"         # single project
python dropbox_intake.py --root "<root>" --force --markup 1.15 --pricebook pricebook_sample.json
```

Env: `DROPBOX_LOCAL_ROOT`/`DROPBOX_PROJECTS_ROOT`, `PRICEBOOK_PATH`,
`PRICE_MARKUP`, `CACHE_BACKEND` (`file` default, or `firestore`). The pure
renderer `build_quote_artifacts(project_name, session_data, pricebook_path, markup)`
is unit-testable in isolation if you already have session data.

## Domain guidance (grounded in the code)

**System families.** The engine collapses fine-grained types to three coarse
families: `duct`, `pipe`, `equipment` (`_spec_system_family` in
`claude_agent_tools.py`). Matching of specs↔measurements happens at the family
level, so any extracted `supply_duct`, `chilled_water_pipe`, etc. must reduce to
one of these.

**Thickness bands** (`validate_specifications`, ASHRAE 90.1 commercial-typical):

| Family | Typical thickness band |
|--------|------------------------|
| duct   | 1.0"–3.0"              |
| pipe   | 0.5"–3.0"              |

Thickness ≤ 0 is an error. Below band → warning (verify ASHRAE 90.1 minimums);
above band → warning (confirm; may indicate cold-storage / cryogenic).

**Outdoor / weather protection.** A spec with `location` in
{`outdoor`, `exposed_to_weather`, `exposed`} **must** carry one of
`aluminum_jacket`, `weatherproofing`, `stainless_bands` in
`special_requirements`, or validation raises an error.

**Chilled-water vapor barrier.** A `pipe` spec whose system type contains
`chilled` should have `vapor_barrier` or `mastic_seal`; otherwise validation
emits an info recommendation for a continuous vapor barrier.

**Materials & facings** (recognized by the extractor / pricing engine):
materials `fiberglass`, `elastomeric`, `cellular glass`, `mineral wool`;
facings `FSK`, `ASJ`, `White Vinyl`, `PVJ`, `PVC` (jacketing:
`aluminum_jacket`, `pvc_jacket_20mil/30mil`, `stainless_jacket`).

**Pricing & labor defaults** (`PricingEngine` / `QuoteGenerator`):
- `markup` is a unit-price multiplier, default `1.0` (demo/intake often use `1.15`).
- `contingency_percent` default `10.0` (added on top of subtotal).
- Labor rate `$65.0/hr`; per-LF labor: duct `0.45`, pipe `0.35`, jacketing
  `0.25`, mastic `0.15`; +20% overhead for setup/cleanup/supervision.
- Fitting allowance on insulation quantity: `+0.5` per elbow, `+1.0` per tee.
- Pricebook: pass `pricebook_path` or set `PRICEBOOK_PATH`. The engine reads
  either a supplier schema (`{"supplier_prices": [...], "defaults": {markup_percent}}`)
  or a flat `key → price` JSON; falls back to built-in default prices.
  Known files: `distribution_international_pricebook_2025_list.json`,
  `pricebook_sample.json`.

**Drawing scale.** `DrawingMeasurementExtractor` parses scales like
`1/4" = 1'-0"` (→ factor 48). If no scale is found and no measurements detected,
it auto-detects scale and retries. Lengths are linear feet; sizes are diameters
or `WxH` dimensions. CV measurement requires `opencv-python` + `pdf2image`; when
unavailable it degrades gracefully — prefer `manual_entry_measurements([...])`
or supply measurement dicts directly.

## Document-quality tips

- Use **searchable** PDFs (real text, not scanned images) — spec extraction is
  regex over `pdfplumber` text and keys off `insulation`, `division 23`,
  `section 23 07`.
- Point spec extraction at **Section 23 07 00** (HVAC insulation) pages.
- Provide mechanical **drawings with visible labels/scale** so takeoff and scale
  detection work.

## Environment

- `ANTHROPIC_API_KEY` — required for the agent loop (entry point A) and
  `extract_project_info`. Steps 2–7 in entry point B run without it.
- Optional: `use_vertex_ai=True` to route through Vertex AI Model Garden.
- Python deps: `anthropic`, `pdfplumber`, `pdf2image`, `pymupdf`,
  `opencv-python-headless`, `numpy`, `pydantic` (see `requirements.txt`).
