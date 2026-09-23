import os
import stat
import tempfile
import unittest
from pathlib import Path

import yaml

from herdr_review.config import (
    ConfigError,
    ConfigNotFound,
    config_path,
    is_secretish,
    load_config,
    parse_config,
    public_json,
)

VALID = {
    "profiles": {
        "claude-opus": {"kind": "claude", "args": ["--model", "opus", "--dangerously-skip-permissions"]},
        "codex": {"kind": "codex"},
        "glm": {"kind": "claude", "env": {"ANTHROPIC_AUTH_TOKEN": "${ZAI_TOKEN}"}, "args": ["--model", "glm-5"]},
    },
    "presets": {"default": {"reviewers": ["claude-opus", "codex"], "orchestrator": "claude-opus", "fixer": "claude-opus"}},
    "settings": {"layout": "grid", "autodecide": True, "checkin_sec": 60},
}


def errors_of(raw, environ=None):
    try:
        parse_config(raw, environ or {})
    except ConfigError as e:
        return e.errors
    return []


class ParseConfigTest(unittest.TestCase):
    def test_valid_config_parses(self):
        cfg = parse_config(VALID, {"ZAI_TOKEN": "secret"})
        self.assertEqual(cfg.profiles["claude-opus"].kind, "claude")
        self.assertEqual(cfg.profiles["codex"].args, [])
        self.assertEqual(cfg.profiles["glm"].env, {"ANTHROPIC_AUTH_TOKEN": "secret"})
        self.assertEqual(cfg.env_var_refs, ("ZAI_TOKEN",))
        self.assertEqual(cfg.presets["default"].reviewers, ["claude-opus", "codex"])
        self.assertEqual(cfg.settings.layout, "grid")
        self.assertTrue(cfg.settings.autodecide)
        self.assertEqual(cfg.settings.checkin_sec, 60)
        self.assertFalse(cfg.settings.close_agents_on_finish)
        self.assertEqual(cfg.settings.runs_dir, Path("~/.local/state/herdr-review/runs").expanduser())

    def test_unset_env_var_is_an_error(self):
        errs = errors_of(VALID, {})
        self.assertTrue(any("ZAI_TOKEN" in e for e in errs))

    def test_profile_name_rules(self):
        raw = {"profiles": {"Bad Name": {"kind": "x"}, "orch": {"kind": "x"}, "a" * 26: {"kind": "x"}}}
        errs = errors_of(raw)
        self.assertEqual(len([e for e in errs if "name" in e]), 3)

    def test_kind_required(self):
        errs = errors_of({"profiles": {"p": {"args": []}}})
        self.assertTrue(any("profiles.p.kind" in e for e in errs))

    def test_args_and_env_types(self):
        errs = errors_of({"profiles": {"p": {"kind": "x", "args": "notalist", "env": ["notamap"]}}})
        self.assertTrue(any("profiles.p.args" in e for e in errs))
        self.assertTrue(any("profiles.p.env" in e for e in errs))

    def test_bool_in_args_or_env_is_rejected(self):
        errs = errors_of({"profiles": {"p": {"kind": "x", "args": [True], "env": {"A": False}}}})
        self.assertTrue(any("profiles.p.args" in e for e in errs))
        self.assertTrue(any("profiles.p.env" in e for e in errs))

    def test_preset_references_must_exist(self):
        raw = {"profiles": {"p": {"kind": "x"}}, "presets": {"d": {"reviewers": ["nope"], "orchestrator": "p", "fixer": "zzz"}}}
        errs = errors_of(raw)
        self.assertTrue(any("presets.d.reviewers" in e and "nope" in e for e in errs))
        self.assertTrue(any("presets.d.fixer" in e and "zzz" in e for e in errs))

    def test_preset_reviewers_non_empty(self):
        raw = {"profiles": {"p": {"kind": "x"}}, "presets": {"d": {"reviewers": [], "orchestrator": "p", "fixer": "p"}}}
        self.assertTrue(any("presets.d.reviewers" in e for e in errors_of(raw)))

    def test_settings_validation(self):
        raw = {"profiles": {"p": {"kind": "x"}}, "settings": {"layout": "stack", "checkin_sec": 0, "autodecide": "yes", "unknown": 1}}
        errs = errors_of(raw)
        self.assertTrue(any("settings.layout" in e for e in errs))
        self.assertTrue(any("settings.checkin_sec" in e for e in errs))
        self.assertTrue(any("settings.autodecide" in e for e in errs))
        self.assertTrue(any("settings.unknown" in e for e in errs))

    def test_checkin_sec_rejects_values_above_one_day(self):
        raw = {"profiles": {"p": {"kind": "x"}}, "settings": {"checkin_sec": 30000000}}
        errs = errors_of(raw)
        self.assertTrue(any("settings.checkin_sec" in e and "between 1 and 86400" in e for e in errs))

    def test_profiles_required(self):
        self.assertTrue(any("profiles" in e for e in errors_of({})))
        self.assertTrue(any("mapping" in e for e in errors_of("just a string")))

    def test_public_json_hides_env_values(self):
        cfg = parse_config(VALID, {"ZAI_TOKEN": "secret"})
        pub = public_json(cfg)
        self.assertEqual(pub["profiles"]["glm"]["env_keys"], ["ANTHROPIC_AUTH_TOKEN"])
        self.assertNotIn("secret", str(pub))
        self.assertEqual(pub["presets"]["default"]["orchestrator"], "claude-opus")
        self.assertEqual(pub["settings"]["layout"], "grid")

    def test_scope_setting(self):
        self.assertEqual(parse_config({"profiles": {"p": {"kind": "x"}}}, {}).settings.scope, "auto")
        cfg = parse_config({"profiles": {"p": {"kind": "x"}}, "settings": {"scope": "worktree"}}, {})
        self.assertEqual(cfg.settings.scope, "worktree")
        self.assertEqual(public_json(cfg)["settings"]["scope"], "worktree")
        errs = errors_of({"profiles": {"p": {"kind": "x"}}, "settings": {"scope": "everything"}})
        self.assertTrue(any("settings.scope" in e and "auto, commits, worktree" in e for e in errs))


