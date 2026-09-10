"""Load and validate ~/.config/herdr-review/config.yaml."""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,24}$")
RESERVED_PROFILE_NAMES = {"orch", "fixer"}
LAYOUTS = ("tabs", "grid")
DEFAULT_RUNS_DIR = "~/.local/state/herdr-review/runs"
ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SETTINGS_KEYS = ("layout", "autodecide", "close_agents_on_finish", "checkin_sec", "runs_dir")
SECRETISH_KEY_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL)", re.IGNORECASE)
MIN_MASKED_VALUE_LEN = 16


class ConfigError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


class ConfigNotFound(ConfigError):
    pass


@dataclass
class Profile:
    name: str
    kind: str
    args: list[str]
    env: dict[str, str]
    env_refs: tuple[str, ...] = ()      # ${VAR} names this profile's env refers to, before expansion


@dataclass
class Preset:
    name: str
    reviewers: list[str]
    orchestrator: str
    fixer: str


@dataclass
class Settings:
    layout: str = "tabs"
    autodecide: bool = False
    close_agents_on_finish: bool = False
    checkin_sec: int = 300
    runs_dir: Path = field(default_factory=lambda: Path(DEFAULT_RUNS_DIR).expanduser())


@dataclass
class Config:
    profiles: dict[str, Profile]
    presets: dict[str, Preset]
    settings: Settings
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    env_var_refs: tuple[str, ...] = ()


def is_secretish(key: str, value: str) -> bool:
    """Mask when the key looks like a secret OR the value is long. Never `and`: each condition
    alone would narrow the coverage; their disjunction only lets through the intersection —
    a non-secret-looking name with a short value, which is the case that ruins the log."""
    return bool(value) and (bool(SECRETISH_KEY_RE.search(key)) or len(value) >= MIN_MASKED_VALUE_LEN)


def unmasked_env_warnings(profiles: Mapping[str, Profile]) -> list[str]:
    """One line per env value that will travel unmasked into runner.log. Names the key, never it."""
    return [
        f"profiles.{p.name}: '{key}' is short and its name does not look like a secret,"
        " so its value is not masked in runner.log"
        for p in profiles.values()
        for key, value in p.env.items()
        if value and not is_secretish(key, value)
    ]


def config_path(environ: Mapping[str, str] = os.environ) -> Path:
    if environ.get("HERDR_REVIEW_CONFIG"):
        return Path(environ["HERDR_REVIEW_CONFIG"]).expanduser()
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(environ.get("HOME", "~"), ".config")
    return Path(base).expanduser() / "herdr-review" / "config.yaml"


def _is_scalar(value: object) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _expand_env(value: str, environ: Mapping[str, str], errors: list[str], where: str) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in environ:
            errors.append(f"{where}: environment variable ${{{name}}} is not set")
            return ""
        return environ[name]

    return ENV_VAR_RE.sub(sub, value)


def _refs_in(env: Mapping[str, object]) -> tuple[str, ...]:
    refs: list[str] = []
    for v in env.values():
        for name in ENV_VAR_RE.findall(str(v)):
            if name not in refs:
                refs.append(name)
    return tuple(refs)


def _parse_profiles(raw: object, environ: Mapping[str, str], errors: list[str]) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    if not isinstance(raw, dict) or not raw:
        errors.append("profiles: must be a non-empty mapping")
        return profiles
    for name, body in raw.items():
        where = f"profiles.{name}"
        if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
            errors.append(f"{where}: name must match ^[a-z][a-z0-9_-]{{0,24}}$")
            continue
        if name in RESERVED_PROFILE_NAMES:
            errors.append(f"{where}: name is reserved")
            continue
        if not isinstance(body, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        kind = body.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            errors.append(f"{where}.kind: required non-empty string (herdr agent kind)")
            kind = ""
        args = body.get("args", [])
        if not isinstance(args, list) or not all(_is_scalar(a) for a in args):
            errors.append(f"{where}.args: must be a list of strings")
            args = []
        env = body.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and _is_scalar(v) for k, v in env.items()):
            errors.append(f"{where}.env: must be a mapping of strings")
            env = {}
        refs = _refs_in(env)
        env = {k: _expand_env(str(v), environ, errors, f"{where}.env.{k}") for k, v in env.items()}
        profiles[name] = Profile(name=name, kind=kind.strip(), args=[str(a) for a in args], env=env, env_refs=refs)
    return profiles


