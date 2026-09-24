"""Agent startup dialogs: the ones the runner answers without an LLM, and the one it never answers."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Claude Code asks at startup to approve the servers of a project's .mcp.json, and saves every answer —
# Esc included — into .claude/settings.local.json of that repository. The same setting given on the
# command line makes the dialog unnecessary and is never written back.
# attribution.commit set to an empty string keeps Claude Code's commit trailer out of the fixer's
# commits: the fixer's task file forbids trailers too, and the setting makes it certain.
CLAUDE_SESSION_SETTINGS = '{"enableAllProjectMcpServers": true, "attribution": {"commit": ""}}'
MCP_DIALOG = re.compile(r"new MCP servers? found in this project", re.IGNORECASE)
MCP_REFUSAL = (
    "Claude Code asks to approve this project's MCP servers (.mcp.json); herdr-review never answers "
    "that dialog: every answer, Esc included, is saved into .claude/settings.local.json of the "
    "repository. Usually the profile passes its own --settings, which replaces the session-only one: "
    'add "enableAllProjectMcpServers": true there.'
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
    """The args an agent of <kind> starts with. A claude agent gets the session-only settings unless
    its profile passes a --settings of its own: the last --settings wins, so one of the two would be lost."""
    if kind == "claude" and not any(a == "--settings" or a.startswith("--settings=") for a in args):
        return ["--settings", CLAUDE_SESSION_SETTINGS, *args]
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
        continue                # the glyph on an unrelated line above the options: Codex's composer, a header
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


def _screen_dialog(herdr, name: str) -> tuple[str, tuple[str, ...] | None] | None:
    """The known dialog on <name>'s visible screen; None for any other screen and for a failed read."""
    return recognize(herdr.agent_read(name, source="visible", lines=SCREEN_LINES) or "")


def mcp_refusal(herdr, name: str) -> str | None:
    """MCP_REFUSAL when Claude Code's MCP approval dialog is on <name>'s screen, else None.

    herdr 0.9.0 takes that dialog with several servers for an idle agent, so `agent start` succeeds
    while it is up. Only this dialog is looked for: herdr judged the agent ready, and answering what
    looks like a trust dialog's lingering text could type into a live input."""
    found = _screen_dialog(herdr, name)
    return MCP_REFUSAL if found is not None and found[0] == "claude-mcp" else None


def _settle(herdr, name: str) -> tuple[tuple[str, tuple[str, ...] | None] | None, bool]:
    """Wait for <name> to turn idle after an answer, then look at its screen: (the dialog on it, whether
    the agent turned idle). A wait that failed other than by timing out leaves no settled screen to
    look at: (None, False), and nothing more is sent."""
    waited = herdr.agent_wait(name, until="idle", timeout_ms=WAIT_MS)
    if not waited.ok and waited.error_code != "timeout":
        return None, False
    found = _screen_dialog(herdr, name)
    if found is None and not waited.ok:
        # The answered dialog is gone after a timed-out wait: the agent may only be slow to turn idle
        # (project MCP servers starting), so give it one more wait, and look again after it: herdr
        # calls the MCP dialog idle, so the wait alone proves nothing.
        waited = herdr.agent_wait(name, until="idle", timeout_ms=WAIT_MS)
        if not waited.ok and waited.error_code != "timeout":
            return None, False
        found = _screen_dialog(herdr, name)
    return found, waited.ok


def resolve_startup_dialog(herdr, name: str) -> DialogOutcome:
    """Answer the trust dialogs of Claude Code, Codex and Grok; refuse Claude Code's MCP approval dialog.

    Each dialog is answered at most once: seen again on any later look, it is stale text or stuck, and a
    second key could pick "No, exit" or "Quit" or land in a live input. The agent is resolved only when
    it turned idle and the look after that found no dialog."""
    answered: set[str] = set()
    idle = False
    found = _screen_dialog(herdr, name)
    while found is not None:
        dialog, keys = found
        if dialog == "claude-mcp":
            return DialogOutcome(resolved=False, refusal=MCP_REFUSAL)
        if dialog in answered or len(answered) == MAX_DIALOGS or not keys:
            return DialogOutcome(resolved=False)
        if not herdr.agent_send_keys(name, *keys).ok:
            return DialogOutcome(resolved=False)
        answered.add(dialog)
        found, idle = _settle(herdr, name)
    return DialogOutcome(resolved=idle)
