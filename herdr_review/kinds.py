"""What an agent of a given kind starts with beyond its profile: claude's session-only settings; OpenCode's private
server and the run's session config."""
from __future__ import annotations

import json
from pathlib import Path

# --add-dir takes several directories, up to the next option.
ADD_DIR = "--add-dir"
# OpenCode 2 reads OPENCODE_CONFIG and OPENCODE_CONFIG_CONTENT in its server, once, at start. Only a private
# server (--standalone) starts with the agent's environment; without one the agent talks to the owner's shared
# server, which never sees the profile's config, or starts that server with the agent's environment.
OPENCODE_CONFIG_ENV = "OPENCODE_CONFIG"
OPENCODE_CONFIG_NAME = "opencode.json"
# The repository's own OpenCode config is read after OPENCODE_CONFIG: its rules would override the run's, and an
# opencode orchestrator has nobody to answer the dialog that follows; a branch under review could also start its
# MCP servers and plugins with the profile's env. The flag also keeps the repository's root AGENTS.md out of the
# agent's instructions at start.
OPENCODE_PROJECT_CONFIG_ENV = "OPENCODE_DISABLE_PROJECT_CONFIG"


def claude_session_settings(runs_dir: Path | str) -> str:
    """The session-only settings of a claude agent, given on its command line and never written back.

    Claude Code asks at startup to approve the servers of a project's .mcp.json, and saves every answer — Esc
    included — into .claude/settings.local.json of that repository: enableAllProjectMcpServers makes the dialog
    unnecessary. attribution.commit set to an empty string keeps Claude Code's commit trailer out of the fixer's
    commits: the fixer's task file forbids trailers too, and the setting makes it certain. additionalDirectories
    grants <runs_dir>, where every agent writes outside the repository: since 2.1.326 Claude Code asks at startup
    whether to trust a directory given with --add-dir that it has not trusted before, and one granted here it takes
    without asking."""
    return json.dumps({
        "enableAllProjectMcpServers": True,
        "attribution": {"commit": ""},
        "permissions": {"additionalDirectories": [str(runs_dir)]},
    }, ensure_ascii=False)


def _has_option(args: list[str], *names: str) -> bool:
    return any(a == n or a.startswith(n + "=") for a in args for n in names)


def _inside(value: str, runs_dir: Path) -> bool:
    """Whether the --add-dir value <value> names <runs_dir> or a directory inside it. Claude Code expands the `~`
    itself; a relative value counts from the agent's own directory and is never taken for the runs directory."""
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            return False
        path = path.resolve()
    except (OSError, RuntimeError):     # an unknown ~user, a symlink loop
        return False
    return path == runs_dir or runs_dir in path.parents


def _without_runs_dir(args: list[str], runs_dir: Path) -> tuple[list[str], list[str]]:
    """(<args> without the --add-dir values that name <runs_dir> or lie inside it, those values). A --add-dir left
    with none of its values goes too."""
    kept: list[str] = []
    gone: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith(ADD_DIR + "="):
            value = arg[len(ADD_DIR) + 1:]
            if _inside(value, runs_dir):
                gone.append(value)
            else:
                kept.append(arg)
            i += 1
            continue
        if arg != ADD_DIR:
            kept.append(arg)
            i += 1
            continue
        end = i + 1
        while end < len(args) and not args[end].startswith("-"):
            end += 1
        values = args[i + 1:end]
        stay = [v for v in values if not _inside(v, runs_dir)]
        gone += [v for v in values if _inside(v, runs_dir)]
        if stay or not values:
            kept += [arg, *stay]
        i = end
    return kept, gone


def _startup(kind: str, args: list[str], runs_dir: Path | str) -> tuple[list[str], list[str]]:
    if kind == "claude" and not _has_option(args, "--settings"):
        kept, gone = _without_runs_dir(args, Path(runs_dir).resolve())
        return ["--settings", claude_session_settings(runs_dir), *kept], gone
    if kind == "opencode" and not _has_option(args, "--standalone", "--server"):
        return ["--standalone", *args], []
    return list(args), []


def startup_args(kind: str, args: list[str], runs_dir: Path | str) -> list[str]:
    """The args an agent of <kind> starts with. A claude agent gets the session-only settings, which grant it
    <runs_dir>, unless its profile passes a --settings of its own: the last --settings wins, so one of the two would
    be lost. Its profile's --add-dir for <runs_dir>, or for a directory inside it, is left out: the settings grant
    that directory already, and the flag would stop the agent at Claude Code's dialog for a new directory. An
    opencode agent gets a private server unless its profile chose one itself: --standalone, or --server."""
    return _startup(kind, args, runs_dir)[0]


def left_out_add_dirs(kind: str, args: list[str], runs_dir: Path | str) -> list[str]:
    """The --add-dir values of <args> that startup_args leaves out."""
    return _startup(kind, args, runs_dir)[1]


def startup_env(kind: str, run_dir: Path | str) -> dict[str, str]:
    """What an agent of <kind> takes into its environment from the run, under its profile's env: an opencode
    agent's server reads the run's session config from OPENCODE_CONFIG, and none of the repository's own."""
    if kind == "opencode":
        return {OPENCODE_CONFIG_ENV: str(Path(run_dir) / OPENCODE_CONFIG_NAME), OPENCODE_PROJECT_CONFIG_ENV: "1"}
    return {}


def opencode_config(run_dir: Path | str) -> dict:
    """The session config of a run's opencode agents. Every agent reads and writes its run directory, which lies
    outside the repository, where OpenCode asks first; the rule allows that directory alone, not the other runs
    beside it: OpenCode checks `<directory>/*`, and its `*` crosses `/`. OpenCode also asks before it reads a
    `.env` file, and an opencode orchestrator has nobody to answer: such reads are denied instead, the examples
    still allowed, which OpenCode's last matching rule decides. The question tool's form would block an agent
    nobody sits at. A denied tool answers the model with `Permission denied`."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "external_directory": {f"{run_dir}/*": "allow"},
            "read": {"*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
            "question": "deny",
        },
    }
