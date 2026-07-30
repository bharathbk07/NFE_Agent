"""Load and render version-controlled LLM prompts from the project registry.

Keeping prompt text outside Python makes instructions reviewable, reusable, and
independently versionable. All prompt names resolve beneath the repository's
``prompts`` directory; callers cannot traverse outside that directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def prompt_path(name: str) -> Path:
    """Resolve a prompt filename within the project prompt directory.

    Args:
        name: Prompt filename, with or without the ``.txt`` suffix.
            Nested paths under ``prompts/`` are allowed (e.g. ``agents/supervisor``).

    Returns:
        Absolute path to the requested prompt file.

    Raises:
        ValueError: If ``name`` is empty or attempts path traversal.
    """
    filename = name.strip().replace("\\", "/")
    if not filename:
        raise ValueError("Prompt name cannot be empty.")
    if filename.startswith("/") or ".." in filename.split("/"):
        raise ValueError(f"Invalid prompt name: {name!r}")
    if not filename.endswith(".txt"):
        filename = f"{filename}.txt"

    root = PROMPTS_DIR.resolve()
    candidate = (PROMPTS_DIR / filename).resolve()
    if not str(candidate).startswith(str(root) + "/") and candidate != root:
        # Also reject if somehow outside root
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Prompt must stay under {PROMPTS_DIR}.") from exc
    return candidate


def load_prompt_text(name: str) -> str:
    """Read a UTF-8 prompt from the project prompt directory.

    Args:
        name: Prompt filename, with or without the ``.txt`` suffix.

    Returns:
        Raw prompt template text.

    Raises:
        ValueError: If the prompt name is invalid.
        FileNotFoundError: If the prompt file does not exist.
        OSError: If the file cannot be read.
    """
    return prompt_path(name).read_text(encoding="utf-8")


def render_prompt(name: str, **values: Any) -> str:
    """Render a stored prompt using named ``str.format`` placeholders.

    Args:
        name: Prompt filename, with or without the ``.txt`` suffix.
        **values: Named values referenced by the prompt template.

    Returns:
        Fully rendered prompt text ready for an LLM call.

    Raises:
        KeyError: If the prompt references a value not supplied by the caller.
        ValueError: If the prompt name or format string is invalid.
        FileNotFoundError: If the prompt file does not exist.
        OSError: If the file cannot be read.
    """
    return load_prompt_text(name).format(**values)
