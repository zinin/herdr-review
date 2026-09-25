import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from herdr_review import __version__, gitutil
from herdr_review.cli import build_parser, main, resolve_status_run_dir, scope_lines, status_run_spec
from herdr_review.launch import basename_slug, project_slug
from herdr_review.runner import RunnerError
from herdr_review.status import RunStatus


class CliSmokeTest(unittest.TestCase):
    def test_version_flag_prints_version(self):
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"herdr-review {__version__}")


class CliParsingTest(unittest.TestCase):
    def test_launch_flags(self):
        args = build_parser().parse_args(["launch", "--reviewers", "a,b", "--no-autodecide", "--layout", "grid", "--json"])
        self.assertEqual(args.reviewers, "a,b")
        self.assertIs(args.autodecide, False)
        self.assertEqual(args.layout, "grid")
        args = build_parser().parse_args(["launch"])
        self.assertIsNone(args.autodecide)

    def test_run_subcommands_parse(self):
        args = build_parser().parse_args(["run", "prompt", "hr1-codex", "--file", "/x", "--retry", "--run", "/r"])
        self.assertEqual((args.subcmd, args.name, args.file, args.retry, args.run), ("prompt", "hr1-codex", "/x", True, "/r"))
        args = build_parser().parse_args(["run", "finish", "--commits", "a,b"])
        self.assertEqual(args.commits, "a,b")
        args = build_parser().parse_args(["run", "notify", "--title", "t", "--sound", "request"])
        self.assertEqual(args.sound, "request")

    def test_run_autodecide_parses(self):
        args = build_parser().parse_args(["run", "autodecide", "--run", "/r"])
        self.assertEqual((args.subcmd, args.run), ("autodecide", "/r"))

    def test_status_prints_the_decision_mode(self):
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            st = RunStatus.create(run_dir, run_id="hrtest", repo=d, layout="tabs")
            st.set("autodecide", False)
            st.save()
            out = io.StringIO()
            with redirect_stdout(out):
                main(["status", "--run", str(run_dir)])
            self.assertIn("autodecide: off", out.getvalue())
            st.set("autodecide", True)
            st.set("autodecide_switched_at", "2026-09-10T16:33:00+00:00")
            st.save()
            out = io.StringIO()
            with redirect_stdout(out):
                main(["status", "--run", str(run_dir)])
            self.assertIn("autodecide: on", out.getvalue())
            self.assertIn("2026-09-10T16:33:00+00:00", out.getvalue())

    def test_status_says_the_working_tree_changed_not_who_changed_it(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunStatus.create(Path(d), run_id="hrtest", repo=d, layout="tabs")
            st.set("drift", True)
            st.save()
            out = io.StringIO()
            with redirect_stdout(out):
                main(["status", "--run", d])
            self.assertIn("drift: рабочее дерево изменилось во время ревью", out.getvalue())
            self.assertNotIn("ревьюер", out.getvalue())

    def test_status_accepts_positional_latest_and_run_flag(self):
        p = build_parser()
        self.assertEqual(status_run_spec(p.parse_args(["status", "latest"])), "latest")
        self.assertEqual(status_run_spec(p.parse_args(["status", "--run", "latest"])), "latest")
        self.assertEqual(status_run_spec(p.parse_args(["status", "--run", "/some/run"])), "/some/run")
        self.assertEqual(status_run_spec(p.parse_args(["status", "/some/run"])), "/some/run")
        self.assertIsNone(status_run_spec(p.parse_args(["status"])))
        self.assertEqual(status_run_spec(p.parse_args(["status", "latest", "--json"])), "latest")
        agreeing = p.parse_args(["status", "latest", "--run", "latest"])
        self.assertEqual(status_run_spec(agreeing), "latest")
        conflicting = p.parse_args(["status", "latest", "--run", "/flag"])
        with self.assertRaises(RunnerError):
            status_run_spec(conflicting)

    def test_resolve_status_run_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(resolve_status_run_dir("/some/run", {}, Path(d)), Path("/some/run"))
            self.assertEqual(resolve_status_run_dir(None, {"HERDR_REVIEW_RUN": "/env/run"}, Path(d)), Path("/env/run"))
            with self.assertRaises(RunnerError):
                resolve_status_run_dir("latest", {"HERDR_REVIEW_CONFIG": "/nonexistent"}, Path(d))

    def test_bad_poll_sec_is_runner_error(self):
        from herdr_review.cli import cmd_run
        args = build_parser().parse_args(["run", "wait", "--run", "/nope"])
        with self.assertRaises(RunnerError) as ctx:
            cmd_run(args, {"HERDR_REVIEW_POLL_SEC": "fast"})
        self.assertIn("HERDR_REVIEW_POLL_SEC", str(ctx.exception))

    def test_non_positive_poll_sec_is_runner_error(self):
        from herdr_review.cli import cmd_run
        args = build_parser().parse_args(["run", "wait", "--run", "/nope"])
        for raw in ("0", "-1", "nan"):
            with self.assertRaises(RunnerError) as ctx:
                cmd_run(args, {"HERDR_REVIEW_POLL_SEC": raw})
            self.assertIn("must be a positive number", str(ctx.exception))

    def test_launch_scope_flag(self):
        self.assertEqual(build_parser().parse_args(["launch", "--scope", "worktree"]).scope, "worktree")
        self.assertIsNone(build_parser().parse_args(["launch"]).scope)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["launch", "--scope", "everything"])

    def test_scope_lines(self):
        commits = {"scope": "commits", "base": "origin/master", "uncommitted": [" M a.txt", "?? notes/", "?? x"], "untracked": None}
        self.assertEqual(scope_lines(commits), [
            "  объём:        коммиты ветки (origin/master..HEAD)",
            "  вне ревью:    ваши незакоммиченные файлы (изменённых: 1, неотслеживаемых: 2); их никто не удалит и не закоммитит",
        ])
        self.assertEqual(scope_lines({**commits, "uncommitted": []}), ["  объём:        коммиты ветки (origin/master..HEAD)"])
        worktree = {"scope": "worktree", "base": "master", "uncommitted": ["?? new.py"], "untracked": {"files": 3, "skipped": 1}}
        self.assertEqual(scope_lines(worktree), [
            "  объём:        рабочее дерево — коммиты и незакоммиченное; неотслеживаемых файлов у ревьюеров: 3, из них пропущено: 1",
            "  фиксы:        останутся незакоммиченными — закоммитите их сами",
        ])
        self.assertEqual(scope_lines({**worktree, "uncommitted": [" M a.txt"], "untracked": {"files": 0, "skipped": 0}}), [
            "  объём:        рабочее дерево — коммиты и незакоммиченное",
            "  фиксы:        останутся незакоммиченными — закоммитите их сами",
        ])

    def test_close_parses(self):
        args = build_parser().parse_args(["close", "latest", "--force", "--json"])
        self.assertEqual((args.cmd, status_run_spec(args), args.force, args.json), ("close", "latest", True, True))
        args = build_parser().parse_args(["close"])
        self.assertEqual((status_run_spec(args), args.force), (None, False))

    def test_exclusive_parses(self):
        args = build_parser().parse_args(["exclusive", "--wait", "5", "--", "git", "commit", "--", "f"])
        self.assertEqual((args.cmd, args.wait, args.command), ("exclusive", 5.0, ["--", "git", "commit", "--", "f"]))

    def test_exclusive_timeout_parses(self):
        self.assertEqual(build_parser().parse_args(["exclusive", "--", "true"]).timeout, 1800.0)
        self.assertEqual(build_parser().parse_args(["exclusive", "--timeout", "90", "--", "true"]).timeout, 90.0)


