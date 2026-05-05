"""Tests for JudgeAgent + EstimationLoopAgent (agent_validator.py)."""

from unittest.mock import MagicMock

import pytest

from agent_validator import EstimationLoopAgent, JudgeAgent, JudgeReview


CLEAN_SPEC = {
    "system_type": "supply_duct",
    "thickness": 1.5,
    "material": "fiberglass",
    "facing": "FSK",
    "location": "indoor",
    "special_requirements": [],
}

UNPROTECTED_OUTDOOR_SPEC = {
    "system_type": "chilled_water_pipe",
    "thickness": 1.0,
    "material": "fiberglass",
    "location": "outdoor",
    "special_requirements": [],
}

DUCT_MEASUREMENT = {
    "item_id": "D-1",
    "system_type": "duct",
    "size": "12x8",
    "length": 50.0,
    "location": "mech room",
}

ORPHAN_EQUIPMENT_MEASUREMENT = {
    "item_id": "E-1",
    "system_type": "equipment",
    "size": "1",
    "length": 5.0,
    "location": "roof",
}


class TestJudgeAgent:
    def test_passes_on_clean_session(self):
        review = JudgeAgent().review({
            "specifications": [CLEAN_SPEC],
            "measurements": [DUCT_MEASUREMENT],
        })
        assert review.passed is True
        assert review.errors == []

    def test_passes_on_empty_session(self):
        # No specs, no measurements: nothing to flag, so it passes.
        review = JudgeAgent().review({"specifications": [], "measurements": []})
        assert review.passed is True

    def test_fails_on_outdoor_without_protection(self):
        review = JudgeAgent().review({
            "specifications": [UNPROTECTED_OUTDOOR_SPEC],
            "measurements": [],
        })
        assert review.passed is False
        assert any("weather protection" in e["message"] for e in review.errors)

    def test_promotes_unmatched_measurements_to_errors(self):
        review = JudgeAgent().review({
            "specifications": [CLEAN_SPEC],
            "measurements": [DUCT_MEASUREMENT, ORPHAN_EQUIPMENT_MEASUREMENT],
        })
        assert review.passed is False
        assert any(
            e.get("spec_or_item_id") == "E-1" and "no covering specification" in e["message"]
            for e in review.errors
        )

    def test_to_dict_round_trip(self):
        review = JudgeAgent().review({"specifications": [], "measurements": []})
        d = review.to_dict()
        assert d["passed"] is True
        assert "errors" in d and "warnings" in d

    def test_max_errors_to_pass_threshold(self):
        # With max_errors_to_pass=1, a single error should still pass.
        judge = JudgeAgent(max_errors_to_pass=1)
        review = judge.review({
            "specifications": [UNPROTECTED_OUTDOOR_SPEC],
            "measurements": [],
        })
        assert review.passed is True
        assert len(review.errors) == 1


class _FakeEstimator:
    """Stand-in for InsulationEstimationAgent. Configurable per-iteration session_data."""

    def __init__(self, session_data_sequence):
        self._sequence = list(session_data_sequence)
        self._idx = 0
        self.run_calls = []

    def run(self, user_message, context=None):
        self.run_calls.append({"user_message": user_message, "context": context})
        return f"response-{self._idx + 1}"

    def get_session_data(self):
        # Advance to next snapshot after each run.
        snapshot = self._sequence[min(self._idx, len(self._sequence) - 1)]
        self._idx += 1
        return snapshot


