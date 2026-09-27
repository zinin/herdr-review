# OpenCode Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A herdr-review profile of kind `opencode` (OpenCode 2) takes part in a review. herdr-review starts it with `--standalone` and with a session-only config that allows the run directory and denies the question tool. The orchestrator and the prompts know OpenCode's dialogs and its shell timeout.

**Architecture:** A new module, `herdr_review/kinds.py`, holds what an agent of each kind starts with beyond its profile: claude's `--settings` (moved from `dialogs.py`), and opencode's `--standalone`, `OPENCODE_CONFIG` and session config. `launch` writes `<run_dir>/opencode.json` when the run has an opencode agent and puts `OPENCODE_CONFIG` into the orchestrator's environment; the runner does the same for the reviewers and the fixer. The validator checks `OPENCODE_CONFIG_CONTENT`. The orchestrator and heavy-command prompts gain OpenCode's dialog keys and a rule about tool timeouts. README, the example config, CHANGELOG and SMOKE document it, and a real run with a MiMo reviewer verifies it.

**Tech Stack:** Python ≥ 3.11 (stdlib + PyYAML), `unittest`, bats with the fake herdr, herdr 0.9.1, OpenCode 2.0.18.

**Spec:** `docs/superpowers/specs/2026-09-27-opencode-agents-design.md`

## Global Constraints

- **OpenCode version.** Kind `opencode` means OpenCode 2.x; OpenCode 1.x is not supported.
- **Args.** `startup_args("opencode", args)` returns `["--standalone", *args]`, unless an argument is `--standalone` or `--server`, or starts with `--standalone=` or `--server=`.
- **Session config.** The file is `<run_dir>/opencode.json`, passed in env `OPENCODE_CONFIG`. Its content is exactly `{"$schema": "https://opencode.ai/config.json", "permission": {"external_directory": {"<runs_dir>/*": "allow"}, "question": "deny"}}`, where `<runs_dir>` is the resolved absolute path that `run.json` records.
- **Environment order.** An agent's environment is built as: the run's variables → `startup_env(kind, run_dir)` → the profile's `env` → `HERDR_REVIEW_AGENT`. A profile's own `OPENCODE_CONFIG` wins.
- **Validation message.** `profiles.<name>.env.OPENCODE_CONFIG_CONTENT: not a JSON object (<reason>)`. It never contains the value.
- **Prompt language.** Prompts to agents stay in English. Nothing addressed to the owner changes language.
- **Prompt rendering.** Rendered reviewer and fixer texts contain no `{`, and a test checks this. A template may hold only the `{UPPER_CASE}` placeholders its value set has.
- **Dependencies and the owner's config.** No new dependencies. The plugin never edits the owner's config (`~/.config/herdr-review/config.yaml`).
- **Commits.** Subjects follow the repository: `feat: …`, `test: …`, `docs: …`, each a lowercase sentence. Every commit message ends with the line `Claude-Session: https://claude.ai/code/session_01LL6CfYh2n6CN2EViZvzock`.
- **Tests.** Run them with `tests/run.sh unit` (= `python3 -m unittest discover -s tests/unit -t . -v`) and `tests/run.sh bats`. The baseline on `feat/opencode` at `dc0fe17` is 432 unit tests and 33 bats tests, all passing. In the main Claude Code session, run tests through the `build-forge:build-runner` agent.

## Review Focus

1. **A relative `settings.runs_dir`** (`.review-runs`): the session config must name the resolved absolute directory, or OpenCode's rule never matches. Task 2 has the test.
2. **An opencode profile used only as the fixer**: the runner starts the fixer late, so `launch` must still write the file. Task 2 has the test.
3. **A broken `OPENCODE_CONFIG_CONTENT` that carries an API key**: the validator's message must not echo it. Task 4 has the test.
4. **A `runs_dir` with a space in its path**: the rule must keep the path whole, since OpenCode matches literally except for `*` and `?`. Task 2 has the test.
5. **An opencode profile with `--server=URL`, or with `--standalone` after other args**: nothing may be added. Task 1 has the test.

---

### Task 1: `kinds.py` — what an agent of each kind starts with

**Files:**
- Create: `herdr_review/kinds.py`
- Modify: `herdr_review/dialogs.py:7-12` (drop `CLAUDE_SESSION_SETTINGS`), `herdr_review/dialogs.py:47-52` (drop `startup_args`)
- Modify: `herdr_review/launch.py:17` (imports)
- Create: `tests/unit/test_kinds.py`
- Modify: `tests/unit/test_dialogs.py:1-4` (imports), `tests/unit/test_dialogs.py:275-289` (drop the moved `StartupArgsTest`)
- Modify: `tests/unit/test_launch.py:11` (import)

**Interfaces:**
- Produces:
  - `herdr_review.kinds.CLAUDE_SESSION_SETTINGS: str` — unchanged value.
  - `OPENCODE_CONFIG_ENV = "OPENCODE_CONFIG"`, `OPENCODE_CONFIG_NAME = "opencode.json"`.
  - `startup_args(kind: str, args: list[str]) -> list[str]`.
  - `startup_env(kind: str, run_dir: Path | str) -> dict[str, str]`.
  - `opencode_config(runs_dir: Path | str) -> dict`.

- [ ] **Step 1: Write the failing test** — create `tests/unit/test_kinds.py`:

```python
import json
import unittest
from pathlib import Path

from herdr_review.kinds import CLAUDE_SESSION_SETTINGS, OPENCODE_CONFIG_NAME, opencode_config, startup_args, startup_env


class StartupArgsTest(unittest.TestCase):
    def test_claude_starts_with_the_session_settings(self):
        args = ["--model", "opus"]
        self.assertEqual(startup_args("claude", args), ["--settings", CLAUDE_SESSION_SETTINGS, "--model", "opus"])
        self.assertEqual(args, ["--model", "opus"])          # the profile's own list stays as it was
        self.assertEqual(json.loads(CLAUDE_SESSION_SETTINGS), {"enableAllProjectMcpServers": True, "attribution": {"commit": ""}})

    def test_a_profile_with_its_own_settings_is_left_alone(self):
        for args in (["--settings", "/x.json"], ["--model", "opus", "--settings=/x.json"]):
            with self.subTest(args=args):
                self.assertEqual(startup_args("claude", args), args)

    def test_opencode_starts_with_a_private_server(self):
        args = ["--auto"]
        self.assertEqual(startup_args("opencode", args), ["--standalone", "--auto"])
        self.assertEqual(args, ["--auto"])
        self.assertEqual(startup_args("opencode", []), ["--standalone"])

    def test_an_opencode_profile_that_chose_its_server_is_left_alone(self):
        for args in (
            ["--standalone"],
            ["--auto", "--standalone"],
            ["--server", "http://127.0.0.1:4096"],
            ["--auto", "--server=http://127.0.0.1:4096"],
        ):
            with self.subTest(args=args):
                self.assertEqual(startup_args("opencode", args), args)

    def test_other_kinds_are_left_alone(self):
        self.assertEqual(startup_args("codex", ["-m", "gpt-5.5"]), ["-m", "gpt-5.5"])
        self.assertEqual(startup_args("grok", []), [])
        self.assertEqual(startup_args("codex", ["--server=x"]), ["--server=x"])
        # OpenCode's flags mean nothing to claude: it still gets its settings
        self.assertEqual(startup_args("claude", ["--standalone"]), ["--settings", CLAUDE_SESSION_SETTINGS, "--standalone"])


class StartupEnvTest(unittest.TestCase):
    def test_an_opencode_agent_reads_the_runs_session_config(self):
        self.assertEqual(startup_env("opencode", Path("/runs/p/r1")), {"OPENCODE_CONFIG": "/runs/p/r1/opencode.json"})
        self.assertEqual(startup_env("opencode", "/runs/p/r1"), {"OPENCODE_CONFIG": "/runs/p/r1/opencode.json"})
        self.assertEqual(OPENCODE_CONFIG_NAME, "opencode.json")

    def test_other_kinds_get_nothing(self):
        for kind in ("claude", "codex", "grok", "gemini"):
            with self.subTest(kind=kind):
                self.assertEqual(startup_env(kind, Path("/runs/p/r1")), {})


class OpencodeConfigTest(unittest.TestCase):
    def test_the_run_directory_is_allowed_and_the_question_tool_denied(self):
        cfg = opencode_config(Path("/home/u/.local/state/herdr-review/runs"))
        self.assertEqual(cfg, {
            "$schema": "https://opencode.ai/config.json",
            "permission": {
                "external_directory": {"/home/u/.local/state/herdr-review/runs/*": "allow"},
                "question": "deny",
            },
        })
        self.assertEqual(json.loads(json.dumps(cfg)), cfg)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest tests.unit.test_kinds -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'herdr_review.kinds'`.

