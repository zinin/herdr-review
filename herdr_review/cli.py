"""Command-line interface for herdr-review."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Mapping

from . import PACKAGE_ROOT, __version__, gitutil
from .config import SCOPES, ConfigError, load_config, public_json
from .herdr import Herdr
from .launch import LaunchError, LaunchOptions, basename_slug, launch, project_slug
from .render import RenderError
from .runner import Runner, RunnerError
from .scope import uncommitted_counts
from .status import RunStatus, StatusError

RUNNER_PATH = PACKAGE_ROOT / "bin" / "herdr-review"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr-review", description="Multi-agent code review inside herdr")
    parser.add_argument("--version", action="version", version=f"herdr-review {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("profiles", help="print the validated config (profiles without secrets)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("launch", help="start a review run: orchestrator tab + agent")
    p.add_argument("--preset")
    p.add_argument("--reviewers", help="comma-separated profile names")
    p.add_argument("--orchestrator")
    p.add_argument("--fixer")
    p.add_argument("--base", help="base branch or ref (default: origin/HEAD, master, main)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--autodecide", dest="autodecide", action="store_true", default=None)
    g.add_argument("--no-autodecide", dest="autodecide", action="store_false")
    p.add_argument("--layout", choices=("tabs", "grid"))
    p.add_argument("--description", help="what was implemented (goes into the review prompt)")
    p.add_argument("--plan", help="path to the plan / requirements document")
    p.add_argument("--scope", choices=SCOPES,
                   help="the change under review: auto (the branch's commits, else the working tree), commits, worktree")
    p.add_argument("--focus", action="store_true", help="switch to the orchestrator tab")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="show a run's status")
    p.add_argument("run_pos", nargs="?", metavar="DIR", help="run directory or 'latest'")
    p.add_argument("--run", help="run directory or 'latest' (default: $HERDR_REVIEW_RUN, else latest)")
    p.add_argument("--json", action="store_true")

    r = sub.add_parser("run", help="runner subcommands used by the orchestrator agent")
    rs = r.add_subparsers(dest="subcmd", required=True)

    def runsub(name: str, help_: str) -> argparse.ArgumentParser:
        sp = rs.add_parser(name, help=help_)
        sp.add_argument("--run", help="run directory (default: $HERDR_REVIEW_RUN)")
        return sp

    runsub("start-reviewers", "create tabs/panes and start every reviewer")
    sp = runsub("wait", "wait for a state change, a blocked agent, or the check-in interval")
    sp.add_argument("--agent")
    sp = runsub("prompt", "send an agent its prompt (its review prompt, or --file)")
    sp.add_argument("name")
    sp.add_argument("--file")
    sp.add_argument("--retry", action="store_true")
    sp = runsub("fail", "take an agent out of the run")
    sp.add_argument("name")
    sp.add_argument("--reason", required=True)
    runsub("autodecide", "switch the run to automatic decisions (one-way)")
    runsub("collect", "validate review files, re-prompt once, detect drift")
    runsub("start-fixer", "create the fixer's tab/pane and start it")
    sp = runsub("notify", "show a herdr notification")
    sp.add_argument("--title", required=True)
    sp.add_argument("--body")
    sp.add_argument("--sound", choices=("none", "done", "request"))
    sp = runsub("phase", "record the current phase")
    sp.add_argument("name")
    sp = runsub("finish", "mark the run finished")
    sp.add_argument("--commits", default="", help="comma-separated commit hashes")
    return parser


def status_run_spec(args: argparse.Namespace) -> str | None:
    flag = getattr(args, "run", None)
    positional = getattr(args, "run_pos", None)
    if flag is not None and positional is not None and flag != positional:
        raise RunnerError("pass the run either positionally or with --run, not both")
    return flag if flag is not None else positional


def recorded_repo(run_dir: Path) -> str | None:
    """The repository a run belongs to, as its own status.json records it."""
    try:
        data = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    repo = data.get("repo") if isinstance(data, dict) else None
    return repo if isinstance(repo, str) and repo else None


def resolve_latest(latest: Path, repo: Path) -> Path:
    """Follow a `latest` symlink, refusing a run that belongs to another repository."""
    run_dir = latest.resolve()
    recorded = recorded_repo(run_dir)
    if recorded is not None and Path(recorded) != Path(repo):
        raise RunnerError(f"{latest} points at a run of {recorded}, not {repo}")
    return run_dir


def resolve_status_run_dir(arg: str | None, environ: Mapping[str, str], cwd: Path) -> Path:
    if arg and arg != "latest":
        return Path(arg).expanduser()
    if not arg and environ.get("HERDR_REVIEW_RUN"):
        return Path(environ["HERDR_REVIEW_RUN"])
    try:
        cfg = load_config(environ=environ)
        repo = gitutil.repo_root(cwd)
    except (ConfigError, gitutil.GitError) as e:
        raise RunnerError(str(e)) from e
    runs_dir = (Path(cwd) / cfg.settings.runs_dir).resolve()
    latest = runs_dir / project_slug(repo) / "latest"
    if latest.exists():
        return resolve_latest(latest, repo)
    # Runs made before project directories carried the path digest live under the bare basename.
    legacy = runs_dir / basename_slug(repo) / "latest"
    if legacy.exists():
        recorded = recorded_repo(legacy.resolve())
        if recorded is not None and Path(recorded) == Path(repo):
            return resolve_latest(legacy, repo)
    raise RunnerError(f"no runs for this repository under {latest.parent}")


def _run_dir_arg(args: argparse.Namespace, environ: Mapping[str, str]) -> Path:
    value = getattr(args, "run", None) or environ.get("HERDR_REVIEW_RUN")
    if not value:
        raise RunnerError("no run directory: pass --run DIR or set HERDR_REVIEW_RUN")
    return Path(value).expanduser()


def cmd_profiles(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    pub = public_json(load_config(environ=environ))
    if args.json:
        print(json.dumps(pub, ensure_ascii=False, indent=2))
        return 0
    print(f"config: {pub['config_path']}")
    print("profiles:")
    for name, p in pub["profiles"].items():
        env = f" env={','.join(p['env_keys'])}" if p["env_keys"] else ""
        print(f"  {name:<26} {p['kind']:<10} {' '.join(p['args'])}{env}")
    print("presets:")
    for name, p in pub["presets"].items():
        print(f"  {name:<12} reviewers={','.join(p['reviewers'])} orchestrator={p['orchestrator']} fixer={p['fixer']}")
    print("settings: " + " ".join(f"{k}={v}" for k, v in pub["settings"].items()))
    for w in pub["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def scope_lines(result: dict) -> list[str]:
    """The launch summary's lines about what the reviewers review."""
    if result["scope"] == "commits":
        lines = [f"  объём:        коммиты ветки ({result['base']}..HEAD)"]
        if result["uncommitted"]:
            changed, untracked = uncommitted_counts(result["uncommitted"])
            lines.append(f"  вне ревью:    ваши незакоммиченные файлы (изменённых: {changed}, неотслеживаемых: {untracked}); их никто не тронет")
        return lines
    line = "  объём:        рабочее дерево — коммиты и незакоммиченное"
    untracked = result.get("untracked") or {}
    if untracked.get("files"):
        line += f"; неотслеживаемых файлов у ревьюеров: {untracked['files']}"
        if untracked.get("skipped"):
            line += f", из них пропущено: {untracked['skipped']}"
    return [line]


