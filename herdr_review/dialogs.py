"""Agent screens the runner reads without an LLM: the startup dialogs it answers, the one it never answers,
Codex's startup update and grok's background work."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Claude Code asks at startup to approve the servers of a project's .mcp.json, and saves every answer —
# Esc included — into .claude/settings.local.json of that repository. kinds.CLAUDE_SESSION_SETTINGS, given
# on the command line, makes the dialog unnecessary.
MCP_DIALOG = re.compile(r"new MCP servers? found in this project", re.IGNORECASE)
MCP_REFUSAL = (
    "Claude Code asks to approve this project's MCP servers (.mcp.json); herdr-review never answers "
    "that dialog: every answer, Esc included, is saved into .claude/settings.local.json of the "
    "repository. Usually the profile passes its own --settings, which replaces the session-only one: "
    'add "enableAllProjectMcpServers": true there.'
)
# After a successful start nobody has seen a screen herdr could not read: a prompt typed there could answer
# that dialog, so none is sent.
MCP_UNCHECKED = "herdr could not read the screen to check for Claude Code's MCP approval dialog; no prompt was sent"
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
# A narrow pane of the grid layout wraps a dialog's phrase over two lines, inside the dialog's box.
BOX_DRAWING = re.compile("[\u2500-\u257f]")
WHITESPACE = re.compile(r"\s+")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CODEX_UPDATE_MENU = re.compile(r"\bUpdate available\b.*\bUpdate now\b")
CODEX_UPDATING = re.compile(r"\bUpdating Codex via\b")
CODEX_UPDATED = re.compile(r"\bUpdate ran successfully!\s*Please restart Codex\.")
CODEX_SESSION = re.compile(r"\bOpenAI Codex\s*\(v\d")
# Grok: the status line above its input while background work runs and the agent looks idle — between turns, or
# while a turn waits in get_command_or_subagent_output: "◉ 1 command still running · send a message to interrupt",
# "○ 1 command still running · 1 queued, Enter to send now", "◎ 1 command · 2 monitors · 1 loop · 1 subagent still
# running", "◎ waiting · send a message to interrupt". The glyph in front changes. Only the bottom of the screen is
# searched, where grok draws the line: the transcript above it may quote "… still running in the background".
GROK_STATUS_LINES = 20
GROK_BACKGROUND = re.compile(r"\b\d+ (?:command|monitor|loop|subagent)s?(?:\s*·\s*\d+ (?:command|monitor|loop|subagent)s?)*\s+still running\b")
GROK_WAITING = re.compile(r"\bwaiting\s*·\s*send a message to interrupt\b")
MAX_DIALOGS = 3          # Claude Code shows the trust dialog first and the MCP dialog after it
WAIT_MS = 30000
SCREEN_LINES = 60


@dataclass(frozen=True)
class DialogOutcome:
    resolved: bool
    refusal: str | None = None      # a dialog the runner recognised and must never answer
    codex_update: bool = False
    codex_updating: bool = False
    codex_ready: bool = False


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


def _flat(screen: str) -> str:
    """<screen> as one line for the phrase searches: box-drawing characters dropped, and every run of
    whitespace, line breaks included, one space."""
    return WHITESPACE.sub(" ", BOX_DRAWING.sub("", ANSI_ESCAPE.sub("", screen)))


def codex_update_started(screen: str) -> bool:
    flat = _flat(screen)
    return bool(CODEX_UPDATE_MENU.search(flat) or CODEX_UPDATING.search(flat))


def codex_update_complete(screen: str) -> bool:
    return bool(CODEX_UPDATED.search(_flat(screen)))


def codex_update_running(screen: str) -> bool:
    flat = _flat(screen)
    return bool(CODEX_UPDATING.search(flat) or CODEX_UPDATED.search(flat))


def codex_session_ready(screen: str) -> bool:
    flat = _flat(screen)
    return bool(CODEX_SESSION.search(flat) and not (
        CODEX_UPDATE_MENU.search(flat) or CODEX_UPDATING.search(flat) or CODEX_UPDATED.search(flat)
    ))


def grok_background(screen: str) -> str | None:
    """The background work grok's status line names at the bottom of <screen>, in the line's own words (`1 command
    still running`, `waiting · send a message to interrupt`); None when the line is not there."""
    bottom = _flat("\n".join(screen.splitlines()[-GROK_STATUS_LINES:]))
    found = GROK_BACKGROUND.search(bottom) or GROK_WAITING.search(bottom)
    return found.group(0) if found else None


def recognize(screen: str) -> tuple[str, tuple[str, ...] | None] | None:
    """(dialog, keys) for a known startup dialog, None for any other screen. The keys are None for the
    dialog the runner never answers and for a known dialog whose cursor layout it does not know.

    The phrases are searched in the flattened screen, so a phrase a narrow pane wraps is still found; the
    cursor is read from the raw lines, and a cursor line the wrap left without its option's words gets no key."""
    flat = _flat(screen)
    if MCP_DIALOG.search(flat):
        return "claude-mcp", None
    if CLAUDE_TRUST.search(flat):
        return "claude-trust", _cursor_keys(screen, CLAUDE_CURSOR, CLAUDE_TRUST, CLAUDE_NO, "down")
    if CODEX_TRUST.search(flat):
        return "codex-trust", _cursor_keys(screen, CODEX_CURSOR, CODEX_TRUST, CODEX_NO, "up")
    if GROK_TRUST.search(flat):
        return "grok-trust", ("y",)
    return None