class TestEstimationLoopAgent:
    def test_succeeds_on_first_iteration_when_clean(self):
        estimator = _FakeEstimator([
            {"specifications": [CLEAN_SPEC], "measurements": [DUCT_MEASUREMENT]},
        ])
        loop = EstimationLoopAgent(estimator=estimator, max_iterations=3)
        result = loop.run("analyze project")
        assert result["success"] is True
        assert result["iterations"] == 1
        assert len(estimator.run_calls) == 1
        assert estimator.run_calls[0]["user_message"] == "analyze project"

    def test_escalates_then_passes(self):
        estimator = _FakeEstimator([
            # First pass: outdoor pipe missing protection (fails)
            {"specifications": [UNPROTECTED_OUTDOOR_SPEC], "measurements": []},
            # Second pass: estimator "fixed" the spec (passes)
            {
                "specifications": [{**UNPROTECTED_OUTDOOR_SPEC, "special_requirements": ["aluminum_jacket"]}],
                "measurements": [],
            },
        ])
        loop = EstimationLoopAgent(estimator=estimator, max_iterations=3)
        result = loop.run("analyze project")
        assert result["success"] is True
        assert result["iterations"] == 2
        # Second call should have received the escalation message, not the original.
        assert "validator flagged" in estimator.run_calls[1]["user_message"]
        assert "weather protection" in estimator.run_calls[1]["user_message"]
        # Context should only flow on iteration 1.
        assert estimator.run_calls[1]["context"] is None

    def test_returns_failure_after_max_iterations(self):
        # All iterations return the same failing session_data.
        bad = {"specifications": [UNPROTECTED_OUTDOOR_SPEC], "measurements": []}
        estimator = _FakeEstimator([bad, bad, bad])
        loop = EstimationLoopAgent(estimator=estimator, max_iterations=3)
        result = loop.run("analyze project")
        assert result["success"] is False
        assert result["iterations"] == 3
        assert len(estimator.run_calls) == 3
        assert "Max iterations" in result["error"]

    def test_history_records_each_iteration(self):
        estimator = _FakeEstimator([
            {"specifications": [UNPROTECTED_OUTDOOR_SPEC], "measurements": []},
            {"specifications": [CLEAN_SPEC], "measurements": []},
        ])
        loop = EstimationLoopAgent(estimator=estimator, max_iterations=3)
        result = loop.run("analyze project")
        assert len(result["history"]) == 2
        assert result["history"][0]["review"]["passed"] is False
        assert result["history"][1]["review"]["passed"] is True

    def test_passes_initial_context_through(self):
        estimator = _FakeEstimator([
            {"specifications": [CLEAN_SPEC], "measurements": []},
        ])
        loop = EstimationLoopAgent(estimator=estimator, max_iterations=3)
        ctx = {"spec_pdf": "/tmp/specs.pdf"}
        loop.run("analyze project", context=ctx)
        assert estimator.run_calls[0]["context"] == ctx


class TestVertexClientToolsParameter:
    """Tools pass-through and grounding helper on VertexAIMessagesClient."""

    def test_google_search_retrieval_helper_shape(self):
        from vertex_ai_client import google_search_retrieval
        assert google_search_retrieval() == {"google_search_retrieval": {}}
        assert google_search_retrieval(disable_attribution=True) == {
            "google_search_retrieval": {"disable_attribution": True}
        }

    def test_create_passes_tools_to_payload(self):
        from vertex_ai_client import (
            VertexAIMessagesClient,
            VERTEX_CLAUDE_OPUS_MODEL,
            google_search_retrieval,
        )

        client = VertexAIMessagesClient.__new__(VertexAIMessagesClient)
        client.project_id = "p"
        client.region = "us-central1"
        client.credentials = MagicMock(valid=True, token="tok")
        client._Request = MagicMock()
        client._requests = MagicMock()

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "id": "id",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": VERTEX_CLAUDE_OPUS_MODEL,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
        client._requests.post.return_value = fake_response

        tools = [
            {"name": "x", "description": "y", "input_schema": {"type": "object"}},
            google_search_retrieval(),
        ]
        client.create(messages=[{"role": "user", "content": "hi"}], tools=tools)

        sent_payload = client._requests.post.call_args.kwargs["json"]
        assert sent_payload["tools"] == tools

    def test_create_omits_tools_when_none(self):
        from vertex_ai_client import VertexAIMessagesClient, VERTEX_CLAUDE_OPUS_MODEL

        client = VertexAIMessagesClient.__new__(VertexAIMessagesClient)
        client.project_id = "p"
        client.region = "us-central1"
        client.credentials = MagicMock(valid=True, token="tok")
        client._Request = MagicMock()
        client._requests = MagicMock()

        fake_response = MagicMock()
        fake_response.json.return_value = {
            "id": "", "type": "message", "role": "assistant",
            "content": [], "model": VERTEX_CLAUDE_OPUS_MODEL,
            "stop_reason": "end_turn", "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        client._requests.post.return_value = fake_response

        client.create(messages=[{"role": "user", "content": "hi"}])
        sent_payload = client._requests.post.call_args.kwargs["json"]
        assert "tools" not in sent_payload
