"""What an agent of a given kind starts with beyond its profile: claude's session-only settings; OpenCode's private
server and the run's session config."""
from __future__ import annotations

from pathlib import Path

# Claude Code asks at startup to approve the servers of a project's .mcp.json, and saves every answer —
# Esc included — into .claude/settings.local.json of that repository. The same setting given on the
# command line makes the dialog unnecessary and is never written back.
# attribution.commit set to an empty string keeps Claude Code's commit trailer out of the fixer's
# commits: the fixer's task file forbids trailers too, and the setting makes it certain.
CLAUDE_SESSION_SETTINGS = '{"enableAllProjectMcpServers": true, "attribution": {"commit": ""}}'
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


def _has_option(args: list[str], *names: str) -> bool:
    return any(a == n or a.startswith(n + "=") for a in args for n in names)


def startup_args(kind: str, args: list[str]) -> list[str]:
    """The args an agent of <kind> starts with. A claude agent gets the session-only settings unless its profile
    passes a --settings of its own: the last --settings wins, so one of the two would be lost. An opencode agent
    gets a private server unless its profile chose one itself: --standalone, or --server."""
    if kind == "claude" and not _has_option(args, "--settings"):
        return ["--settings", CLAUDE_SESSION_SETTINGS, *args]
    if kind == "opencode" and not _has_option(args, "--standalone", "--server"):
        return ["--standalone", *args]
    return list(args)


def startup_env(kind: str, run_dir: Path | str) -> dict[str, str]:
    """What an agent of <kind> takes into its environment from the run, under its profile's env: an opencode
    agent's server reads the run's session config from OPENCODE_CONFIG, and none of the repository's own."""
    if kind == "opencode":
        return {OPENCODE_CONFIG_ENV: str(Path(run_dir) / OPENCODE_CONFIG_NAME), OPENCODE_PROJECT_CONFIG_ENV: "1"}
    return {}


def opencode_config(run_dir: Path | str) -> dict:
    """The session config of a run's opencode agents. Every agent reads and writes its run directory, which lies
    outside the repository, where OpenCode asks first; the rule allows that directory alone, not the other runs
    beside it: OpenCode checks `<directory>/*`, and its `*` crosses `/`. The question tool's form would block an
    agent nobody sits at; denied, the tool answers the model with `Permission denied` instead."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "external_directory": {f"{run_dir}/*": "allow"},
            "question": "deny",
        },
    }
