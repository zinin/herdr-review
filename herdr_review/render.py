"""Strict {PLACEHOLDER} rendering for prompt templates."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


class RenderError(ValueError):
    """A template references a placeholder that has no value."""


def placeholders(template: str) -> set[str]:
    return set(PLACEHOLDER.findall(template))


def render(template: str, values: Mapping[str, object]) -> str:
    missing = placeholders(template) - set(values)
    if missing:
        raise RenderError("unfilled placeholders: " + ", ".join(sorted(missing)))
    return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template)


def render_file(path: Path, values: Mapping[str, object]) -> str:
    return render(Path(path).read_text(encoding="utf-8"), values)
