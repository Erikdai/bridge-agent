from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    provider: str  # "anthropic" | "openai"
    model: str
    effort: str
    max_steps: int
    runs_dir: str
    browser_channel: str | None
    cdp_url: str | None
    confirm_mode: str = "risky"  # never | risky | always
    confirm_timeout: int = 60  # seconds before an unanswered confirmation counts as "No"
    skills_dir: str = "skills_store"
    permissions_file: str | None = None  # YAML manifest; defaults to ./permissions.yaml when present
    screenshots: str = "sparse"  # never | sparse (only when a desktop tree is nearly empty) | always


def load_settings() -> Settings:
    provider = os.getenv("BRIDGE_PROVIDER", "").strip().lower()
    if not provider:
        if os.getenv("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        else:
            provider = "anthropic"
    if provider == "anthropic":
        default_model = "claude-opus-5-5"
    else:
        default_model = os.getenv("OPENAI_MODEL", "").strip()
    return Settings(
        provider=provider,
        model=os.getenv("BRIDGE_MODEL", "").strip() or default_model,
        effort=os.getenv("BRIDGE_EFFORT", "medium").strip() or "medium",
        max_steps=int(os.getenv("BRIDGE_MAX_STEPS", "40")),
        runs_dir=os.getenv("BRIDGE_RUNS_DIR", "runs"),
        browser_channel=os.getenv("BRIDGE_BROWSER_CHANNEL", "").strip() or None,
        cdp_url=os.getenv("BRIDGE_CDP_URL", "").strip() or None,
        confirm_mode=(os.getenv("BRIDGE_CONFIRM", "risky").strip().lower() or "risky"),
        confirm_timeout=int(os.getenv("BRIDGE_CONFIRM_TIMEOUT", "60")),
        skills_dir=os.getenv("BRIDGE_SKILLS_DIR", "skills_store"),
        permissions_file=(
            os.getenv("BRIDGE_PERMISSIONS", "").strip()
            or ("permissions.yaml" if os.path.exists("permissions.yaml") else None)
        ),
        screenshots=os.getenv("BRIDGE_SCREENSHOTS", "sparse").strip().lower() or "sparse",
    )
