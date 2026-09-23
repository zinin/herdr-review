"""Agent startup dialogs: the ones the runner answers without an LLM, and the one it never answers."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Claude Code asks at startup to approve the servers of a project's .mcp.json, and saves every answer —
# Esc included — into .claude/settings.local.json of that repository. The same setting given on the
# command line makes the dialog unnecessary and is never written back.
CLAUDE_MCP_SETTINGS = '{"enableAllProjectMcpServers": true}'
MCP_DIALOG = re.compile(r"new MCP servers? found in this project", re.IGNORECASE)
MCP_REFUSAL = (
    "Claude Code asks to approve this project's MCP servers (.mcp.json); herdr-review never answers "
    "that dialog: every answer, Esc included, is saved into .claude/settings.local.json of the "
    "repository. It appears only when the profile passes its own --settings: add "
    '"enableAllProjectMcpServers": true there.'
)
# Claude Code: "❯ No, exit" / "Yes, I trust this folder" — the cursor starts on "No, exit".
CLAUDE_CURSOR = "❯"
CLAUDE_TRUST = re.compile(r"yes,\s*i trust", re.IGNORECASE)
CLAUDE_NO = re.compile(r"no,\s*exit", re.IGNORECASE)
# Codex: "› 1. Trust and continue" / "2. Quit".
CODEX_CURSOR = "›"
CODEX_TRUST = re.compile(r"trust and continue", re.IGNORECASE)
CODEX_NO = re.compile(r"\bquit\b", re.IGNORECASE)
# Grok: "Do you trust the contents of this directory?" — the key next to "Yes, proceed" is y.
GROK_TRUST = re.compile(r"do you trust the contents of this directory", re.IGNORECASE)
MAX_DIALOGS = 3          # Claude Code shows the trust dialog first and the MCP dialog after it
WAIT_MS = 30000
SCREEN_LINES = 60


@dataclass(frozen=True)
class DialogOutcome:
    resolved: bool
    refusal: str | None = None      # a dialog the runner recognised and must never answer


def startup_args(kind: str, args: list[str]) -> list[str]:
    """The args an agent of <kind> starts with. A claude agent gets the session-only MCP setting unless
    its profile passes a --settings of its own: the last --settings wins, so one of the two would be lost."""
    if kind == "claude" and not any(a == "--settings" or a.startswith("--settings=") for a in args):
        return ["--settings", CLAUDE_MCP_SETTINGS, *args]
    return list(args)


def _cursor_keys(screen: str, cursor: str, yes: re.Pattern, no: re.Pattern, back: str) -> tuple[str, ...] | None:
    """Keys that pick the <yes> option from where the cursor is, or None for an unknown layout."""
    for raw in screen.splitlines():
        if cursor not in raw:
            continue
        if yes.search(raw):
            return ("enter",)
        if no.search(raw):
            return (back, "enter")
        return None
    return None


def recognize(screen: str) -> tuple[str, tuple[str, ...] | None] | None:
    """(dialog, keys) for a known startup dialog, None for any other screen. The keys are None for the
    dialog the runner never answers and for a known dialog whose cursor layout it does not know."""
    if MCP_DIALOG.search(screen):
        return "claude-mcp", None
    if CLAUDE_TRUST.search(screen):
        return "claude-trust", _cursor_keys(screen, CLAUDE_CURSOR, CLAUDE_TRUST, CLAUDE_NO, "down")
    if CODEX_TRUST.search(screen):
        return "codex-trust", _cursor_keys(screen, CODEX_CURSOR, CODEX_TRUST, CODEX_NO, "up")
    if GROK_TRUST.search(screen):
        return "grok-trust", ("y",)
    return None


def resolve_startup_dialog(herdr, name: str) -> DialogOutcome:
    """Answer the trust dialogs of Claude Code, Codex and Grok; refuse Claude Code's MCP approval dialog."""
    for _ in range(MAX_DIALOGS):
        found = recognize(herdr.agent_read(name, source="visible", lines=SCREEN_LINES) or "")
        if found is None:
            return DialogOutcome(resolved=False)
        dialog, keys = found
        if dialog == "claude-mcp":
            return DialogOutcome(resolved=False, refusal=MCP_REFUSAL)
        if not keys or not herdr.agent_send_keys(name, *keys).ok:
            return DialogOutcome(resolved=False)
        waited = herdr.agent_wait(name, until="idle", timeout_ms=WAIT_MS)
        if waited.ok:
            after = recognize(herdr.agent_read(name, source="visible", lines=SCREEN_LINES) or "")
            if after is None:
                return DialogOutcome(resolved=True)
            if after[0] == dialog:
                return DialogOutcome(resolved=False)   # idle, yet the same dialog: stale or stuck — never a second key
        elif waited.error_code != "timeout":
            return DialogOutcome(resolved=False)       # no settled screen: a second key could answer the next dialog
        # The next dialog after an idle wait, or a dialog still up after a timed-out wait: go round.
    return DialogOutcome(resolved=False)
