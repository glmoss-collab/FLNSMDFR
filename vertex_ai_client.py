"""
Vertex AI / Claude integration for FLNSMDFR.

Provides a drop-in client for Anthropic Claude or Vertex AI Model Garden.
"""

import os
import json
import logging
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

VERTEX_CLAUDE_OPUS_MODEL = "claude-opus-4-5-20251101"
VERTEX_ENDPOINT_FORMAT = (
    "https://{region}-aiplatform.googleapis.com/v1/projects/{project_id}/"
    "locations/{region}/publishers/anthropic/models/{model}"
)
SUPPORTED_REGIONS = ["us-central1", "us-east4", "europe-west1", "asia-northeast1"]
DEFAULT_REGION = "us-central1"


class TokenUsage:
    def __init__(self, input_tokens: int = 0, output_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class ContentBlock:
    def __init__(self, type: str, text: str):
        self.type = type
        self.text = text


class MessagesResponse:
    def __init__(self, id: str, type: str, role: str, content: List[ContentBlock], model: str, stop_reason: str, usage: TokenUsage):
        self.id = id
        self.type = type
        self.role = role
        self.content = content
        self.model = model
        self.stop_reason = stop_reason
        self.usage = usage


class VertexAIMessagesClient:
    def __init__(self, project_id: str, region: str = DEFAULT_REGION, credentials: Any = None):
        if region not in SUPPORTED_REGIONS:
            raise ValueError(
                f"Region '{region}' is not supported for Claude on Vertex AI. "
                f"Choose one of: {', '.join(SUPPORTED_REGIONS)}."
            )
        self.project_id = project_id
        self.region = region
        self.credentials = credentials

        try:
            from google.auth import default as google_auth_default
            from google.auth.transport.requests import Request
            import requests
        except ImportError as exc:
            raise ImportError("google-auth and requests are required for Vertex AI integration") from exc

        if self.credentials is None:
            self.credentials, _ = google_auth_default()
        self._Request = Request
        self._requests = requests

    def _endpoint(self, model: str) -> str:
        return VERTEX_ENDPOINT_FORMAT.format(region=self.region, project_id=self.project_id, model=model)

    def _token(self) -> str:
        if not self.credentials.valid:
            self.credentials.refresh(self._Request())
        return self.credentials.token

    def create(
        self,
        model: str = VERTEX_CLAUDE_OPUS_MODEL,
        max_tokens: int = 4096,
        messages: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 1.0,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs,
    ) -> MessagesResponse:
        """Send a Messages-API request to Claude on Vertex AI.

        ``tools`` is passed through to the request payload. For Anthropic
        client-side tools, use the standard ``{"name", "description",
        "input_schema"}`` shape (see :func:`claude_agent_tools.get_tool_schemas`).
        For Google Search Grounding, use :func:`google_search_retrieval` to
        produce the tool dict — see that helper's caveat about availability
        through the Anthropic-on-Vertex endpoint.
        """
        payload = {
            "anthropic_version": "vertex-2023-10-16",
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages or [],
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        payload.update(kwargs)
        url = f"{self._endpoint(model)}:streamRawPredict"
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json"
        }
        response = self._requests.post(url, headers=headers, json=payload, timeout=300)
        response.raise_for_status()
        data = response.json()
        content = [ContentBlock(c.get("type", "text"), c.get("text", "")) for c in data.get("content", [])]
        usage = TokenUsage(input_tokens=data.get("usage", {}).get("input_tokens", 0), output_tokens=data.get("usage", {}).get("output_tokens", 0))
        return MessagesResponse(id=data.get("id", ""), type=data.get("type", "message"), role=data.get("role", "assistant"), content=content, model=data.get("model", model), stop_reason=data.get("stop_reason", ""), usage=usage)


class VertexAIClaudeClient:
    def __init__(self, project_id: Optional[str] = None, region: Optional[str] = None, credentials: Any = None):
        self.project_id = project_id or os.getenv("GCP_PROJECT")
        if not self.project_id:
            raise ValueError("GCP_PROJECT is required for Vertex AI Claude client")
        self.region = region or os.getenv("GCP_REGION", DEFAULT_REGION)
        self.messages = VertexAIMessagesClient(project_id=self.project_id, region=self.region, credentials=credentials)


def google_search_retrieval(disable_attribution: bool = False) -> Dict[str, Any]:
    """Return the Vertex-AI grounding tool dict for Google Search retrieval.

    Mirrors the shape of ``vertexai.preview.generative_models.grounding.
    GoogleSearchRetrieval`` so callers can do::

        client.messages.create(
            tools=[google_search_retrieval()],
            messages=[...],
        )

    Caveat: Vertex AI's Google Search Grounding is currently exposed for
    Gemini models. The Anthropic-on-Vertex endpoint used by
    :class:`VertexAIMessagesClient` does not yet honor this tool — the request
    will be forwarded but Claude on Vertex will not consult Search. This
    helper is here as the integration surface for when that capability
    extends to Claude, and so call sites stop hard-coding the dict shape.
    """
    config: Dict[str, Any] = {}
    if disable_attribution:
        config["disable_attribution"] = True
    return {"google_search_retrieval": config}


def get_claude_client(api_key: Optional[str] = None, project_id: Optional[str] = None, region: Optional[str] = None, use_vertex_ai: Optional[bool] = None):
    should_use_vertex = use_vertex_ai if use_vertex_ai is not None else os.getenv("USE_VERTEX_AI", "false").lower() == "true"
    if should_use_vertex:
        return VertexAIClaudeClient(project_id=project_id, region=region)
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("No Anthropic key configured and USE_VERTEX_AI!=true")
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=key)
    except ImportError as exc:
        raise ImportError("anthropic package required for direct Claude API access") from exc
