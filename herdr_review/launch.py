"""herdr-review launch: preflight, run directory, orchestrator tab and agent."""
from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from . import PROMPTS_DIR, __version__, gitutil
from .config import Config, is_secretish
from .dialogs import try_resolve_startup_dialog
from .herdr import Herdr, HerdrResult
from .render import render_file
from .status import RunStatus

RUN_ID_ALPHABET = string.ascii_lowercase + string.digits
TERMINAL_EXTRA_ORCH = " Do not stop until the run is finished."
ORCH_PROMPT_TIMEOUT_MS = 60000


class LaunchError(Exception):
    pass


@dataclass
class LaunchOptions:
    preset: str | None = None
    reviewers: list[str] | None = None
    orchestrator: str | None = None
    fixer: str | None = None
    base: str | None = None
    autodecide: bool | None = None
    layout: str | None = None
    description: str | None = None
    plan: str | None = None
    focus: bool = False


def new_run_id(rng: random.Random | None = None) -> str:
    rng = rng or random.SystemRandom()
    return "hr" + "".join(rng.choice(RUN_ID_ALPHABET) for _ in range(4))


def basename_slug(repo: Path) -> str:
    """The readable half of a project directory name."""
    return re.sub(r"[^a-z0-9_-]", "-", Path(repo).name.lower()) or "repo"


def project_slug(repo: Path) -> str:
    """Readable, but unique per repository path: two checkouts named `app` must not share a run
    directory or a `latest` symlink."""
    digest = hashlib.sha256(str(Path(repo).resolve()).encode("utf-8")).hexdigest()[:6]
    return f"{basename_slug(repo)}-{digest}"


def resolve_selection(cfg: Config, opts: LaunchOptions) -> tuple[list[str], str, str]:
    preset = None
    if opts.preset:
        preset = cfg.presets.get(opts.preset)
        if preset is None:
            raise LaunchError(f"preset '{opts.preset}' not found in config (have: {', '.join(cfg.presets) or 'none'})")
    elif "default" in cfg.presets:
        preset = cfg.presets["default"]
    reviewers = list(opts.reviewers) if opts.reviewers is not None else (list(preset.reviewers) if preset else [])
    orchestrator = opts.orchestrator or (preset.orchestrator if preset else "")
    fixer = opts.fixer or (preset.fixer if preset else "")
    if not reviewers:
        raise LaunchError("no reviewers selected: pass --reviewers or define presets.default")
    if not orchestrator:
        raise LaunchError("no orchestrator selected: pass --orchestrator or define presets.default")
    if not fixer:
        raise LaunchError("no fixer selected: pass --fixer or define presets.default")
    if len(set(reviewers)) != len(reviewers):
        raise LaunchError("duplicate reviewer profiles: " + ", ".join(reviewers))
    for name in [*reviewers, orchestrator, fixer]:
        if name not in cfg.profiles:
            raise LaunchError(f"unknown profile '{name}' (have: {', '.join(cfg.profiles)})")
    return reviewers, orchestrator, fixer


def _reviewers_table(reviewers: list[dict], run_dir: Path) -> str:
    return "\n".join(
        f"  - `{rv['name']}` — profile `{rv['profile']}` ({rv['kind']}); prompt `{run_dir}/prompts/{rv['profile']}.md`; result `{run_dir}/reviews/{rv['profile']}.md`"
        for rv in reviewers
    )


def _unfinished_runs(project_dir: Path) -> list[str]:
    found = []
    if not project_dir.is_dir():
        return found
    for d in sorted(project_dir.iterdir()):
        st = d / "status.json"
        if d.is_symlink() or not st.exists():
            continue
        try:
            phase = json.loads(st.read_text(encoding="utf-8")).get("phase")
        except (OSError, json.JSONDecodeError):
            continue
        if phase not in ("finished", "aborted"):
            found.append(f"{d.name} ({phase})")
    return found


def _profile_spec(cfg: Config, profile: str, name: str) -> dict:
    p = cfg.profiles[profile]
    return {"name": name, "profile": profile, "kind": p.kind, "args": list(p.args), "env_keys": sorted(p.env)}


