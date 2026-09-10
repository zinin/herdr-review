"""Known agent startup dialogs the runner answers without an LLM."""
from __future__ import annotations

import re

TRUST_DIALOG = re.compile(r"trust this folder|Yes, I trust", re.IGNORECASE)
CURSOR = "❯"
YES_OPTION = re.compile(r"yes,\s*i trust", re.IGNORECASE)
NO_OPTION = re.compile(r"no,\s*exit", re.IGNORECASE)


def _trust_keys(screen: str) -> tuple[str, ...] | None:
    """Keys that confirm the trust dialog given the current cursor line, or None if unknown."""
    for raw in screen.splitlines():
        if CURSOR not in raw:
            continue
        if YES_OPTION.search(raw):
            return ("enter",)
        if NO_OPTION.search(raw):
            return ("down", "enter")
        return None
    return None


def try_resolve_startup_dialog(herdr, name: str) -> bool:
    """Confirm the Claude Code 'trust this folder' dialog. True when the agent is idle afterwards."""
    screen = herdr.agent_read(name, source="visible", lines=60) or ""
    if not TRUST_DIALOG.search(screen):
        return False
    keys = _trust_keys(screen)
    if not keys:
        return False
    if not herdr.agent_send_keys(name, *keys).ok:
        return False
    if not herdr.agent_wait(name, until="idle", timeout_ms=30000).ok:
        return False
    screen = herdr.agent_read(name, source="visible", lines=60) or ""
    return not TRUST_DIALOG.search(screen)