class ResolveLatestTest(unittest.TestCase):
    """`latest` is per repository, and two checkouts named the same must not share it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runs = self.root / "runs"
        config = self.root / "config.yaml"
        config.write_text(
            "profiles:\n  codex: {kind: codex}\n"
            "presets:\n  default: {reviewers: [codex], orchestrator: codex, fixer: codex}\n"
            f"settings: {{runs_dir: {self.runs}}}\n",
            encoding="utf-8",
        )
        self.environ = {"HERDR_REVIEW_CONFIG": str(config)}

    def tearDown(self):
        self.tmp.cleanup()

    def checkout(self, client: str) -> Path:
        repo = self.root / client / "app"
        repo.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        return gitutil.repo_root(repo)

    def run_of(self, project: str, repo: Path) -> Path:
        run_dir = self.runs / project / "20260910-120000-hrtest"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text(json.dumps({"agents": {}, "repo": str(repo)}), encoding="utf-8")
        latest = self.runs / project / "latest"
        latest.symlink_to(run_dir.name)
        return run_dir.resolve()

    def test_latest_does_not_return_another_checkout_with_the_same_name(self):
        one, two = self.checkout("client-one"), self.checkout("client-two")
        run_two = self.run_of(project_slug(two), two)
        self.assertEqual(resolve_status_run_dir("latest", self.environ, two), run_two)
        with self.assertRaises(RunnerError) as ctx:
            resolve_status_run_dir("latest", self.environ, one)
        self.assertIn("no runs", str(ctx.exception))

    def test_a_run_under_the_bare_basename_is_still_found(self):
        one = self.checkout("client-one")
        run = self.run_of(basename_slug(one), one)          # the naming used before the digest
        self.assertEqual(resolve_status_run_dir("latest", self.environ, one), run)

    def test_a_bare_basename_run_of_another_repository_is_not_used(self):
        one, two = self.checkout("client-one"), self.checkout("client-two")
        self.run_of(basename_slug(one), two)
        with self.assertRaises(RunnerError) as ctx:
            resolve_status_run_dir("latest", self.environ, one)
        self.assertIn("no runs", str(ctx.exception))

    def test_latest_pointing_at_another_repository_is_refused(self):
        one, two = self.checkout("client-one"), self.checkout("client-two")
        self.run_of(project_slug(one), two)                 # the directory was moved or reused
        with self.assertRaises(RunnerError) as ctx:
            resolve_status_run_dir("latest", self.environ, one)
        self.assertIn("points at a run of", str(ctx.exception))
        self.assertIn(str(two), str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
