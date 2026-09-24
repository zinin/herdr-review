import unittest

from herdr_review import PROMPTS_DIR
from herdr_review.render import placeholders, render_file
from herdr_review.runner import DRIFT_GONE
from herdr_review.scope import fixer_skeleton

NO_COMMIT = "Commit nothing. The change under review is uncommitted work"
EXPECTED = {
    "terminal.md": {"FILE"},
    "reviewer.md": {"DESCRIPTION", "PLAN_REFERENCE", "REPO", "BASE_REF", "MERGE_BASE", "RESULT_PATH", "REVIEWER", "SCOPE_STEPS", "SCRATCH_DIR"},
    "fixer-auto.md": {"RUN_DIR", "COMMIT_RULES"},
    "fixer-decision.md": {"RUN_DIR", "COMMIT_RULES"},
    "fixer-commit-auto.md": {"RUN_DIR"},
    "fixer-commit-decision.md": {"RUN_DIR"},
    "fixer-commit-none.md": set(),
    "scope-commits.md": {"MERGE_BASE", "UNCOMMITTED"},
    "scope-worktree.md": {"MERGE_BASE", "UNTRACKED"},
    "orchestrator.md": {
        "RUN_DIR", "RUNNER", "RUN_ID", "REPO", "BRANCH", "BASE_REF", "MERGE_BASE", "REVIEWERS", "ORCH_NAME",
        "FIXER_NAME", "FIXER_PROFILE", "AUTODECIDE", "LAYOUT", "CHECKIN_SEC", "DESCRIPTION", "PLAN_REFERENCE",
        "FIXER_AUTO_SKELETON", "FIXER_DECISION_SKELETON", "START_HEAD", "SCOPE", "UNCOMMITTED_COUNT",
    },
}