def cmd_launch(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    cfg = load_config(environ=environ)
    reviewers = [x.strip() for x in args.reviewers.split(",") if x.strip()] if args.reviewers else None
    opts = LaunchOptions(
        preset=args.preset, reviewers=reviewers, orchestrator=args.orchestrator, fixer=args.fixer, base=args.base,
        autodecide=args.autodecide, layout=args.layout, description=args.description, plan=args.plan, focus=args.focus,
        scope=args.scope,
    )
    result = launch(opts, cfg, Herdr(), environ, Path.cwd(), RUNNER_PATH)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"Запущен прогон {result['run_id']}")
    print(f"  run dir:      {result['run_dir']}")
    print(f"  оркестратор:  {result['orchestrator']} (вкладка {result['tab']})")
    print(f"  ревьюеры:     {', '.join(result['reviewers'])}")
    if result["skipped"]:
        print(f"  пропущены:    {', '.join(result['skipped'])}")
    print(f"  база:         {result['base']} ({result['merge_base'][:12]})")
    for line in scope_lines(result):
        print(line)
    print(f"  autodecide:   {'on' if result['autodecide'] else 'off'}; layout: {result['layout']}")
    print(f"  смотреть:     {result['hints']['focus']}")
    print(f"  статус:       {result['hints']['status']}")
    for w in result["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    run_dir = resolve_status_run_dir(status_run_spec(args), environ, Path.cwd())
    try:
        st = RunStatus.load(run_dir)
    except StatusError as e:
        raise RunnerError(str(e)) from e
    data = st.data
    if args.json:
        out = copy.deepcopy(data)
        out["run_dir"] = str(run_dir)
        for n in data.get("agents", {}):
            out["agents"][n]["since_sec"] = st.since_sec(n)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"run {data.get('run_id')}  phase: {data.get('phase')}  branch: {data.get('branch')} → {data.get('base')}")
    print(f"dir: {run_dir}")
    if data.get("autodecide"):
        when = data.get("autodecide_switched_at")
        print("autodecide: on" + (f" (переключён пользователем: {when})" if when else ""))
    else:
        print("autodecide: off")
    if data.get("waiting_for_user"):
        print("ожидает ответа пользователя в панели оркестратора")
    if data.get("drift"):
        print("drift: ревьюер изменил рабочее дерево")
    print(f"{'agent':<28} {'role':<9} {'state':<15} {'since':>6}  file  reason")
    for n, a in data["agents"].items():
        reason = (a.get("reason") or "")[:60]
        file_ok = "yes" if a.get("result_ok") else "-"
        print(f"{n:<28} {a.get('role', ''):<9} {a['state']:<15} {st.since_sec(n):>5}s  {file_ok:<4}  {reason}")
    if data.get("commits"):
        print("commits: " + ", ".join(data["commits"]))
    return 0


