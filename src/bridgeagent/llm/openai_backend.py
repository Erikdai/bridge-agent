"""OpenAI-compatible backend (OpenAI itself, DeepSeek, Qwen, relays ...).

Tries native structured outputs first; if the endpoint rejects
response_format, falls back to "reply with JSON only" plus local validation,
so third-party endpoints still work.
"""

from __future__ import annotations

import base64
import json
import re

from openai import OpenAI

from ..actions import Action
from .base import Decision

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json_object(text: str) -> str:
    """Return the first balanced {...} block in text, or the text itself."""
    text = _FENCE.sub("", text.strip())
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


_ACTION_FIELDS = set(Action.model_fields)


def coerce_decision(data: dict) -> Decision:
    """Accept the usual deviations of prompt-only JSON: action fields at the top
    level, `action` given as a bare string, missing reasoning."""
    if not isinstance(data, dict):
        raise ValueError("decision is not a JSON object")
    action = data.get("action")
    if isinstance(action, str):
        action = {"type": action}
    elif not isinstance(action, dict):
        action = {}
    for key in list(data.keys()):
        if key in _ACTION_FIELDS and key not in ("type",) or key == "type":
            action.setdefault(key, data.pop(key))
    data["action"] = action
    data.setdefault("reasoning", "")
    for key in list(data.keys()):
        if key not in ("reasoning", "note", "action"):
            data.pop(key)
    return Decision.model_validate(data)


class OpenAIPlanner:
    def __init__(self, model: str):
        self.client = OpenAI()  # reads OPENAI_API_KEY and OPENAI_BASE_URL
        self.model = model
        self._structured_ok: bool | None = None
        self._vision_ok: bool | None = None  # text-only models (DeepSeek chat etc.) reject image parts

    def _messages(self, system: str, user_text: str, screenshot_png: bytes | None) -> list[dict]:
        user_content: list[dict] = []
        if screenshot_png and self._vision_ok is not False:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(screenshot_png).decode()},
                }
            )
        elif screenshot_png:
            user_text += "\n(A screenshot was captured but this model cannot see images; rely on the tree and press/launch actions.)"
        user_content.append({"type": "text", "text": user_text})
        return [{"role": "system", "content": system}, {"role": "user", "content": user_content}]

    def decide(self, system: str, user_text: str, screenshot_png: bytes | None) -> tuple[Decision, dict]:
        try:
            return self._ask(system, user_text, Decision, screenshot_png)
        except Exception:
            if screenshot_png and self._vision_ok is None:
                self._vision_ok = False  # retry once without the image
                return self._ask(system, user_text, Decision, screenshot_png)
            raise

    def ask(self, system: str, user_text: str, schema):
        return self._ask(system, user_text, schema, None)

    def _ask(self, system: str, user_text: str, schema, screenshot_png: bytes | None):
        messages = self._messages(system, user_text, screenshot_png)
        if self._structured_ok is not False:
            try:
                resp = self.client.chat.completions.parse(model=self.model, messages=messages, response_format=schema)
                parsed = resp.choices[0].message.parsed
                if parsed is not None:
                    self._structured_ok = True
                    return parsed, self._usage(resp)
            except Exception as e:  # endpoint does not support json_schema
                if self._structured_ok is None:
                    self._structured_ok = False
                else:
                    raise e
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        messages[0]["content"] += (
            "\n\nReply with a single JSON object only, no prose, matching this JSON schema:\n" + schema_json
        )
        resp = self.client.chat.completions.create(model=self.model, messages=messages)
        text = resp.choices[0].message.content or ""
        data = json.loads(extract_json_object(text))
        parsed = coerce_decision(data) if schema is Decision else schema.model_validate(data)
        if screenshot_png and self._vision_ok is None:
            self._vision_ok = True
        return parsed, self._usage(resp)

    @staticmethod
    def _usage(resp) -> dict:
        u = getattr(resp, "usage", None)
        if u is None:
            return {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        cached = 0
        details = getattr(u, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        return {
            "input_tokens": u.prompt_tokens,
            "output_tokens": u.completion_tokens,
            "cache_read_input_tokens": cached,
        }