def _parse_presets(raw: object, profiles: dict[str, Profile], errors: list[str]) -> dict[str, Preset]:
    presets: dict[str, Preset] = {}
    if raw is None:
        return presets
    if not isinstance(raw, dict):
        errors.append("presets: must be a mapping")
        return presets
    for name, body in raw.items():
        where = f"presets.{name}"
        if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
            errors.append(f"{where}: name must match ^[a-z][a-z0-9_-]{{0,24}}$ (quote the key if YAML read it as a boolean or a number)")
            continue
        if not isinstance(body, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        reviewers = body.get("reviewers")
        if not isinstance(reviewers, list) or not reviewers or not all(isinstance(r, str) for r in reviewers):
            errors.append(f"{where}.reviewers: must be a non-empty list of profile names")
            reviewers = []
        for r in reviewers:
            if r not in profiles:
                errors.append(f"{where}.reviewers: unknown profile '{r}'")
        roles: dict[str, str] = {}
        for role in ("orchestrator", "fixer"):
            v = body.get(role)
            if not isinstance(v, str) or not v:
                errors.append(f"{where}.{role}: required profile name")
                v = ""
            elif v not in profiles:
                errors.append(f"{where}.{role}: unknown profile '{v}'")
            roles[role] = v
        presets[name] = Preset(name=name, reviewers=list(reviewers), orchestrator=roles["orchestrator"], fixer=roles["fixer"])
    return presets


def _parse_settings(raw: object, errors: list[str]) -> Settings:
    s = Settings()
    if raw is None:
        return s
    if not isinstance(raw, dict):
        errors.append("settings: must be a mapping")
        return s
    for key in raw:
        if key not in SETTINGS_KEYS:
            errors.append(f"settings.{key}: unknown key (allowed: {', '.join(SETTINGS_KEYS)})")
    if "layout" in raw:
        if raw["layout"] in LAYOUTS:
            s.layout = raw["layout"]
        else:
            errors.append(f"settings.layout: must be one of {', '.join(LAYOUTS)}")
    for key in ("autodecide", "close_agents_on_finish"):
        if key in raw:
            if isinstance(raw[key], bool):
                setattr(s, key, raw[key])
            else:
                errors.append(f"settings.{key}: must be true or false")
    if "checkin_sec" in raw:
        v = raw["checkin_sec"]
        if isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 86400:
            s.checkin_sec = v
        else:
            errors.append("settings.checkin_sec: must be an integer between 1 and 86400")
    if "runs_dir" in raw:
        if isinstance(raw["runs_dir"], str) and raw["runs_dir"].strip():
            s.runs_dir = Path(raw["runs_dir"]).expanduser()
        else:
            errors.append("settings.runs_dir: must be a non-empty path")
    return s


def _env_var_refs(raw_profiles: object) -> tuple[str, ...]:
    refs: list[str] = []
    seen: set[str] = set()
    if not isinstance(raw_profiles, dict):
        return ()
    for body in raw_profiles.values():
        if not isinstance(body, dict):
            continue
        env = body.get("env", {})
        if not isinstance(env, dict):
            continue
        for v in env.values():
            for name in ENV_VAR_RE.findall(str(v)):
                if name not in seen:
                    seen.add(name)
                    refs.append(name)
    return tuple(refs)


def yaml_error_text(e: yaml.YAMLError) -> str:
    """The message without str(e)'s source excerpt: that excerpt can hold a credential."""
    problem = getattr(e, "problem", None)
    mark = getattr(e, "problem_mark", None)
    if problem and mark is not None:
        return f"{problem} (line {mark.line + 1}, column {mark.column + 1})"
    return type(e).__name__


def parse_config(raw: object, environ: Mapping[str, str] = os.environ) -> Config:
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError(["config must be a mapping with profiles, presets, settings"])
    profiles = _parse_profiles(raw.get("profiles"), environ, errors)
    presets = _parse_presets(raw.get("presets"), profiles, errors)
    settings = _parse_settings(raw.get("settings"), errors)
    if errors:
        raise ConfigError(errors)
    return Config(profiles=profiles, presets=presets, settings=settings, env_var_refs=_env_var_refs(raw.get("profiles")))


def load_config(path: Path | None = None, environ: Mapping[str, str] = os.environ) -> Config:
    path = Path(path) if path else config_path(environ)
    if not path.exists():
        raise ConfigNotFound([f"config not found: {path}. Copy config.example.yaml there, fill in profiles, chmod 600."])
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError([f"{path}: cannot read: {e}"]) from e
    except yaml.YAMLError as e:
        raise ConfigError([f"{path}: YAML error: {yaml_error_text(e)}"]) from e
    cfg = parse_config(raw, environ)
    cfg.path = path
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        cfg.warnings.append(f"{path}: permissions are wider than 600; env may hold tokens — run chmod 600")
    cfg.warnings.extend(unmasked_env_warnings(cfg.profiles))
    return cfg


def public_json(cfg: Config) -> dict:
    return {
        "config_path": str(cfg.path) if cfg.path else None,
        "profiles": {n: {"kind": p.kind, "args": p.args, "env_keys": sorted(p.env)} for n, p in cfg.profiles.items()},
        "presets": {n: {"reviewers": p.reviewers, "orchestrator": p.orchestrator, "fixer": p.fixer} for n, p in cfg.presets.items()},
        "settings": {
            "layout": cfg.settings.layout,
            "autodecide": cfg.settings.autodecide,
            "close_agents_on_finish": cfg.settings.close_agents_on_finish,
            "checkin_sec": cfg.settings.checkin_sec,
            "runs_dir": str(cfg.settings.runs_dir),
        },
        "warnings": cfg.warnings,
    }