def launch(
    opts: LaunchOptions,
    cfg: Config,
    herdr: Herdr,
    environ: Mapping[str, str],
    cwd: Path,
    runner_path: Path,
    which: Callable[[str], str | None] = shutil.which,
    run_id: str | None = None,
    now: Callable[[], float] = time.time,
) -> dict:
    warnings = list(cfg.warnings)

    # ----- preflight
    if environ.get("HERDR_ENV") != "1":
        raise LaunchError("not inside herdr: HERDR_ENV is not 1")
    workspace_id = environ.get("HERDR_WORKSPACE_ID")
    if not workspace_id:
        raise LaunchError("HERDR_WORKSPACE_ID is not set; run from a herdr-managed pane")
    if not herdr.status_ok():
        raise LaunchError("herdr server is not running (check `herdr status`)")
    try:
        repo = gitutil.repo_root(cwd)
    except gitutil.GitError as e:
        raise LaunchError(str(e)) from e
    reviewers, orch_profile, fixer_profile = resolve_selection(cfg, opts)
    if not Path(runner_path).is_file():
        raise LaunchError(f"runner not found: {runner_path}")
    for role, pname in (("orchestrator", orch_profile), ("fixer", fixer_profile)):
        kind = cfg.profiles[pname].kind
        if not which(kind):
            raise LaunchError(f"{role} profile '{pname}': executable '{kind}' not found in PATH")
    usable = []
    for pname in reviewers:
        kind = cfg.profiles[pname].kind
        if which(kind):
            usable.append(pname)
        else:
            warnings.append(f"reviewer '{pname}' skipped: executable '{kind}' not found in PATH")
    if not usable:
        raise LaunchError("no usable reviewers: none of the selected profiles has its executable in PATH")
    try:
        base = opts.base or gitutil.detect_base(repo)
        mb = gitutil.merge_base(repo, base)
    except gitutil.GitError as e:
        raise LaunchError(str(e)) from e
    if not gitutil.has_changes(repo, mb):
        raise LaunchError(f"nothing to review: the working tree equals {base} ({mb[:12]})")
    if gitutil.status_short(repo).strip():
        warnings.append("working tree has uncommitted changes; yolo reviewers share this tree and can modify them")
    layout = opts.layout or cfg.settings.layout
    autodecide = cfg.settings.autodecide if opts.autodecide is None else opts.autodecide
    project = project_slug(repo)
    # The orchestrator tab starts in the repository root, so every path it is handed must be absolute.
    runs_dir = (Path(cwd) / cfg.settings.runs_dir).resolve()
    project_dir = runs_dir / project
    for stale in _unfinished_runs(project_dir):
        warnings.append(f"another run of this repository is not finished: {stale}")

    # ----- run directory
    run_id = run_id or new_run_id()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    run_dir = project_dir / f"{stamp}-{run_id}"
    try:
        run_dir.mkdir(parents=True, mode=0o700)
        run_dir.chmod(0o700)
        (run_dir / "prompts").mkdir()
        (run_dir / "reviews").mkdir()
    except OSError as e:
        raise LaunchError(f"cannot create the run directory {run_dir}: {e}") from e
    reviewers_spec = [_profile_spec(cfg, p, f"{run_id}-{p}") for p in usable]
    orch = _profile_spec(cfg, orch_profile, f"{run_id}-orch")
    fixer = _profile_spec(cfg, fixer_profile, f"{run_id}-fixer")
    description = (opts.description or "").strip() or "(not provided)"
    plan_ref = (opts.plan or "").strip() or "(not provided)"
    branch = gitutil.current_branch(repo)
    run_json = {
        "version": __version__,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "repo": str(repo),
        "project": project,
        "branch": branch,
        "base": base,
        "merge_base": mb,
        "description": description,
        "plan": plan_ref,
        "autodecide": autodecide,
        "layout": layout,
        "checkin_sec": cfg.settings.checkin_sec,
        "close_agents_on_finish": cfg.settings.close_agents_on_finish,
        "workspace_id": workspace_id,
        "reviewers": reviewers_spec,
        "orchestrator": orch,
        "fixer": fixer,
        "runner": str(runner_path),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now())),
    }
    (run_dir / "run.json").write_text(json.dumps(run_json, indent=2, ensure_ascii=False), encoding="utf-8")

    # ----- prompts
    for rv in reviewers_spec:
        text = render_file(PROMPTS_DIR / "reviewer.md", {
            "DESCRIPTION": description,
            "PLAN_REFERENCE": plan_ref,
            "REPO": str(repo),
            "BASE_REF": base,
            "MERGE_BASE": mb,
            "RESULT_PATH": str(run_dir / "reviews" / f"{rv['profile']}.md"),
            "REVIEWER": rv["profile"],
        })
        (run_dir / "prompts" / f"{rv['profile']}.md").write_text(text, encoding="utf-8")
    orch_text = render_file(PROMPTS_DIR / "orchestrator.md", {
        "RUN_DIR": str(run_dir),
        "RUNNER": str(runner_path),
        "RUN_ID": run_id,
        "REPO": str(repo),
        "BRANCH": branch,
        "BASE_REF": base,
        "MERGE_BASE": mb,
        "REVIEWERS": _reviewers_table(reviewers_spec, run_dir),
        "ORCH_NAME": orch["name"],
        "FIXER_NAME": fixer["name"],
        "FIXER_PROFILE": fixer_profile,
        "AUTODECIDE": "true" if autodecide else "false",
        "LAYOUT": layout,
        "CHECKIN_SEC": cfg.settings.checkin_sec,
        "DESCRIPTION": description,
        "PLAN_REFERENCE": plan_ref,
        "FIXER_AUTO_SKELETON": render_file(PROMPTS_DIR / "fixer-auto.md", {"RUN_DIR": str(run_dir)}),
        "FIXER_DECISION_SKELETON": render_file(PROMPTS_DIR / "fixer-decision.md", {"RUN_DIR": str(run_dir)}),
    })
    (run_dir / "orchestrator.md").write_text(orch_text, encoding="utf-8")
    status = RunStatus.create(run_dir, run_id=run_id, repo=str(repo), branch=branch, base=base, merge_base=mb, autodecide=autodecide, layout=layout)

    # ----- herdr: log into runner.log from here on, mask profile secrets
    log_path = run_dir / "runner.log"

    def log(line: str) -> None:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} launch: {line}\n")

    log(f"run {run_id} created in {run_dir}; reviewers: {', '.join(usable)}")
    herdr.log = log
    secrets = [v for p in cfg.profiles.values() for k, v in p.env.items() if is_secretish(k, v)]
    for name in cfg.env_var_refs:
        raw = environ.get(name)
        if raw and is_secretish(name, raw):     # the same secrets before ${VAR} expansion
            secrets.append(raw)
    herdr.mask_values = secrets
    env_all = {"HERDR_REVIEW_RUN": str(run_dir)}
    for key in ("HERDR_REVIEW_CONFIG", "XDG_CONFIG_HOME"):
        if key in environ:
            env_all[key] = environ[key]
    selected = [*usable, orch_profile, fixer_profile]
    for name in dict.fromkeys(ref for prof in selected for ref in cfg.profiles[prof].env_refs):
        if name in environ:
            env_all[name] = environ[name]
    env_all.update(cfg.profiles[orch_profile].env)
    try:
        r = herdr.tab_create(workspace_id, str(repo), f"rv-{run_id}: orch", env_all, focus=False)
        if not r.ok or not r.result:
            raise LaunchError(f"herdr tab create failed: {r.error_code}: {r.message}\nRun directory: {run_dir}")
        try:
            tab_id = r.result["tab"]["tab_id"]
            pane_id = r.result["root_pane"]["pane_id"]
        except (KeyError, TypeError) as e:
            raise LaunchError(f"unexpected `tab create` response: {r.text[:300]}\nRun directory: {run_dir}") from e
        status.set("orchestrator", {"name": orch["name"], "profile": orch_profile, "kind": orch["kind"], "tab": tab_id, "pane": pane_id})
        status.save()

        r = herdr.agent_start(orch["name"], orch["kind"], pane_id, orch["args"])
        if not r.ok and r.error_code == "agent_not_ready" and try_resolve_startup_dialog(herdr, orch["name"]):
            log("orchestrator startup dialog resolved automatically")
            r = HerdrResult(True, 0)
        if not r.ok:
            screen = herdr.pane_read(pane_id) or ""
            raise LaunchError(
                f"orchestrator '{orch_profile}' failed to start: {r.error_code}: {r.message}\n"
                f"Tab {tab_id} is left open for inspection. Last screen:\n{screen[-2000:]}\n"
                f"Run directory: {run_dir}"
            )
        text = render_file(PROMPTS_DIR / "terminal.md", {"FILE": str(run_dir / "orchestrator.md")}).strip() + TERMINAL_EXTRA_ORCH
        r = herdr.agent_prompt(orch["name"], text, until="working", timeout_ms=ORCH_PROMPT_TIMEOUT_MS)
        if not r.ok and r.error_code in ("agent_prompt_stalled", "timeout"):
            log("orchestrator prompt stalled, retrying once")
            r = herdr.agent_prompt(orch["name"], text, until="working", timeout_ms=ORCH_PROMPT_TIMEOUT_MS)
        if not r.ok:
            raise LaunchError(
                f"orchestrator did not start working: {r.error_code}: {r.message}\n"
                f"Re-prompt by hand:\n  herdr agent prompt {orch['name']} \"{text}\"\n"
                f"Run directory: {run_dir}"
            )
    except LaunchError as e:
        # The directory stays for inspection, but the run must not be reported as unfinished forever.
        status.set("abort_reason", str(e))
        status.set_phase("aborted")
        raise
    latest = project_dir / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(run_dir.name)
    except OSError as e:
        warnings.append(f"cannot update {latest}: {e}")
        log(f"cannot update {latest}: {e}")
    status.set_phase("reviewing")
    if opts.focus:
        herdr.tab_focus(tab_id)
    return {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "orchestrator": orch["name"],
        "tab": tab_id,
        "pane": pane_id,
        "reviewers": [rv["profile"] for rv in reviewers_spec],
        "skipped": [p for p in reviewers if p not in usable],
        "base": base,
        "merge_base": mb,
        "autodecide": autodecide,
        "layout": layout,
        "warnings": warnings,
        "hints": {"focus": f"herdr agent focus {orch['name']}", "status": f"{runner_path} status latest"},
    }
