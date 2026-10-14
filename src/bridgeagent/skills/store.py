"""Skill store.

A skill is a backend-independent action sequence distilled from a successful
run: every step targets a node by descriptor (role + name), never by ref, so it
can be re-resolved on a fresh observation and replayed without an LLM call.
Matching is keyword overlap; embeddings can replace it without changing the
on-disk format.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class SkillStep:
    action: str  # click | type | press | scroll | navigate | launch | focus_browser
    target: dict[str, str] | None = None  # {"role": "button", "name": "Save"}
    surface: str | None = None  # browser | desktop
    text: str | None = None
    keys: str | None = None
    url: str | None = None
    command: str | None = None
    path: str | None = None  # local file for upload
    submit: bool = False
    window: str | None = None  # title of the window the step was taken in; replay re-activates it
    site: str | None = None  # credential site key (fill_credential); the secret itself is never stored
    template: str | None = None  # text with {v1} placeholders when the typed value came from the page
    variables: dict[str, dict] | None = None  # v1 -> {pattern, kind, example}; see params.py


@dataclass
class Skill:
    name: str
    description: str
    steps: list[SkillStep] = field(default_factory=list)
    source: str = "distilled"  # distilled | recorded | healed
    runs: int = 0
    successes: int = 0
    healed: int = 0  # times a failed replay was repaired by the LLM and written back
    last_status: str = "ok"  # ok | failed  (failed skills are skipped until re-learned)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Skill:
        data = json.loads(text)
        data["steps"] = [SkillStep(**s) for s in data.get("steps", [])]
        return cls(**data)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[\w一-鿿]+", text.lower()) if len(t) > 1}


class SkillStore:
    def __init__(self, root: str | Path = "skills_store"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, skill: Skill) -> Path:
        path = self.root / f"{skill.name}.json"
        path.write_text(skill.to_json(), encoding="utf-8")
        return path

    def load(self, name: str) -> Skill:
        return Skill.from_json((self.root / f"{name}.json").read_text(encoding="utf-8"))

    def all(self) -> list[Skill]:
        return [Skill.from_json(p.read_text(encoding="utf-8")) for p in sorted(self.root.glob("*.json"))]

    def match(self, task: str, min_overlap: float = 0.5, include_failed: bool = False) -> Skill | None:
        q = _tokens(task)
        best, best_score = None, 0.0
        for skill in self.all():
            if skill.last_status != "ok" and not include_failed:
                continue
            s = _tokens(skill.description + " " + skill.name)
            if not s or not q:
                continue
            score = len(q & s) / len(q | s)
            if score > best_score:
                best, best_score = skill, score
        return best if best_score >= min_overlap else None

    def delete(self, name: str) -> bool:
        path = self.root / f"{name}.json"
        if path.exists():
            path.unlink()
            return True
        return False