- [ ] **Step 3: Create `herdr_review/kinds.py`**

```python
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
    agent's server reads the run's session config from OPENCODE_CONFIG."""
    if kind == "opencode":
        return {OPENCODE_CONFIG_ENV: str(Path(run_dir) / OPENCODE_CONFIG_NAME)}
    return {}


def opencode_config(runs_dir: Path | str) -> dict:
    """The session config of a run's opencode agents. Every agent reads and writes the run directory, which lies
    outside the repository, where OpenCode asks first: OpenCode checks `<directory>/*`, and its `*` crosses `/`.
    The question tool's form would block an agent nobody sits at; denied, the tool answers the model with
    `Permission denied` instead."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": {
            "external_directory": {f"{runs_dir}/*": "allow"},
            "question": "deny",
        },
    }
```

- [ ] **Step 4: Remove the moved code from `herdr_review/dialogs.py`**

Replace these lines (7-13):

```python
# Claude Code asks at startup to approve the servers of a project's .mcp.json, and saves every answer —
# Esc included — into .claude/settings.local.json of that repository. The same setting given on the
# command line makes the dialog unnecessary and is never written back.
# attribution.commit set to an empty string keeps Claude Code's commit trailer out of the fixer's
# commits: the fixer's task file forbids trailers too, and the setting makes it certain.
CLAUDE_SESSION_SETTINGS = '{"enableAllProjectMcpServers": true, "attribution": {"commit": ""}}'
MCP_DIALOG = re.compile(r"new MCP servers? found in this project", re.IGNORECASE)
```

with:

```python
# Claude Code asks at startup to approve the servers of a project's .mcp.json, and saves every answer —
# Esc included — into .claude/settings.local.json of that repository. kinds.CLAUDE_SESSION_SETTINGS, given
# on the command line, makes the dialog unnecessary.
MCP_DIALOG = re.compile(r"new MCP servers? found in this project", re.IGNORECASE)
```

Then delete the whole `startup_args` function, blank lines around it included:

```python
def startup_args(kind: str, args: list[str]) -> list[str]:
    """The args an agent of <kind> starts with. A claude agent gets the session-only settings unless
    its profile passes a --settings of its own: the last --settings wins, so one of the two would be lost."""
    if kind == "claude" and not any(a == "--settings" or a.startswith("--settings=") for a in args):
        return ["--settings", CLAUDE_SESSION_SETTINGS, *args]
    return list(args)
```

- [ ] **Step 5: Point the imports at `kinds`**

In `herdr_review/launch.py`, replace line 17:

```python
from .dialogs import MCP_UNCHECKED, mcp_check, resolve_startup_dialog, startup_args
```

with:

```python
from .dialogs import MCP_UNCHECKED, mcp_check, resolve_startup_dialog
from .kinds import startup_args
```

In `tests/unit/test_dialogs.py`:
- Remove `import json` at line 1. After the move, nothing else in the file uses it; confirm with `grep -n "json\." tests/unit/test_dialogs.py`, which must print nothing.
- Replace the import at line 4 with:

```python
from herdr_review.dialogs import MCP_REFUSAL, DialogOutcome, mcp_check, recognize, resolve_startup_dialog
```

- Delete the whole `class StartupArgsTest(unittest.TestCase):` block with its three tests (lines 275-289). Its tests now live in `test_kinds.py`.

In `tests/unit/test_launch.py`, replace line 11:

```python
from herdr_review.dialogs import CLAUDE_SESSION_SETTINGS, MCP_REFUSAL, MCP_UNCHECKED
```

with:

```python
from herdr_review.dialogs import MCP_REFUSAL, MCP_UNCHECKED
from herdr_review.kinds import CLAUDE_SESSION_SETTINGS
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_kinds tests.unit.test_dialogs tests.unit.test_launch -v`
Expected: PASS.

Run: `tests/run.sh unit`
Expected: PASS. `test_dialogs.py` loses three tests, and `test_kinds.py` has eight, so the total is 437.

- [ ] **Step 7: Commit**

```bash
git add herdr_review/kinds.py herdr_review/dialogs.py herdr_review/launch.py tests/unit/test_kinds.py tests/unit/test_dialogs.py tests/unit/test_launch.py
git commit -F - <<'EOF'
feat: an opencode agent starts with a private server

kinds.py holds what an agent of a given kind starts with beyond its
profile: claude's session-only settings, moved from dialogs.py, and
now opencode's --standalone, the OPENCODE_CONFIG that names the run's
session config, and that config itself.

Claude-Session: https://claude.ai/code/session_01LL6CfYh2n6CN2EViZvzock
EOF
```

---

### Task 2: `launch` writes the session config and hands it to the orchestrator

**Files:**
- Modify: `herdr_review/launch.py` (the `kinds` import; after `run.json` is written, about line 229; the orchestrator's env, lines 291-300)
- Test: `tests/unit/test_launch.py`

**Interfaces:**
- Consumes: `kinds.OPENCODE_CONFIG_NAME`, `kinds.opencode_config(runs_dir)`, `kinds.startup_env(kind, run_dir)`, `kinds.startup_args` (Task 1).
- Produces: `<run_dir>/opencode.json` exists exactly when an agent of the run is of kind `opencode`. The runner (Task 3) names it in `OPENCODE_CONFIG`.

- [ ] **Step 1: Write the failing tests** — in `tests/unit/test_launch.py`:

Extend the Task 1 import to:

```python
from herdr_review.kinds import CLAUDE_SESSION_SETTINGS, opencode_config
```

Add after `ENV = {…}` (line 27):

```python
MIMO_CONFIG = '{"model": "xiaomi-token-plan-sgp/mimo-v2.6-pro"}'
```

Add to `class LaunchTest`, after `do_launch`:

```python
    def use_opencode(self, reviewers=("mimo", "codex"), orchestrator="claude-opus", fixer="claude-opus", args=(), env=None):
        """self.cfg with an opencode profile `mimo` next to the usual ones."""
        mimo = {"kind": "opencode", "args": list(args), "env": {"OPENCODE_CONFIG_CONTENT": MIMO_CONFIG, **(env or {})}}
        raw = {
            "profiles": {**RAW["profiles"], "mimo": mimo},
            "presets": {"default": {"reviewers": list(reviewers), "orchestrator": orchestrator, "fixer": fixer}},
            "settings": {"checkin_sec": 7},
        }
        self.cfg = parse_config(raw, {})
        self.cfg.settings.runs_dir = self.root / "runs"
```

Add these tests to `class LaunchTest`:

```python
    def test_an_opencode_reviewer_gets_a_private_server_and_the_runs_session_config(self):
        self.use_opencode()
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        runs_dir = (self.root / "runs").resolve()
        self.assertEqual(json.loads((run_dir / "opencode.json").read_text()), opencode_config(runs_dir))
        self.assertEqual(opencode_config(runs_dir)["permission"]["external_directory"], {f"{runs_dir}/*": "allow"})
        run_json = json.loads((run_dir / "run.json").read_text())
        self.assertEqual(run_json["reviewers"][0]["args"], ["--standalone"])
        self.assertEqual(run_json["reviewers"][0]["env_keys"], ["OPENCODE_CONFIG_CONTENT"])
        orch_env = self.herdr.calls_named("tab_create")[0][4]
        self.assertNotIn("OPENCODE_CONFIG", orch_env)             # a claude orchestrator gets nothing of OpenCode's

    def test_a_run_without_an_opencode_agent_writes_no_session_config(self):
        res = self.do_launch()
        self.assertFalse((Path(res["run_dir"]) / "opencode.json").exists())

    def test_an_opencode_orchestrator_reads_the_session_config_under_its_profiles_env(self):
        self.use_opencode(orchestrator="mimo")
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        env = self.herdr.calls_named("tab_create")[0][4]
        self.assertEqual(env["OPENCODE_CONFIG"], str(run_dir / "opencode.json"))
        self.assertEqual(env["OPENCODE_CONFIG_CONTENT"], MIMO_CONFIG)
        keys = list(env)
        self.assertLess(keys.index("OPENCODE_CONFIG"), keys.index("OPENCODE_CONFIG_CONTENT"))
        self.assertEqual(keys[-1], "HERDR_REVIEW_AGENT")
        self.assertEqual(self.herdr.calls_named("agent_start")[0], ("agent_start", "hrtest-orch", "opencode", "w1:p2", ["--standalone"]))

    def test_a_profiles_own_opencode_config_replaces_the_runs(self):
        self.use_opencode(orchestrator="mimo", env={"OPENCODE_CONFIG": "/home/me/opencode-review.json"})
        self.do_launch()
        self.assertEqual(self.herdr.calls_named("tab_create")[0][4]["OPENCODE_CONFIG"], "/home/me/opencode-review.json")

    def test_an_opencode_fixer_alone_still_gets_the_session_config(self):
        self.use_opencode(reviewers=("codex",), fixer="mimo")
        res = self.do_launch()
        run_dir = Path(res["run_dir"])
        self.assertTrue((run_dir / "opencode.json").is_file())
        self.assertEqual(json.loads((run_dir / "run.json").read_text())["fixer"]["args"], ["--standalone"])

    def test_a_skipped_opencode_reviewer_writes_no_session_config(self):
        self.use_opencode()
        res = launch(LaunchOptions(), self.cfg, self.herdr, ENV, self.repo, self.runner,
                     which=lambda kind: None if kind == "opencode" else which_ok(kind), run_id="hrtest")
        self.assertEqual(res["skipped"], ["mimo"])
        self.assertFalse((Path(res["run_dir"]) / "opencode.json").exists())

    def test_an_opencode_profile_that_chose_its_server_starts_as_it_says(self):
        self.use_opencode(args=["--server=http://127.0.0.1:4096"])
        res = self.do_launch()
        run_json = json.loads((Path(res["run_dir"]) / "run.json").read_text())
        self.assertEqual(run_json["reviewers"][0]["args"], ["--server=http://127.0.0.1:4096"])

    def test_the_session_config_names_a_relative_runs_dir_by_its_absolute_path(self):
        self.use_opencode()
        sub = self.repo / "sub"
        sub.mkdir()
        self.cfg.settings.runs_dir = Path(".review-runs")
        res = launch(LaunchOptions(), self.cfg, self.herdr, ENV, sub, self.runner, which=which_ok, run_id="hrrel")
        rules = json.loads((Path(res["run_dir"]) / "opencode.json").read_text())["permission"]["external_directory"]
        self.assertEqual(rules, {f"{(sub / '.review-runs').resolve()}/*": "allow"})

    def test_the_session_config_keeps_a_runs_dir_with_a_space_whole(self):
        self.use_opencode()
        self.cfg.settings.runs_dir = self.root / "review runs"
        res = self.do_launch()
        rules = json.loads((Path(res["run_dir"]) / "opencode.json").read_text())["permission"]["external_directory"]
        self.assertEqual(rules, {f"{(self.root / 'review runs').resolve()}/*": "allow"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_launch -v`
Expected: five of the nine new tests FAIL:
- `…_gets_a_private_server_…`, `…_relative_runs_dir_…` and `…_with_a_space_whole` fail with `FileNotFoundError` for `opencode.json`;
- `…_fixer_alone_…` fails with `AssertionError`;
- `…_opencode_orchestrator_…` fails with `KeyError: 'OPENCODE_CONFIG'`.

The other four pass already and guard the change: no file without an opencode agent, none for a skipped one, the `--server=` profile left alone, and a profile's own `OPENCODE_CONFIG` kept.

- [ ] **Step 3: Implement**

In `herdr_review/launch.py`, extend the Task 1 import:

```python
from .kinds import OPENCODE_CONFIG_NAME, opencode_config, startup_args, startup_env
```

Right after the line that writes `run.json`:

```python
        (run_dir / "run.json").write_text(json.dumps(run_json, indent=2, ensure_ascii=False), encoding="utf-8")
```

add:

```python
        if any(spec["kind"] == "opencode" for spec in (*reviewers_spec, orch, fixer)):
            # Every opencode agent of the run reads it through OPENCODE_CONFIG (kinds.startup_env), the fixer too,
            # which the runner starts later.
            (run_dir / OPENCODE_CONFIG_NAME).write_text(json.dumps(opencode_config(runs_dir), indent=2) + "\n", encoding="utf-8")
```

In the orchestrator's environment, replace:

```python
    env_all.update(cfg.profiles[orch_profile].env)
    env_all[exclusive.AGENT_ENV] = orch["name"]
```

with:

```python
    env_all.update(startup_env(orch["kind"], run_dir))
    env_all.update(cfg.profiles[orch_profile].env)
    env_all[exclusive.AGENT_ENV] = orch["name"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_launch -v`
Expected: PASS.

Run: `tests/run.sh unit`
Expected: PASS, 446 tests.

- [ ] **Step 5: Commit**

```bash
git add herdr_review/launch.py tests/unit/test_launch.py
git commit -F - <<'EOF'
feat: launch writes the session config of a run's opencode agents

When any agent of the run is of kind opencode, launch writes
<run_dir>/opencode.json, which allows the runs directory and denies
the question tool, and an opencode orchestrator gets OPENCODE_CONFIG
under its profile's env.

Claude-Session: https://claude.ai/code/session_01LL6CfYh2n6CN2EViZvzock
EOF
```

---

### Task 3: the runner hands the session config to opencode reviewers and fixers

**Files:**
- Modify: `herdr_review/runner.py` (imports; `_agent_env`, lines 236-239; its callers at lines 320, 349 and 588)
- Test: `tests/unit/test_runner_start.py` (the `RAW` profiles, `make_run`, new tests in `StartReviewersTest`)

**Interfaces:**
- Consumes: `kinds.startup_env(kind, run_dir)` (Task 1); the `kind` of every spec in `run.json`, which `launch` has always written.
- Produces: `Runner._agent_env(name: str, profile: str, kind: str) -> dict[str, str]`.

- [ ] **Step 1: Write the failing tests** — in `tests/unit/test_runner_start.py`:

Above `RAW`, add:

```python
MIMO_CONFIG = '{"model": "xiaomi-token-plan-sgp/mimo-v2.6-pro"}'
```

Add a profile to `RAW["profiles"]`:

```python
        "mimo": {"kind": "opencode", "env": {"OPENCODE_CONFIG_CONTENT": MIMO_CONFIG}},
```

Give `make_run` a `fixer` parameter. Replace its signature line and its fixer spec:

```python
def make_run(root: Path, repo: Path, layout="tabs", reviewers=("claude-opus", "codex", "gemini"), close=False, fixer="codex") -> Path:
```

```python
        "orchestrator": spec("claude-opus", "hrtest-orch"), "fixer": spec(fixer, "hrtest-fixer"),
```

Add to `class StartReviewersTest`:

```python
    def test_an_opencode_reviewer_reads_the_runs_session_config(self):
        run_dir = make_run(self.root, self.repo, reviewers=("mimo", "codex"))
        self.runner(run_dir).start_reviewers()
        tabs = self.herdr.calls_named("tab_create")
        self.assertEqual(tabs[0][4], {"HERDR_REVIEW_RUN": str(run_dir), "GIT_OPTIONAL_LOCKS": "0",
                                      "OPENCODE_CONFIG": str(run_dir / "opencode.json"),
                                      "OPENCODE_CONFIG_CONTENT": MIMO_CONFIG, "HERDR_REVIEW_AGENT": "hrtest-mimo"})
        self.assertEqual(list(tabs[0][4]), ["HERDR_REVIEW_RUN", "GIT_OPTIONAL_LOCKS", "OPENCODE_CONFIG",
                                            "OPENCODE_CONFIG_CONTENT", "HERDR_REVIEW_AGENT"])
        self.assertNotIn("OPENCODE_CONFIG", tabs[1][4])          # codex gets nothing of OpenCode's

    def test_an_opencode_reviewer_in_the_grid_reads_it_too(self):
        run_dir = make_run(self.root, self.repo, layout="grid", reviewers=("mimo", "codex"))
        self.runner(run_dir).start_reviewers()
        envs = {e["HERDR_REVIEW_AGENT"]: e for e in (s[5] for s in self.herdr.calls_named("pane_split")) if "HERDR_REVIEW_AGENT" in e}
        self.assertEqual(envs["hrtest-mimo"]["OPENCODE_CONFIG"], str(run_dir / "opencode.json"))
        self.assertNotIn("OPENCODE_CONFIG", envs["hrtest-codex"])

    def test_a_profiles_own_opencode_config_replaces_the_runs(self):
        self.cfg.profiles["mimo"].env = {"OPENCODE_CONFIG": "/home/me/opencode-review.json"}
        run_dir = make_run(self.root, self.repo, reviewers=("mimo",))
        self.runner(run_dir).start_reviewers()
        self.assertEqual(self.herdr.calls_named("tab_create")[0][4]["OPENCODE_CONFIG"], "/home/me/opencode-review.json")

    def test_an_opencode_fixer_reads_the_runs_session_config(self):
        run_dir = make_run(self.root, self.repo, reviewers=("codex",), fixer="mimo")
        self.runner(run_dir).start_fixer()
        env = self.herdr.calls_named("tab_create")[-1][4]
        self.assertEqual((env["OPENCODE_CONFIG"], env["HERDR_REVIEW_AGENT"]), (str(run_dir / "opencode.json"), "hrtest-fixer"))
        self.assertEqual(self.herdr.calls_named("agent_start")[-1][2], "opencode")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_runner_start -v`
Expected: three of the four new tests FAIL. The reviewer test fails on the dict comparison; the grid and fixer tests fail with `KeyError: 'OPENCODE_CONFIG'`. The test of a profile's own `OPENCODE_CONFIG` passes already and guards the change. The existing tests still PASS.

- [ ] **Step 3: Implement**

In `herdr_review/runner.py`, add after the `from .herdr import …` line:

```python
from .kinds import startup_env
```

Replace `_agent_env`:

```python
    def _agent_env(self, name: str, profile: str) -> dict[str, str]:
        """An agent's environment: the run's, then its profile's env, which may override the run's, then its own
        name, which `herdr-review exclusive` records as the holder of the build queue."""
        return {**self._base_env(), **self._profile_env(profile), exclusive.AGENT_ENV: name}
```

with:

```python
    def _agent_env(self, name: str, profile: str, kind: str) -> dict[str, str]:
        """An agent's environment: the run's, then what its kind takes from the run (kinds.startup_env: an opencode
        agent's session config), then its profile's env, which may override both, then its own name, which
        `herdr-review exclusive` records as the holder of the build queue."""
        return {**self._base_env(), **startup_env(kind, self.run_dir), **self._profile_env(profile), exclusive.AGENT_ENV: name}
```

Update the three callers:

```python
                env = self._agent_env(s["name"], s["profile"], s["kind"])
```

```python
                env = self._agent_env(spec["name"], spec["profile"], spec["kind"]) if spec else self._base_env()
```

```python
        env = self._agent_env(name, fx["profile"], fx["kind"])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_runner_start -v`
Expected: PASS.

Run: `tests/run.sh unit`
Expected: PASS, 450 tests.

- [ ] **Step 5: Commit**

```bash
git add herdr_review/runner.py tests/unit/test_runner_start.py
git commit -F - <<'EOF'
feat: opencode reviewers and fixers read the run's session config

The runner builds an agent's environment from the run's variables, then
kinds.startup_env, then the profile's env, then HERDR_REVIEW_AGENT, so
an opencode reviewer or fixer gets OPENCODE_CONFIG in tabs and in the
grid, and a profile's own OPENCODE_CONFIG still wins.

Claude-Session: https://claude.ai/code/session_01LL6CfYh2n6CN2EViZvzock
EOF
```

---

### Task 4: the validator checks `OPENCODE_CONFIG_CONTENT`

**Files:**
- Modify: `herdr_review/config.py` (imports; new constants and `_check_opencode_config` above `_parse_profiles`; one call in `_parse_profiles` after line 149)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: a `ConfigError` line `profiles.<name>.env.OPENCODE_CONFIG_CONTENT: not a JSON object (<reason>)`.

- [ ] **Step 1: Write the failing tests** — add to `class ParseConfigTest` in `tests/unit/test_config.py`:

```python
    def test_opencode_config_content_must_be_a_json_object(self):
        def mimo(value):
            return {"profiles": {"mimo": {"kind": "opencode", "env": {"OPENCODE_CONFIG_CONTENT": value}}}}
        self.assertEqual(errors_of(mimo('{"model": "xiaomi-token-plan-sgp/mimo-v2.6-pro"}')), [])
        errs = errors_of(mimo('{"model": "x",}'))
        self.assertEqual(len(errs), 1, errs)
        self.assertRegex(errs[0], r"^profiles\.mimo\.env\.OPENCODE_CONFIG_CONTENT: not a JSON object \(.+, line 1, column \d+\)$")
        for value, what in (('["model"]', "an array"), ('"model"', "a string"), ("42", "a number"), ("null", "null")):
            with self.subTest(value=value):
                self.assertEqual(errors_of(mimo(value)), [f"profiles.mimo.env.OPENCODE_CONFIG_CONTENT: not a JSON object (it is {what})"])

    def test_a_broken_opencode_config_is_reported_without_its_value(self):
        value = '{"provider": {"x": {"options": {"apiKey": "sk-live-1234567890"}}}'      # one closing brace short
        errs = errors_of({"profiles": {"mimo": {"kind": "opencode", "env": {"OPENCODE_CONFIG_CONTENT": value}}}})
        self.assertEqual(len(errs), 1, errs)
        self.assertNotIn("sk-live-1234567890", errs[0])
        self.assertNotIn("apiKey", errs[0])

    def test_the_opencode_config_is_checked_after_expansion(self):
        ok = {"profiles": {"mimo": {"kind": "opencode", "env": {"OPENCODE_CONFIG_CONTENT": '{"model": "${OC_MODEL}"}'}}}}
        self.assertEqual(errors_of(ok, {"OC_MODEL": "xiaomi-token-plan-sgp/mimo-v2.6-pro"}), [])
        unset = {"profiles": {"mimo": {"kind": "opencode", "env": {"OPENCODE_CONFIG_CONTENT": '{"n": ${OC_N}}'}}}}
        self.assertEqual(errors_of(unset), ["profiles.mimo.env.OPENCODE_CONFIG_CONTENT: environment variable ${OC_N} is not set"])

    def test_the_opencode_config_is_checked_for_any_kind(self):
        errs = errors_of({"profiles": {"claude-x": {"kind": "claude", "env": {"OPENCODE_CONFIG_CONTENT": "nope"}}}})
        self.assertEqual(len(errs), 1, errs)
        self.assertTrue(errs[0].startswith("profiles.claude-x.env.OPENCODE_CONFIG_CONTENT: not a JSON object ("), errs)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_config -v`
Expected: the new tests FAIL, because they get no errors, except the expansion test, which passes already.

- [ ] **Step 3: Implement** — in `herdr_review/config.py`:

Add `import json` to the imports, after `from __future__ import annotations`:

```python
import json
import os
import re
import stat
```

After `MIN_MASKED_VALUE_LEN = 16`, add:

```python
OPENCODE_CONFIG_CONTENT = "OPENCODE_CONFIG_CONTENT"
JSON_TYPE_NAMES = {list: "an array", str: "a string", bool: "a boolean", int: "a number", float: "a number", type(None): "null"}
```

Above `def _parse_profiles`, add:

```python
def _check_opencode_config(env: dict[str, str], errors: list[str], where: str) -> None:
    """OPENCODE_CONFIG_CONTENT must be a JSON object: OpenCode's config loader dies on one it cannot parse, and the
    agent would fail in its tab, far from the cause. The message never holds the value, which may carry a key. A
    value whose ${VAR} is not set already has its error."""
    key = OPENCODE_CONFIG_CONTENT
    if key not in env or any(e.startswith(f"{where}.env.{key}:") for e in errors):
        return
    try:
        data = json.loads(env[key])
    except json.JSONDecodeError as e:
        errors.append(f"{where}.env.{key}: not a JSON object ({e.msg}, line {e.lineno}, column {e.colno})")
        return
    if not isinstance(data, dict):
        errors.append(f"{where}.env.{key}: not a JSON object (it is {JSON_TYPE_NAMES[type(data)]})")
```

In `_parse_profiles`, right after the expansion line:

```python
        env = {k: _expand_env(str(v), environ, errors, f"{where}.env.{k}") for k, v in env.items()}
```

add:

```python
        _check_opencode_config(env, errors, where)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_config -v`
Expected: PASS.

Run: `tests/run.sh unit`
Expected: PASS, 454 tests.

- [ ] **Step 5: Commit**

```bash
git add herdr_review/config.py tests/unit/test_config.py
git commit -F - <<'EOF'
feat: the validator requires OPENCODE_CONFIG_CONTENT to be a JSON object

OpenCode's config loader dies on a document it cannot parse, and the
agent would fail in its tab, far from the cause. The message names the
profile and the key, never the value, which may carry a key.

Claude-Session: https://claude.ai/code/session_01LL6CfYh2n6CN2EViZvzock
EOF
```

---

### Task 5: the prompts know OpenCode's dialogs and a shell tool's own timeout

**Files:**
- Modify: `prompts/orchestrator.md` (Phase 2 step 1, line 45; the `blocked` cases, lines 51-56; the red flags table, line 194)
- Modify: `prompts/exclusive.md` (after line 5)
- Test: `tests/unit/test_prompts.py`

**Interfaces:**
- Consumes: the placeholders the templates already have: `{CHECKIN_SEC}`, `{RUN_DIR}` and `{RUNNER}` in `orchestrator.md`, `{RUNNER}` in `exclusive.md`. No new placeholder.

- [ ] **Step 1: Write the failing tests** — in `tests/unit/test_prompts.py`:

Add to `class PromptTemplatesTest`, after `test_orchestrator_prompt_names_the_dialogs_and_protects_the_users_files`:

```python
    def test_the_orchestrator_answers_opencodes_dialogs_and_never_always_allow(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values.update(RUNNER="/opt/hr/bin/herdr-review", RUN_DIR="/run", CHECKIN_SEC=300)
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            "OpenCode's permission dialog — `△ Permission required` over `Allow once` · `Always allow` · `Reject` — is decided by the rules above, but no letter answers it",
            "confirm with `enter` while the cursor is on `Allow once`, where it starts",
            "refuse with `esc`, then `enter` if a `Reject permission` box opens",
            "Never select `Always allow`: OpenCode keeps it for this project in every later session",
            "OpenCode asks in a `Questions` form, and herdr sends no prompt while it is open: dismiss the form with `esc`",
            "quota or API error (OpenCode's: a red `Error: …` line under its reply)",
            "| Selecting `Always allow` in OpenCode's permission dialog | `enter` on `Allow once`, or `esc` to refuse. |",
            "The call can take all of those 300 s: when your shell tool stops a command after a timeout of its own, give this call a longer one.",
        ):
            self.assertIn(phrase, text)
```

In `test_the_heavy_command_rules_reach_the_reviewer_and_the_fixer`, add to the loop body, next to the other `assertIn` lines:

```python
                self.assertIn("give the call a timeout that covers the command and up to 60 s of waiting for the turn", text)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.unit.test_prompts -v`
Expected: the new test and the heavy-command test FAIL with `AssertionError: '…' not found in …`.

- [ ] **Step 3: Edit `prompts/orchestrator.md`**

(a) In Phase 2 step 1, replace:

```
or after {CHECKIN_SEC} s (`"checkin"`). Per agent it reports
```

with:

```
or after {CHECKIN_SEC} s (`"checkin"`). The call can take all of those {CHECKIN_SEC} s: when your shell tool stops a command after a timeout of its own, give this call a longer one. Per agent it reports
```

(b) Replace the question bullet:

```
     * a question about the task (which base? which files? may I read X?) → answer in one message with `herdr agent prompt <name> "<answer>"`, using the run facts above and the reviewer's prompt file `{RUN_DIR}/prompts/<profile>.md`;
```

with two bullets, the OpenCode dialog first:

```
     * OpenCode's permission dialog — `△ Permission required` over `Allow once` · `Always allow` · `Reject` — is decided by the rules above, but no letter answers it: confirm with `enter` while the cursor is on `Allow once`, where it starts; refuse with `esc`, then `enter` if a `Reject permission` box opens. Never select `Always allow`: OpenCode keeps it for this project in every later session, the user's own included. A refusal ends that agent's turn, so the prompt that follows a refusal reaches it;
     * a question about the task (which base? which files? may I read X?) → answer in one message with `herdr agent prompt <name> "<answer>"`, using the run facts above and the reviewer's prompt file `{RUN_DIR}/prompts/<profile>.md`. OpenCode asks in a `Questions` form, and herdr sends no prompt while it is open: dismiss the form with `esc`, which ends the agent's turn, then answer;
```

(c) Replace:

```
     * a login prompt, quota or API error, or a dialog you do not understand →
```

with:

```
     * a login prompt, quota or API error (OpenCode's: a red `Error: …` line under its reply), or a dialog you do not understand →
```

(d) In the red flags table, after the row:

```
| Answering Claude Code's MCP approval dialog, even with Esc | `run fail` that agent with the reason. |
```

add the row:

```
| Selecting `Always allow` in OpenCode's permission dialog | `enter` on `Allow once`, or `esc` to refuse. |
```

- [ ] **Step 4: Edit `prompts/exclusive.md`** — after the bullet:

```
- A command still running after 30 minutes is stopped: exit code 124 with a `herdr-review exclusive: timed out` line. For a build you know takes longer, pass `--timeout <seconds>` before the `--`.
```

add the bullet:

```
- If your shell tool stops a call after a timeout of its own — OpenCode's does after 2 minutes unless the call sets a longer one — give the call a timeout that covers the command and up to 60 s of waiting for the turn: when the tool stops the call, the wrapper stops the command with it, and the build is lost.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_prompts -v`
Expected: PASS. `test_placeholder_sets` passes too, because no new placeholder appeared, and the heavy-command test's `assertNotIn("{", text)` holds.

Run: `tests/run.sh unit`
Expected: PASS, 455 tests.

- [ ] **Step 6: Commit**

```bash
git add prompts/orchestrator.md prompts/exclusive.md tests/unit/test_prompts.py
git commit -F - <<'EOF'
feat: the orchestrator answers OpenCode's dialogs, and agents outlast their tool's timeout

The orchestrator confirms OpenCode's permission dialog with enter on
Allow once, refuses with esc and never selects Always allow, which
OpenCode keeps for the project in every later session; it dismisses a
Questions form before it answers, and treats a red Error line as an API
error. The heavy-command rules and run wait tell an agent whose shell
tool stops a call after a timeout of its own to give the call a longer
one.

Claude-Session: https://claude.ai/code/session_01LL6CfYh2n6CN2EViZvzock
EOF
```

---

### Task 6: an end-to-end case through the fake herdr

**Files:**
- Test: `tests/bats/run.bats` (a new `@test` at the end of the file)

**Interfaces:**
- Consumes: the whole chain from Tasks 1-4: the validator accepts the profile, `launch` writes the file, and the runner starts the reviewer with `--standalone` and `OPENCODE_CONFIG`.

- [ ] **Step 1: Write the test** — append to `tests/bats/run.bats`:

```bash
@test "run: an opencode reviewer starts with its own server and the run's session config" {
  printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/opencode"; chmod +x "$TMP/bin/opencode"
  cat > "$XDG_CONFIG_HOME/herdr-review/config.yaml" <<EOF
profiles:
  claude-opus: {kind: claude, args: [--model, opus]}
  mimo: {kind: opencode, env: {OPENCODE_CONFIG_CONTENT: '{"model": "xiaomi-token-plan-sgp/mimo-v2.6-pro"}'}}
presets:
  default: {reviewers: [mimo], orchestrator: claude-opus, fixer: claude-opus}
settings: {runs_dir: $TMP/runs, checkin_sec: 1}
EOF
  chmod 600 "$XDG_CONFIG_HOME/herdr-review/config.yaml"
  run "$HR" launch --json
  [ "$status" -eq 0 ]
  RUN="$(run_dir_of)"; export HERDR_REVIEW_RUN="$RUN"
  RUNS="$(cd "$TMP/runs" && pwd -P)"
  [ -f "$RUN/opencode.json" ]
  json_has "$(cat "$RUN/opencode.json")" "d['permission']=={'external_directory': {'$RUNS/*': 'allow'}, 'question': 'deny'}"
  [ "$(grep -c 'label rv-.*: orch .*OPENCODE_CONFIG' "$FAKE_HERDR_LOG")" -eq 0 ]   # a claude orchestrator gets nothing of OpenCode's

  run "$HR" run start-reviewers
  [ "$status" -eq 0 ]
  grep -q "tab create --workspace w1 --cwd $REPO --label rv-.*: mimo --env HERDR_REVIEW_RUN=$RUN --env GIT_OPTIONAL_LOCKS=0 --env OPENCODE_CONFIG=$RUN/opencode.json --env OPENCODE_CONFIG_CONTENT=.* --env HERDR_REVIEW_AGENT=hr.*-mimo --no-focus" "$FAKE_HERDR_LOG"
  grep -q 'agent start hr.*-mimo --kind opencode --pane w1:p3 --timeout 300000 -- --standalone$' "$FAKE_HERDR_LOG"
}
```

- [ ] **Step 2: Run it**

Run: `bats tests/bats/run.bats`
Expected: PASS, since Tasks 1-4 are in place. If it fails, read the fake herdr log it names (`$FAKE_HERDR_LOG`, printed by bats on failure) and fix the test or the code. Do not weaken the assertions.

Run: `tests/run.sh bats`
Expected: PASS, 34 tests.

- [ ] **Step 3: Commit**

```bash
git add tests/bats/run.bats
git commit -F - <<'EOF'
test: an opencode reviewer through the fake herdr

Claude-Session: https://claude.ai/code/session_01LL6CfYh2n6CN2EViZvzock
EOF
```

---

### Task 7: README, the example config, CHANGELOG and SMOKE

**Files:**
- Modify: `README.md` (Requirements, line 11; Configure: the YAML sample, lines 71-74; the mode table, lines 99-104; the paragraph at line 106; the rules table, lines 113-114; a new "OpenCode" paragraph after line 121; Troubleshooting, after line 203)
- Modify: `config.example.yaml` (the header comment, lines 1-23; a profile after `grok`, line 66)
- Modify: `CHANGELOG.md` (an `Unreleased` section at the top)
- Modify: `tests/SMOKE.md` (item 13)
- Test: `tests/unit/test_config.py` (the example config parses)

**Interfaces:**
- Consumes: the behaviour of Tasks 1-5, described as built.

- [ ] **Step 1: Write the failing test** — add `import json` to the imports of `tests/unit/test_config.py`, and add to `class ParseConfigTest`:

```python
    def test_the_example_config_is_valid(self):
        example = Path(__file__).resolve().parents[2] / "config.example.yaml"
        cfg = load_config(example, environ={"HOME": "/home/u"})
        self.assertEqual(cfg.profiles["mimo"].kind, "opencode")
        self.assertEqual(json.loads(cfg.profiles["mimo"].env["OPENCODE_CONFIG_CONTENT"]), {"model": "xiaomi-token-plan-sgp/mimo-v2.6-pro"})
        self.assertNotIn("mimo", cfg.presets["default"].reviewers)
```

Run: `python3 -m unittest tests.unit.test_config -v`
Expected: FAIL with `KeyError: 'mimo'`.

- [ ] **Step 2: `config.example.yaml`**

After the header paragraph that ends with:

```
# gets nothing added and must carry those keys in its settings file itself.
```

add:

```
#
# Every profile of kind opencode (OpenCode 2) starts with --standalone, a private OpenCode server
# that gets the agent's environment, and with OPENCODE_CONFIG naming the run's session config,
# which allows settings.runs_dir and denies the question tool. OpenCode's own rules allow every
# command and edit and ask only about paths outside the repository, so an opencode profile needs
# no auto mode flag and no --add-dir; --auto is its yolo. Its TUI has no --model: the model goes
# into OPENCODE_CONFIG_CONTENT in env, a JSON object (`opencode models` lists the ids). A profile
# that sets its own OPENCODE_CONFIG replaces the run's, and its file must allow settings.runs_dir.
```

After the `grok` profile:

```yaml
  grok:
    kind: grok
    args: [-m, grok-4.6, --permission-mode, auto]
    # args: [-m, grok-4.6, --always-approve]
```

add:

```yaml
  mimo:                          # OpenCode 2; see the note on opencode above
    kind: opencode
    env:
      OPENCODE_CONFIG_CONTENT: '{"model": "xiaomi-token-plan-sgp/mimo-v2.6-pro"}'
    # args: [--auto]
```

Leave `presets.default` unchanged, so it does not demand a MiMo subscription from every reader.

- [ ] **Step 3: `README.md`**

(a) Requirements: after the line

```
- The agent CLIs you want to use in PATH (`claude`, `codex`, `gemini`, `grok`, `opencode`, …). `herdr agent start --help` lists every kind herdr can start.
```

add:

```
- For kind `opencode`: OpenCode 2.x, and herdr's opencode integration (`herdr integration install opencode`); without it herdr reads an opencode agent's state from its screen, less exactly.
```

(b) The YAML sample in Configure: after its `grok` profile (the line `    # args: [-m, grok-4.6, --always-approve]`), add:

```yaml
  mimo:                          # OpenCode 2: its TUI has no --model, so the model goes into its config
    kind: opencode
    env:
      OPENCODE_CONFIG_CONTENT: '{"model": "xiaomi-token-plan-sgp/mimo-v2.6-pro"}'
    # args: [--auto]             # yolo
```

(c) The mode table: after the `gemini` row, add:

```
| `opencode` | no flag: its own rules allow every command and edit and ask about paths outside the repository; herdr-review allows the run directory | `--auto` |
```

(d) In the paragraph that begins `` `<runs_dir>` is `settings.runs_dir` ``, after the sentence `The codex sandbox refuses that write without `--add-dir`; claude only consults its classifier more often.`, insert:

```
OpenCode needs no such flag: herdr-review allows the run directory in the config it gives every opencode agent (OpenCode below).
```

(e) The rules table: in the `args` row, replace `Passed verbatim, with one addition: a profile of kind `claude` without a `--settings` of its own starts with `--settings '{"enableAllProjectMcpServers": true, "attribution": {"commit": ""}}'` (see MCP servers below).` with:

```
Passed verbatim, with two additions: a profile of kind `claude` without a `--settings` of its own starts with `--settings '{"enableAllProjectMcpServers": true, "attribution": {"commit": ""}}'` (see MCP servers below), and a profile of kind `opencode` without `--standalone` or `--server` starts with `--standalone` (see OpenCode below).
```

In the `env` row, after `For tokens and base URLs.`, insert ` A profile of kind `opencode` names its model here (see OpenCode below).`

(f) After the `**MCP servers.**` paragraph, before `## Usage`, add a paragraph:

```
**OpenCode.** The OpenCode 2 TUI has no `--model`: a profile of kind `opencode` names its model in `env`, as `OPENCODE_CONFIG_CONTENT: '{"model": "<provider>/<model>"}'` (`opencode models` lists the ids), and the validator checks that the value is a JSON object. OpenCode reads its config in its server, once, at start, so every opencode agent starts with `--standalone`: a private server that inherits the agent's environment and exits with it. Without it the agent would talk to your shared OpenCode server, which ignores the profile's config, or start that server with the agent's environment for your own sessions afterwards. A profile that passes `--standalone` or `--server` gets nothing added, and with `--server` neither its `env` nor the run's config reaches that server. OpenCode asks before it reads or writes outside the repository, and every agent of a run works in the run directory, so herdr-review writes `<run_dir>/opencode.json`, which allows `<runs_dir>/*` and denies the question tool, whose form would block an agent nobody sits at, and hands it to every opencode agent as `OPENCODE_CONFIG`. A profile that sets `OPENCODE_CONFIG` itself replaces that file; its own file must then allow the run directory, or its agents ask. The orchestrator answers OpenCode's permission dialog with `Allow once` or `esc`, never with `Always allow`, which OpenCode keeps for the project in every later session. OpenCode's shell tool stops a call after 2 minutes unless the call sets a longer timeout; the prompts tell the agents to set one for a build and for `run wait`. OpenCode 1.x is not supported.
```

(g) Troubleshooting: after the bullet that begins `- A reviewer or the fixer shows ` ❓` — a dialog is waiting in its tab;`, add:

```
- An opencode agent shows ` ✗` and its last screen says `Unrecognized flag: --model` — the OpenCode 2 TUI takes no `--model`: move the model into `OPENCODE_CONFIG_CONTENT` (OpenCode in Configure).
- An opencode agent's prompt stalls and its tab shows `Model unavailable` — OpenCode does not know the model id in `OPENCODE_CONFIG_CONTENT`: check it with `opencode models`.
- An opencode agent shows ` ❓` over a red `Error: …` line — its provider refused the request (authentication or quota); the orchestrator takes it off the run.
```

- [ ] **Step 4: `CHANGELOG.md`** — after the line `All notable changes to herdr-review will be documented here.` and its blank line, add:

```
## [Unreleased]

### Added
- Agents of kind `opencode` (OpenCode 2). herdr-review starts each with `--standalone`, a private OpenCode server
  that gets the agent's environment, and with `OPENCODE_CONFIG` naming `<run_dir>/opencode.json`: a session-only
  config that allows the run directory and denies the question tool. The profile names its model in
  `OPENCODE_CONFIG_CONTENT`, which the validator requires to be a JSON object.

### Changed
- The orchestrator answers OpenCode's permission dialog with `Allow once` or `esc`, never `Always allow`, dismisses
  its question form before it answers, and takes an agent with a red `Error: …` line off the run.
- The heavy-command rules and `run wait` tell an agent whose shell tool stops a call after a timeout of its own to
  give the call a longer one.

```

- [ ] **Step 5: `tests/SMOKE.md`** — append:

```
13. OpenCode: a profile `mimo` of kind `opencode` with `OPENCODE_CONFIG_CONTENT: '{"model": "<provider>/<model>"}'` as a reviewer next to another; orchestrator and fixer `claude-opus`. The run directory holds `opencode.json`; the mimo tab's status line names the model; `runner.log` names `<run_id>-mimo` in its `exclusive:` lines when it runs tests; its review is collected with no dialog left for a human; OpenCode's `permission` table (`~/.local/share/opencode/opencode.db`) gains no row.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest tests.unit.test_config -v`
Expected: PASS.

Run: `tests/run.sh`
Expected: PASS, 456 unit and 34 bats tests.

- [ ] **Step 7: Commit**

```bash
git add README.md config.example.yaml CHANGELOG.md tests/SMOKE.md tests/unit/test_config.py
git commit -F - <<'EOF'
docs: opencode agents in the README, the example config and the changelog

Claude-Session: https://claude.ai/code/session_01LL6CfYh2n6CN2EViZvzock
EOF
```

---

### Task 8: a real run with a MiMo reviewer (with the owner)

This task needs the owner, a live herdr session and both subscriptions. Before each outward step, ask the owner, since it opens tabs and spends tokens.

**Files:**
- Modify: `README.md` (the "Checked with" sentence at line 106), after the run passes

- [ ] **Step 1: The profile.** Give the owner these lines for `~/.config/herdr-review/config.yaml`, under `profiles:`. The plugin never edits that file; the owner adds them, or explicitly asks the agent to.

```yaml
  mimo:
    kind: opencode
    env:
      OPENCODE_CONFIG_CONTENT: '{"model": "xiaomi-token-plan-sgp/mimo-v2.6-pro"}'
```

Run: `bin/herdr-review profiles --json`
Expected: exit 0, and `profiles.mimo` is `{"kind": "opencode", "args": [], "env_keys": ["OPENCODE_CONFIG_CONTENT"]}`.

- [ ] **Step 2: OpenCode's permission rows before the run**

```bash
python3 -c 'import os, sqlite3; p = os.path.expanduser("~/.local/share/opencode/opencode.db"); print(sqlite3.connect(f"file:{p}?mode=ro", uri=True).execute("select count(*) from permission").fetchone()[0])'
```

Note the number. It was 0 on 2026-09-27.

- [ ] **Step 3: Launch**, after the owner agrees, from a herdr pane in this repository on `feat/opencode`:

```bash
bin/herdr-review launch --reviewers mimo,claude-opus --orchestrator claude-opus --fixer claude-opus --description "OpenCode agents: --standalone, the run's session config through OPENCODE_CONFIG, the validator's JSON check, the orchestrator's rules for OpenCode's dialogs" --plan docs/superpowers/specs/2026-09-27-opencode-agents-design.md
```

Expected: exit 0, and a summary that names the run directory `<run_dir>` and the orchestrator `<run_id>-orch`.

- [ ] **Step 4: Watch the MiMo reviewer**

- `cat <run_dir>/opencode.json` shows the rule for the absolute `runs_dir` and `"question": "deny"`.
- `herdr agent read <run_id>-mimo --source visible --lines 60` shows the status line with `MiMo-V2.6-Pro Xiaomi Token Plan (Singapore)` and no `△ Permission required`.
- After it has run tests, `grep 'exclusive: .*-mimo' <run_dir>/runner.log` shows `exclusive: <run_id>-mimo running "…"`. A line that begins `exclusive: pid ` for its command means its environment did not reach its shell. This is a failure: stop and debug with superpowers:systematic-debugging.
- `bin/herdr-review status` shows the MiMo reviewer `working` and then `collected`. It never shows ` ❓` for the MiMo reviewer, unless a dialog came from the project's own OpenCode config, which this repository does not have.

- [ ] **Step 5: When the run finishes**

- `bin/herdr-review status`: phase `finished`, the MiMo reviewer `collected`.
- `<run_dir>/reviews/mimo.md` exists and holds all five headings.
- Rerun the command from Step 2: the number is unchanged.
- The fixer's commits are real fixes to this branch. Review them as usual: `git log --oneline dc0fe17..HEAD`.

If a check fails, fix the cause in the task that owns the code, with a test first, and repeat this task's Steps 3-5.

- [ ] **Step 6: Record the check in the README.** In the sentence that ends `…: reviewers, orchestrator and fixer all in auto mode finished a run with no dialog left for a human.`, append:

```
 OpenCode 2.0.18 took part as a reviewer on MiMo V2.6 Pro, next to a Claude Code orchestrator and fixer, again with no dialog left for a human.
```

Use the versions and the model the run actually showed; `opencode --version` prints the first.

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -F - <<'EOF'
docs: OpenCode 2.0.18 checked as a reviewer in a real run

Claude-Session: https://claude.ai/code/session_01LL6CfYh2n6CN2EViZvzock
EOF
```

---

## Before the pull request

Per the owner's rules, the design and the plan must not appear in the PR diff. Remove them as in commit `2f1139e`:

```bash
git rm -r docs/superpowers && git commit -F - <<'EOF'
docs: drop the design and the plan before the pull request

Claude-Session: https://claude.ai/code/session_01LL6CfYh2n6CN2EViZvzock
EOF
```

`git rm` removes only the tracked files, the spec and this plan. The untracked files of earlier work under `docs/` stay as they are. Both documents stay in the branch's history. After that, follow superpowers:finishing-a-development-branch.
