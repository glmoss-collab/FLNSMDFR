"""
Offline regression evaluator for the API-independent service layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_service import EstimateRequest, InsulationEstimationService


def load_cases(path: Path) -> List[Dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_case(service: InsulationEstimationService, case: Dict[str, Any]) -> Dict[str, Any]:
    request = EstimateRequest(**case["input"])
    result = service.estimate(request)
    assertions = case.get("assertions", {})
    passed = True
    failures: List[str] = []

    if "total_gt" in assertions and not (result.total > float(assertions["total_gt"])):
        passed = False
        failures.append(f"total={result.total} is not > {assertions['total_gt']}")

    return {
        "id": case["id"],
        "passed": passed,
        "failures": failures,
        "total": result.total,
        "quote_number": result.quote_number,
    }


def main() -> None:
    cases = load_cases(ROOT / "evals" / "gold_tasks.json")
    service = InsulationEstimationService()
    results = [run_case(service, case) for case in cases]
    passed = sum(1 for result in results if result["passed"])
    failed = len(results) - passed
    print(json.dumps({"passed": passed, "failed": failed, "results": results}, indent=2))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