class IsSecretishTest(unittest.TestCase):
    def test_masks_a_long_value_or_a_secret_looking_name(self):
        self.assertTrue(is_secretish("ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic"))   # long
        self.assertTrue(is_secretish("ANTHROPIC_AUTH_TOKEN", "sk-1"))                          # name
        self.assertTrue(is_secretish("api_key", "x" * 4))
        self.assertTrue(is_secretish("MY_PASSWORD", "hunter2"))

    def test_leaves_a_short_plain_value_alone(self):
        self.assertFalse(is_secretish("ANTHROPIC_MODEL", "opus"))
        self.assertFalse(is_secretish("LANG", "C.UTF-8"))

    def test_an_empty_value_is_never_masked(self):
        self.assertFalse(is_secretish("ANTHROPIC_AUTH_TOKEN", ""))
        self.assertFalse(is_secretish("ANTHROPIC_MODEL", ""))


class LoadConfigTest(unittest.TestCase):
    def test_config_path_from_env_and_xdg(self):
        self.assertEqual(config_path({"HERDR_REVIEW_CONFIG": "/x/c.yaml"}), Path("/x/c.yaml"))
        self.assertEqual(config_path({"XDG_CONFIG_HOME": "/xdg", "HOME": "/h"}), Path("/xdg/herdr-review/config.yaml"))
        self.assertEqual(config_path({"HOME": "/h"}), Path("/h/.config/herdr-review/config.yaml"))

    def test_missing_file_raises_not_found_with_hint(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigNotFound) as ctx:
                load_config(Path(d) / "nope.yaml", {})
            self.assertIn("config.example.yaml", str(ctx.exception))

    def test_loads_file_and_warns_on_wide_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(yaml.safe_dump(VALID), encoding="utf-8")
            os.chmod(p, 0o644)
            cfg = load_config(p, {"ZAI_TOKEN": "s"})
            self.assertEqual(cfg.path, p)
            self.assertTrue(any("600" in w for w in cfg.warnings))
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
            self.assertEqual(load_config(p, {"ZAI_TOKEN": "s"}).warnings, [])

    def test_warns_about_an_env_value_that_will_not_be_masked(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(yaml.safe_dump({
                "profiles": {"glm": {"kind": "claude", "env": {"ANTHROPIC_MODEL": "opus", "ANTHROPIC_AUTH_TOKEN": "sk-1"}}},
            }), encoding="utf-8")
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
            warnings = load_config(p, {}).warnings
        self.assertEqual(len(warnings), 1)
        self.assertIn("ANTHROPIC_MODEL", warnings[0])
        self.assertIn("not masked in runner.log", warnings[0])
        self.assertNotIn("opus", warnings[0])          # the value never appears
        self.assertNotIn("sk-1", warnings[0])

    def test_yaml_error_is_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("profiles: [unclosed", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p, {})

    def test_yaml_error_does_not_carry_the_offending_source_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("profiles:\n  glm:\n    env:\n      TOKEN: sk-SECRET123: oops\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(p, {})
            self.assertNotIn("sk-SECRET123", str(ctx.exception))
            self.assertIn("YAML error", str(ctx.exception))

    def test_undecodable_file_is_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_bytes(b"\xff\xfe not utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(p, {})
            self.assertNotIsInstance(ctx.exception, ConfigNotFound)


if __name__ == "__main__":
    unittest.main()