def _screen(herdr, name: str) -> str | None:
    """<name>'s visible screen; None when herdr could not read it."""
    return herdr.agent_read(name, source="visible", lines=SCREEN_LINES)


def mcp_check(herdr, name: str) -> DialogOutcome:
    """Look for Claude Code's MCP approval dialog on <name>'s screen once `agent start` succeeded: resolved
    when the screen was read without it, refused with MCP_REFUSAL when it is there. A failed read is tried
    once more; when that one fails too, nothing was checked: neither resolved nor refused.

    herdr 0.9.0 takes that dialog with several servers for an idle agent, so `agent start` succeeds
    while it is up. Codex's startup update screen is reported too. No dialog is answered here: herdr
    judged the agent ready, and answering a trust dialog's lingering text could type into a live input."""
    screen = _screen(herdr, name)
    if screen is None:
        screen = _screen(herdr, name)
    if screen is None:
        return DialogOutcome(resolved=False)
    found = recognize(screen)
    if found is not None and found[0] == "claude-mcp":
        return DialogOutcome(resolved=False, refusal=MCP_REFUSAL)
    return DialogOutcome(resolved=True, codex_update=codex_update_started(screen),
                         codex_updating=codex_update_running(screen), codex_ready=codex_session_ready(screen))


def _settle(herdr, name: str) -> tuple[tuple[str, tuple[str, ...] | None] | None, bool, str | None]:
    """Return the dialog, whether its screen was read after an idle wait, and that screen.

    A wait that failed other than by timing out leaves no settled screen to look at: (None, False, None),
    and nothing more is sent. A last read that failed checked nothing, so the agent is not resolved on it."""
    waited = herdr.agent_wait(name, until="idle", timeout_ms=WAIT_MS)
    if not waited.ok and waited.error_code != "timeout":
        return None, False, None
    screen = _screen(herdr, name)
    found = recognize(screen or "")
    if found is None and not waited.ok:
        # The answered dialog is gone after a timed-out wait: the agent may only be slow to turn idle
        # (project MCP servers starting), so give it one more wait, and look again after it: herdr
        # calls the MCP dialog idle, so the wait alone proves nothing.
        waited = herdr.agent_wait(name, until="idle", timeout_ms=WAIT_MS)
        if not waited.ok and waited.error_code != "timeout":
            return None, False, None
        screen = _screen(herdr, name)
        found = recognize(screen or "")
    return found, waited.ok and screen is not None, screen


def resolve_startup_dialog(herdr, name: str) -> DialogOutcome:
    """Answer the trust dialogs of Claude Code, Codex and Grok; refuse Claude Code's MCP approval dialog.

    Each dialog is answered at most once: seen again on any later look, it is stale text or stuck, and a
    second key could pick "No, exit" or "Quit" or land in a live input. The agent is resolved only when
    it turned idle and the look after that read its screen and found no dialog."""
    answered: set[str] = set()
    idle = False
    screen = _screen(herdr, name) or ""
    found = recognize(screen)
    while found is not None:
        dialog, keys = found
        if dialog == "claude-mcp":
            return DialogOutcome(resolved=False, refusal=MCP_REFUSAL)
        if dialog in answered or len(answered) == MAX_DIALOGS or not keys:
            return DialogOutcome(resolved=False)
        if not herdr.agent_send_keys(name, *keys).ok:
            return DialogOutcome(resolved=False)
        answered.add(dialog)
        found, idle, screen = _settle(herdr, name)
    screen = screen or ""
    return DialogOutcome(resolved=idle, codex_update=codex_update_started(screen),
                         codex_updating=codex_update_running(screen), codex_ready=codex_session_ready(screen))