def autodecide_now(runner: Runner) -> bool:
    """Fresh from disk: a `run wait` can hold its in-memory snapshot for a whole check-in window."""
    try:
        data = json.loads((runner.run_dir / "status.json").read_text(encoding="utf-8"))
        return bool(data.get("autodecide"))
    except (OSError, ValueError, AttributeError):
        return bool(runner.status.data.get("autodecide"))


def cmd_run(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    raw_poll = environ.get("HERDR_REVIEW_POLL_SEC", "5")
    try:
        poll = float(raw_poll)
    except ValueError:
        raise RunnerError(f"HERDR_REVIEW_POLL_SEC={raw_poll!r} is not a number") from None
    if not poll > 0:
        raise RunnerError(f"HERDR_REVIEW_POLL_SEC={raw_poll!r} must be a positive number")
    runner = Runner(_run_dir_arg(args, environ), poll_sec=poll)
    sub = args.subcmd
    if sub == "start-reviewers":
        result = runner.start_reviewers()
    elif sub == "wait":
        result = runner.wait(agent=args.agent)
    elif sub == "prompt":
        result = runner.prompt(args.name, file=args.file, retry=args.retry)
    elif sub == "fail":
        result = runner.fail(args.name, args.reason)
    elif sub == "autodecide":
        result = runner.autodecide()
    elif sub == "collect":
        result = runner.collect()
    elif sub == "start-fixer":
        result = runner.start_fixer()
    elif sub == "notify":
        result = runner.notify(args.title, body=args.body, sound=args.sound)
    elif sub == "phase":
        result = runner.phase(args.name)
    elif sub == "finish":
        result = runner.finish(args.commits.split(","))
    else:
        raise RunnerError(f"unknown run subcommand {sub!r}")
    if "autodecide" not in result:
        result["autodecide"] = autodecide_now(runner)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def dispatch(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    if args.cmd is None:
        build_parser().print_help()
        return 2
    if args.cmd == "profiles":
        return cmd_profiles(args, environ)
    if args.cmd == "launch":
        return cmd_launch(args, environ)
    if args.cmd == "status":
        return cmd_status(args, environ)
    if args.cmd == "run":
        return cmd_run(args, environ)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return dispatch(args, os.environ)
    except ConfigError as e:
        print("config error:\n" + "\n".join(e.errors), file=sys.stderr)
        return 1
    except (LaunchError, RunnerError, gitutil.GitError, RenderError) as e:
        print(str(e), file=sys.stderr)
        return 1
    except (OSError, KeyError) as e:
        print(f"herdr-review: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