class PromptTemplatesTest(unittest.TestCase):
    def test_placeholder_sets(self):
        for name, expected in EXPECTED.items():
            with self.subTest(name=name):
                text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
                self.assertEqual(placeholders(text), expected)

    def test_terminal_line(self):
        self.assertEqual(render_file(PROMPTS_DIR / "terminal.md", {"FILE": "/r/x.md"}).strip(), "Read /r/x.md and follow it exactly.")

    def test_reviewer_prompt_has_required_headings_and_rules(self):
        values = {k: "v" for k in EXPECTED["reviewer.md"]}
        values["SCOPE_STEPS"] = "1. step one\n2. step two"
        values["SCRATCH_DIR"] = "/run/scratch/codex"
        text = render_file(PROMPTS_DIR / "reviewer.md", values)
        for heading in ("### Strengths", "### Critical Issues", "### Important Issues", "### Minor Issues", "### Assessment"):
            self.assertIn(heading, text)
        self.assertIn("1. step one\n2. step two\n\n3. Read the modified files", text)
        self.assertIn("goes under `/run/scratch/codex`", text)
        self.assertIn("not into /tmp", text)
        self.assertIn("tracked or untracked", text)
        self.assertIn("DONE", text)
        self.assertIn("gitignored", text)
        self.assertNotIn("git ls-files --others", text)
        self.assertIn("Apart from your review file", text)

    def test_fixer_skeletons_mention_report_and_done(self):
        for kind in ("auto", "decision"):
            text = fixer_skeleton(kind, "commits", "/run")
            self.assertIn("/run/", text)
            self.assertIn("DONE", text)
            self.assertIn("Do not push", text)

    def test_in_the_worktree_scope_the_fixer_commits_nothing(self):
        for kind in ("auto", "decision"):
            with self.subTest(kind=kind):
                worktree = fixer_skeleton(kind, "worktree", "/run")
                self.assertIn(NO_COMMIT, worktree)
                self.assertIn("applied, not committed: the review covers uncommitted work", worktree)
                self.assertNotIn("git commit", worktree)
                self.assertNotIn("git add <file>", worktree)
                self.assertIn(f"/run/fix-{'auto' if kind == 'auto' else '<ORCHESTRATOR: n>'}-report.md", worktree)
                self.assertNotIn("{", worktree)
                commits = fixer_skeleton(kind, "commits", "/run")
                self.assertNotIn(NO_COMMIT, commits)
                self.assertNotIn("the review covers uncommitted work", commits)
                self.assertIn("git commit --only -F /run/fix-", commits)
                self.assertNotIn("{", commits)

    def test_orchestrator_prompt_renders_and_names_every_subcommand(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["RUNNER"] = "/opt/hr/bin/herdr-review"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for sub in ("run start-reviewers", "run wait", "run collect", "run prompt", "run fail", "run start-fixer", "run notify", "run phase", "run finish", "run autodecide"):
            self.assertIn(sub, text)
        self.assertIn('"/opt/hr/bin/herdr-review" run wait', text)
        self.assertNotIn("{", text.replace("{\"", ""))  # no leftover placeholders except JSON examples
        for phrase in ("Phase 1", "Phase 6", "Red flags", "AUTO", "DISPUTED", "DISMISSED", "Проверка решения"):
            self.assertIn(phrase, text)
        self.assertIn("30 s", text)
        self.assertNotIn("within 5 s", text)


    def test_orchestrator_prompt_documents_the_mid_run_autodecide_switch(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["RUNNER"] = "/opt/hr/bin/herdr-review"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        self.assertIn('"/opt/hr/bin/herdr-review" run autodecide', text)
        self.assertIn("«авто»", text)                 # the answer that flips the mode
        self.assertIn("initial autodecide", text)     # the run facts no longer claim to be current

    def test_fixer_skeletons_protect_the_users_files_and_follow_the_repository_style(self):
        for name, message in (("fixer-auto.md", "/run/fix-auto-commit.txt"), ("fixer-decision.md", "/run/fix-<ORCHESTRATOR: n>-commit.txt")):
            with self.subTest(name=name):
                text = fixer_skeleton(name[len("fixer-"):-len(".md")], "commits", "/run")
                self.assertIn("/run/uncommitted.txt", text)
                self.assertIn("has no copy in git", text)
                self.assertIn("applied, not committed", text)
                self.assertIn("committed whole or not at all", text)
                self.assertIn("git status --porcelain --untracked-files=all -- <file>", text)   # whatever status.showUntrackedFiles says
                self.assertNotIn("git status --porcelain -- <file>", text)
                self.assertIn("holds the user's uncommitted work; left to the user", text)
                self.assertIn(f"git commit --only -F {message} --", text)
                self.assertIn("git log -n 20", text)
                self.assertIn("Do not mention the review", text)
                self.assertIn("Add no trailers", text)
                self.assertNotIn("review: auto-fix", text)
                self.assertNotIn('-m "', text)
                self.assertIn("no commit", text)
                self.assertIn("applied, not committed: changed during the review", text)
                self.assertIn('under "Files changed during the review"', text)
                self.assertNotIn("already modified by a reviewer", text)
                self.assertIn("Generated with", text)
                if name == "fixer-decision.md":
                    self.assertIn("Problem: <ORCHESTRATOR:", text)
                else:                                   # --only commits a file whole, the other fix's change too
                    self.assertIn("Apply every fix first, then decide what to commit", text)
                    self.assertIn("applied, not committed: shares <file> with an uncommitted fix", text)
                    self.assertIn("Commit once, with only the files of the fixes still marked `done`", text)

    def test_orchestrator_prompt_names_the_dialogs_and_protects_the_users_files(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["RUNNER"] = "/opt/hr/bin/herdr-review"
        values["RUN_DIR"] = "/run"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            "new MCP servers found in this project", "not even with Esc", "Trust and continue", "Grok — `y`",
            "/run/uncommitted.txt", "/run/scratch/<profile>/", "вне изменения: ваш незакоммиченный файл",
            "применено, не закоммичено", "log --oneline v..HEAD", "rev-parse HEAD",
            "/run/reviews/<profile>.md", "except your review file", "everything else its task file asks for",
            'cursor is on "Quit"', "--name-only", "the user went on editing their own files",
            "In scope `worktree` the fixer commits nothing", "применено, не закоммичено: ревью незакоммиченной работы — закоммитьте сами",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("--stat", text)
        self.assertNotIn("unprotected file", text)

    def test_drift_is_worded_as_a_change_of_the_tree_not_of_a_reviewer(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        texts = {"orchestrator.md": render_file(PROMPTS_DIR / "orchestrator.md", values)}
        for kind in ("auto", "decision"):
            for scope in ("commits", "worktree"):
                texts[f"fixer-{kind} ({scope})"] = fixer_skeleton(kind, scope, "/run")
        for name, text in texts.items():
            with self.subTest(name=name):
                self.assertIn("Files changed during the review", text)
                for old in ("Files a reviewer already changed", "changed by a reviewer", "a reviewer already changed"):
                    self.assertNotIn(old, text)
        self.assertIn("`drift_status` lists the `git status --short` lines that are new since launch", texts["orchestrator.md"])

    def test_the_drift_step_names_uncommitted_work_gone_since_launch(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            f"the lines that are gone since launch, each marked `{DRIFT_GONE.strip()}`",     # the runner's own mark
            "A gone line means uncommitted work that was there at launch no longer shows",
            "name it plainly to the user and in the report's drift section",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("a file uncommitted then changed again", text)

    def test_the_orchestrators_fixer_rules_follow_the_scope(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values.update(RUN_DIR="/run", REPO="/repo", FIXER_NAME="hr-fixer")
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            "In scope `commits` committing its fixes is its job too → confirm",
            'In scope `worktree` it commits nothing: any `git add` or `git commit` → refuse with the dialog\'s own "no" option',
            'herdr agent prompt hr-fixer "Stage nothing and commit nothing: nothing is committed in this scope',
            'In scope `worktree`, note what `git -C "/repo" rev-parse HEAD` prints',
            "that HEAD did not move during the task", "never ask the fixer to commit, rewrite or undo anything in this scope",
            "in scope `worktree` only that HEAD did not move, with no prompt to commit",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("changing repository files and committing them is its job", text)
        commits = text[text.index("In scope `commits`, first make sure"):text.index("In both scopes")]
        self.assertIn("Commit your changes now", commits)                  # the prompts to commit are scope `commits` only
        self.assertIn("its fixes count as `done` without a commit", commits)
        self.assertEqual(text.count("Commit your changes now"), 1)

    def test_orchestrator_checks_that_a_fix_commit_is_the_one_the_task_made(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["RUN_DIR"] = "/run"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            'log -1 --format=%s <hash>', "must equal the first line of `/run/fix-auto-commit.txt`",
            "compared to the first line of `/run/fix-<i>-commit.txt`", "compare subjects, not whole messages",
            "must differ from `v` and from every hash an earlier fix of this run reported",
            "its fixes count as `done` without a commit", "применено, не закоммичено: коммит не создан",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("review(auto-decide)", text)
        self.assertNotIn("review: auto-fix", text)

if __name__ == "__main__":
    unittest.main()
