from __future__ import annotations

import base64
import os

import anthropic

from .base import Decision, PlannerRefused


class AnthropicPlanner:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium"):
        workspace = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        self.client = anthropic.Anthropic(default_headers=headers)
        self.model = model
        self.effort = effort

    def decide(self, system: str, user_text: str, screenshot_png: bytes | None) -> tuple[Decision, dict]:
        return self._ask(system, user_text, Decision, screenshot_png)

    def ask(self, system: str, user_text: str, schema):
        return self._ask(system, user_text, schema, None)

    def _ask(self, system: str, user_text: str, schema, screenshot_png: bytes | None):
        content: list[dict] = []
        if screenshot_png:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(screenshot_png).decode(),
                    },
                }
            )
        content.append({"type": "text", "text": user_text})
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=8000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            output_config={"effort": self.effort},
        )
        if response.stop_reason == "refusal":
            raise PlannerRefused(f"model declined this step: {getattr(response, 'stop_details', None)}")
        if response.parsed_output is None:
            raise RuntimeError(f"no structured output (stop_reason={response.stop_reason})")
        u = response.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        }
        return response.parsed_output, usage
