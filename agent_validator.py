"""
Judge / Loop agents for the FLNSMDFR estimation workflow.

Pattern: Estimator generates a takeoff, Judge reviews it deterministically by
running validate_specifications + cross_reference_data, and the LoopAgent
re-prompts the Estimator with the Judge's findings until the takeoff passes
(or max_iterations is reached). The Judge does not call an LLM — it is a
pure-function validator over the Estimator's session_data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from claude_agent_tools import cross_reference_data, validate_specifications

logger = logging.getLogger(__name__)


@dataclass
class JudgeReview:
    """Structured verdict from a JudgeAgent.review() call."""

    passed: bool
    errors: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    recommendations: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class JudgeAgent:
    """Deterministic validator over an Estimator's session_data.

    Combines two existing tools:
    - validate_specifications: ASHRAE-style spec sanity (thickness bands,
      outdoor protection, chilled-water vapor barrier)
    - cross_reference_data: spec/measurement coverage gaps

    Unmatched measurements (a measurement with no covering spec family) are
    promoted to errors because they represent scope the Estimator missed.
    """

    def __init__(self, max_errors_to_pass: int = 0):
        self.max_errors_to_pass = max_errors_to_pass

    def review(self, session_data: Dict[str, Any]) -> JudgeReview:
        specs = list(session_data.get("specifications") or [])
        measurements = list(session_data.get("measurements") or [])

        validation = validate_specifications(specs)
        cross_ref = cross_reference_data(specs, measurements)

        errors: List[Dict[str, Any]] = list(validation.get("errors", []))
        warnings: List[Dict[str, Any]] = list(validation.get("warnings", []))
        recommendations: List[Dict[str, Any]] = list(validation.get("recommendations", []))

        for unmatched in cross_ref.get("unmatched_measurements", []):
            errors.append({
                "severity": "error",
                "message": (
                    f"Measurement {unmatched.get('item_id')} "
                    f"({unmatched.get('system_type')}) has no covering specification"
                ),
                "spec_or_item_id": unmatched.get("item_id"),
                "suggestion": "Extract a spec for this system family or remove the measurement",
            })

        passed = len(errors) <= self.max_errors_to_pass
        summary = (
            f"{validation.get('summary', '')} {cross_ref.get('summary', '')}"
        ).strip()

        review = JudgeReview(
            passed=passed,
            errors=errors,
            warnings=warnings,
            recommendations=recommendations,
            summary=summary,
        )
        logger.info(
            "Judge review: passed=%s errors=%d warnings=%d",
            review.passed, len(review.errors), len(review.warnings),
        )
        return review


class EstimationLoopAgent:
    """Estimator ↔ Judge loop until validation passes or max_iterations.

    On each iteration:
      1. Estimator runs (free-form prompt or last escalation message).
      2. Judge reviews session_data.
      3. If passed → return success.
         Otherwise → format errors as the next prompt and loop.

    The Estimator is responsible for actually populating session_data via
    its tools (extract_specifications, extract_measurements, etc.). The Judge
    only inspects the final state.
    """

    def __init__(
        self,
        estimator: Any,
        judge: Optional[JudgeAgent] = None,
        max_iterations: int = 3,
    ):
        if estimator is None:
            raise ValueError("estimator is required")
        self.estimator = estimator
        self.judge = judge or JudgeAgent()
        self.max_iterations = max_iterations

    def run(
        self,
        user_message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        history: List[Dict[str, Any]] = []
        review: Optional[JudgeReview] = None
        last_response: str = ""

        for iteration in range(1, self.max_iterations + 1):
            logger.info("Loop iteration %d/%d", iteration, self.max_iterations)
            last_response = self.estimator.run(user_message, context=context if iteration == 1 else None)
            review = self.judge.review(self.estimator.get_session_data())

            history.append({
                "iteration": iteration,
                "estimator_response": last_response,
                "review": review.to_dict(),
            })

            if review.passed:
                return {
                    "success": True,
                    "iterations": iteration,
                    "final_response": last_response,
                    "review": review.to_dict(),
                    "session_data": self.estimator.get_session_data(),
                    "history": history,
                }

            user_message = self._format_escalation(review)

        return {
            "success": False,
            "iterations": self.max_iterations,
            "final_response": last_response,
            "review": review.to_dict() if review else None,
            "session_data": self.estimator.get_session_data(),
            "history": history,
            "error": "Max iterations reached without passing validation",
        }

    @staticmethod
    def _format_escalation(review: JudgeReview) -> str:
        lines = [
            "The validator flagged the following issues with your last takeoff. "
            "Please refine the specifications or measurements to address each one, "
            "then re-run the relevant extraction tools.",
            "",
        ]
        for err in review.errors:
            line = f"- ERROR: {err.get('message', '(no message)')}"
            if err.get("suggestion"):
                line += f"  (suggestion: {err['suggestion']})"
            lines.append(line)
        if review.warnings:
            lines.append("")
            lines.append("Warnings (review but not blocking):")
            for w in review.warnings:
                lines.append(f"- WARN: {w.get('message', '')}")
        return "\n".join(lines)
