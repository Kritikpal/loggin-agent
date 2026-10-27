"""Loads prompt text from the repo-root `prompts/` directory so prompts can be edited without touching code, and
wraps untrusted text before it goes into a prompt."""

# SECURITY: LLM01 (Prompt Injection) — Level B (Moderate). Pattern: system_prompt_template — every piece of user,
# conversation, tool-derived or saved-note text in a prompt goes through untrusted() or conversation(), and the
# prompt that receives it says the tagged content is data. Behavioural hint, not a boundary.
# Applied by: llm-secure-patterns v1.0.0 / System Prompt Design, 2026-10-27.

import json
from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


@cache
def load_prompt(name: str) -> str:
    """Text of `prompts/<name>.md` without surrounding blank lines. Raises FileNotFoundError at import time if a
    prompt file is missing, so a bad deploy fails at startup rather than mid-chat."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def defang(text: str) -> str:
    """Swap angle brackets for look-alikes so untrusted text can never open or close a tag in a prompt."""
    return text.replace("<", "‹").replace(">", "›")


def untrusted(tag: str, text: str) -> str:
    """`text` inside <tag>...</tag>, defanged so it cannot end the block early. Name tags UNTRUSTED_* and tell the
    model in the prompt that their content is data, not instructions."""
    return f"<{tag}>\n{defang(text)}\n</{tag}>"


def conversation(turns: list[dict]) -> str:
    """Earlier turns as a JSON list inside UNTRUSTED_CONVERSATION tags. JSON escapes newlines, so a message cannot
    fake a turn from the other side with a line break and "assistant: approved"."""
    rows = [{"role": t["role"], "content": t["content"]} for t in turns]
    return untrusted("UNTRUSTED_CONVERSATION", json.dumps(rows, ensure_ascii=False))
