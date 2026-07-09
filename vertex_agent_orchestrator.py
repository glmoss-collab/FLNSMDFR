"""
Vertex AI agent orchestrator with deterministic tool execution loop.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from vertex_ai_client import get_claude_client
from vertex_agent_tools import TOOL_REGISTRY, VERTEX_TOOL_SCHEMAS


class VertexAgentOrchestrator:
    def __init__(self, model: str = "claude-opus-4-5-20251101"):
        self.client = get_claude_client(use_vertex_ai=True)
        self.model = model

    def _assistant_text(self, content_blocks: List[Any]) -> str:
        texts: List[str] = []
        for block in content_blocks:
            if getattr(block, "type", "") == "text" and getattr(block, "text", ""):
                texts.append(block.text)
        return "\n".join(texts).strip()

    def run(
        self,
        user_message: str,
        conversation: Optional[List[Dict[str, Any]]] = None,
        max_iterations: int = 6,
    ) -> Dict[str, Any]:
        messages = list(conversation or [])
        messages.append({"role": "user", "content": user_message})
        executed_tools: List[Dict[str, Any]] = []

        for _ in range(max_iterations):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=messages,
                tools=VERTEX_TOOL_SCHEMAS,
            )
            blocks = response.content or []
            tool_uses = [block for block in blocks if getattr(block, "type", "") == "tool_use"]

            if not tool_uses:
                assistant_message = self._assistant_text(blocks)
                messages.append({"role": "assistant", "content": assistant_message})
                return {
                    "success": True,
                    "message": assistant_message,
                    "tools_executed": executed_tools,
                    "conversation": messages,
                }

            assistant_content: List[Dict[str, Any]] = []
            tool_result_content: List[Dict[str, Any]] = []
            for tool_use in tool_uses:
                name = tool_use.name
                tool_input = tool_use.input or {}
                handler = TOOL_REGISTRY.get(name)
                if handler is None:
                    result = {"success": False, "error": f"Unknown tool: {name}"}
                else:
                    try:
                        result = handler(tool_input)
                    except Exception as exc:  # noqa: BLE001
                        result = {"success": False, "error": str(exc)}
                executed_tools.append({"name": name, "input": tool_input, "result": result})
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": tool_use.id,
                        "name": name,
                        "input": tool_input,
                    }
                )
                tool_result_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(result),
                    }
                )

            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_result_content})

        return {
            "success": False,
            "error": "Max tool-iteration limit reached",
            "tools_executed": executed_tools,
            "conversation": messages,
        }
