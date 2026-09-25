import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from herdr_review import PROMPTS_DIR
from herdr_review.gitutil import UntrackedFile
from herdr_review.render import placeholders, render_file
from herdr_review.runner import DRIFT_GONE
from herdr_review.scope import exclusive_rules, fixer_skeleton, reviewer_steps

NO_COMMIT = "Commit nothing. The change under review is uncommitted work"
EXPECTED = {
    "terminal.md": {"FILE"},
    "reviewer.md": {"DESCRIPTION", "PLAN_REFERENCE", "REPO", "BASE_REF", "MERGE_BASE", "RESULT_PATH", "REVIEWER", "SCOPE_STEPS", "SCRATCH_DIR", "EXCLUSIVE_RULES"},
    "fixer-auto.md": {"RUN_DIR", "COMMIT_RULES", "EXCLUSIVE_RULES"},
    "fixer-decision.md": {"RUN_DIR", "COMMIT_RULES", "EXCLUSIVE_RULES"},
    "exclusive.md": {"RUNNER"},
    "fixer-commit-auto.md": {"RUN_DIR"},
    "fixer-commit-decision.md": {"RUN_DIR"},
    "fixer-commit-none.md": {"RUN_DIR"},
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

    def test_a_reviewers_copy_of_the_repository_registers_nothing_in_it(self):
        values = {k: "v" for k in EXPECTED["reviewer.md"]}
        values.update(REPO="/repo", SCRATCH_DIR="/run/scratch/codex")
        reviewer = render_file(PROMPTS_DIR / "reviewer.md", values)
        self.assertIn('Copy the repository with `git clone "/repo" "/run/scratch/codex/repo"`.', reviewer)
        # the flags keep the patch intact under diff.external, color.ui=always, a textconv driver and diff.noprefix,
        # and the copy exact under apply.whitespace; an empty patch, only untracked files uncommitted, applies too
        self.assertIn('When the change under review includes uncommitted work, bring it along with `git -C "/repo" diff'
                      ' --binary --no-color --no-ext-diff --no-textconv --src-prefix=a/ --dst-prefix=b/ HEAD'
                      ' | git -C "/run/scratch/codex/repo" apply --allow-empty --whitespace=nowarn`, and copy the'
                      ' untracked files of the change over.', reviewer)
        for unquoted in ("git clone /repo", "git -C /repo ", "git -C /run/scratch/codex/repo "):   # a path with a space
            self.assertNotIn(unquoted, reviewer)
        self.assertIn("Never use `git worktree add`: it registers the copy in the repository and can create a branch"
                      " there.", reviewer)
        # a linked worktree's `.git` is a file: git in a copy of it moves the owner's HEAD and commits on its branch
        self.assertIn("Never run git in a copy made with `cp` or `rsync`: in a linked worktree `.git` is a file that points"
                      " back at the repository, so such a copy shares its HEAD, index and branches.", reviewer)
        self.assertNotIn("cp -a", reviewer)
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["RUN_DIR"] = "/run"
        orchestrator = render_file(PROMPTS_DIR / "orchestrator.md", values)
        rule = orchestrator[orchestrator.index("       - a reviewer:"):orchestrator.index("       - the fixer:")]
        self.assertIn("`git worktree add` counts as a write into the repository, even with a path under the scratch directory", rule)
        # a reviewer refused for `git worktree add <scratch>/copy` already had its copy under scratch; no backticks
        # in the message: the orchestrator runs it in a shell, inside double quotes
        self.assertIn('`herdr agent prompt <name> "Do not write into the repository or outside your scratch directory, except'
                      ' your review file. Put experiments under /run/scratch/<profile>/. Copy the repository with git clone,'
                      ' never with git worktree add."`', rule)

    def test_the_worktree_steps_say_how_a_quoted_name_is_written(self):
        text = reviewer_steps("worktree", "abc123", [], [UntrackedFile("Icon\r", 0)], Path("/run/uncommitted.txt"), Path("/run/untracked.txt"))
        self.assertIn("- `\"Icon\\r\"` (0 B)", text)
        self.assertIn("A name in double quotes is written the way git quotes a path, with C escapes and each byte that is not"
                      " UTF-8 as three-digit octal; in a shell, `$'…'` with the same escapes names the file: `$'caf\\351.py'`,"
                      " `$'Icon\\r'`.", text)

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
                self.assertIn('git commit --only -F "/run/fix-', commits)
                self.assertNotIn("{", commits)

    def test_the_worktree_commit_rule_leaves_the_skip_rule_in_force(self):
        for kind in ("auto", "decision"):
            with self.subTest(kind=kind):
                text = fixer_skeleton(kind, "worktree", "/run")
                self.assertIn("Report each fix you apply as `applied, not committed: the review covers uncommitted work`", text)
                self.assertIn("The Rules above still hold: a fix that would delete, move or rename a path listed in /run/uncommitted.txt", text)
                self.assertIn("is not applied, and is reported `skipped: <path> holds the user's uncommitted work; left to the user`", text)
                self.assertNotIn("Apply each fix in full", text)                # it read as covering the fixes to skip too

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
                self.assertIn(f'git commit --only -F "{message}" --', text)
                self.assertIn("git log -n 20", text)
                self.assertIn("Do not mention the review", text)
                self.assertIn("Add no trailers", text)
                # git drops a blank line before the subject, and a `#` subject under commit.cleanup=strip
                self.assertIn("Start the file with the subject line, with no blank line before it, and never start the"
                              " subject with `#`", text)
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
                    # the message comes after the decision and holds only what is committed
                    self.assertIn("Only then write the commit message, for the fixes still marked `done`", text)
                    self.assertIn("one line per fix still marked `done`", text)
                    self.assertLess(text.index("then decide what to commit"), text.index("write the commit message"))

    def test_the_fixers_commit_command_keeps_a_message_path_with_a_space_whole(self):
        for kind, message in (("auto", "fix-auto-commit.txt"), ("decision", "fix-<ORCHESTRATOR: n>-commit.txt")):
            with self.subTest(kind=kind):
                text = fixer_skeleton(kind, "commits", "/my runs/hr1")
                commands = re.findall(r"`(git commit --only -F [^`]*)`", text)
                self.assertEqual(len(commands), 1)
                args = shlex.split(commands[0])
                self.assertEqual(args[args.index("-F") + 1], f"/my runs/hr1/{message}")

    def test_orchestrator_prompt_names_the_dialogs_and_protects_the_users_files(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["RUNNER"] = "/opt/hr/bin/herdr-review"
        values["RUN_DIR"] = "/run"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            "new MCP servers found in this project", "not even with Esc", "Trust and continue", "Grok — `y`",
            "/run/uncommitted.txt", "/run/scratch/<profile>/", "вне изменения: ваш незакоммиченный файл",
            "применено, не закоммичено", "log --oneline --first-parent --no-color --no-show-signature v..HEAD", "rev-parse HEAD",
            "/run/reviews/<profile>.md", "except your review file", "everything else its task file asks for",
            'cursor is on "Quit"', "--name-only", "the user went on editing their own files",
            "In scope `worktree` the fixer commits nothing", "применено, не закоммичено: ревью незакоммиченной работы — закоммитьте сами",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("--stat", text)
        self.assertNotIn("unprotected file", text)
        self.assertNotIn("review(auto-decide)", text)
        self.assertNotIn("review: auto-fix", text)

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

    def test_every_prompt_that_introduces_the_uncommitted_list_says_how_to_read_it(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        texts = {"orchestrator.md": render_file(PROMPTS_DIR / "orchestrator.md", values)}
        for kind in ("auto", "decision"):
            for scope in ("commits", "worktree"):
                texts[f"fixer-{kind} ({scope})"] = fixer_skeleton(kind, scope, "/run")
        # the commits-scope reviewer matches the list against `git diff` for a file of the change with uncommitted edits
        texts["scope-commits.md"] = reviewer_steps("commits", "abc123", [" M a.txt"], [], Path("/run/uncommitted.txt"), Path("/run/untracked.txt"))
        for name, text in texts.items():
            with self.subTest(name=name):
                # git prints a quoted untracked directory as `?? "my notes/"`: the path ends in `/`, the entry does not
                self.assertRegex(text, "[Aa]n entry whose path ends in `/` covers everything under that directory")
                self.assertIn("Each entry is a `git status --short` line: a two-character status such as `??` or ` M`, a"
                              " space, then the path — in double quotes with C escapes when it holds a space or another"
                              " special character, and `old -> new` for a rename.", text)
                # `git status --short` quotes `my notes/`; `git … --name-only` and `git diff` do not
                command = "`git diff`" if name == "scope-commits.md" else "`git … --name-only`"
                self.assertIn(f"{command} leaves a path unquoted when a space is its only special character, so compare"
                              " paths, not their quoting: `?? \"my notes/\"` is the untracked directory `my notes/`.", text)
                self.assertNotRegex(text, "[Aa]n entry ending in `/`")
                for old in ("two status letters", "a path that only holds spaces"):        # `??`, ` M`; a path of spaces
                    self.assertNotIn(old, text)

    def test_the_drift_step_names_uncommitted_work_gone_since_launch(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            f"the lines that are gone since launch, each marked `{DRIFT_GONE.strip()}`",     # the runner's own mark
            "A gone line means uncommitted work that was there at launch no longer shows",
            "no longer shows — reverted, stashed or committed, by an agent or by the user",     # committed loses nothing
            "больше не видны — откачены, убраны в stash или закоммичены",
            "name it plainly to the user and in the report's drift section",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("a file uncommitted then changed again", text)
        for lost in ("пропали", "пропавшую"):                                              # committed work is not lost
            self.assertNotIn(lost, text)

    def test_the_drift_step_names_a_file_inside_an_untracked_directory_of_the_launch(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        step = text[text.index("Then look at `drift`"):text.index("If `collect` reports zero")]
        self.assertIn("says in one line that none is new or gone: a file appeared, changed or was deleted inside an"
                      " untracked directory that was already there at launch", step)   # `?? dir/` of the launch collapses it
        self.assertNotIn("went inside", text)                                          # it read as "entered"

    def test_phase_3_verifies_a_file_with_uncommitted_edits_against_the_reviewed_commits(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values.update(RUN_DIR="/run", REPO="/repo")
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        aggregate = text[text.index("## Phase 3"):text.index("## Phase 4")]
        self.assertIn('In scope `commits`, verify a file listed in `/run/uncommitted.txt` against'
                      ' `git -C "/repo" show HEAD:<path>`, not the working tree', aggregate)

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

    def test_in_the_commits_scope_the_fixer_may_not_stage_a_path_changed_during_the_review(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["RUN_DIR"] = "/run"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        rule = text[text.index("       - the fixer:"):text.index("     * a question about the task")]
        self.assertIn('staging or committing a path from `/run/uncommitted.txt` or from "Files changed during the review"'
                      " in its task file (`git add <path>`, `git commit … -- <path>`) → refuse", rule)   # as its own rule says

    def test_in_the_worktree_scope_a_moved_head_is_not_blamed_on_the_fixer(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["REPO"] = "/repo"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        worktree = text[text.index("In scope `worktree` the fixer commits nothing"):text.index("In scope `commits`, first make sure")]
        for phrase in (                                                  # the user may commit during a fixer task too
            'When HEAD moved during the task, list the new commits with'
            ' `git -C "/repo" log --oneline --first-parent --no-color --no-show-signature <the noted hash>..HEAD`',
            "record a commit as the fixer's only when the fixer's report names its hash",
            "«HEAD сдвинулся во время задачи фиксера: <hash> <тема> — коммит фиксера или ваш»",
            "never ask the fixer to commit, rewrite or undo anything in this scope",
        ):
            self.assertIn(phrase, worktree)
        self.assertNotIn("a commit made anyway (HEAD moved)", text)
        self.assertIn("in scope `worktree` only that HEAD did not move, with no prompt to commit, and when it moved, its new"
                      " commits listed and recorded as in Phase 4", text)

    def test_orchestrator_checks_that_a_fix_commit_is_the_one_the_task_made(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["RUN_DIR"] = "/run"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            'the first non-empty line of the commit\'s message (`git -C "v" log -1 --no-color --no-show-signature --format=%B <hash>`)',
            "must equal the first non-empty line of `/run/fix-auto-commit.txt`, trailing whitespace ignored",
            "compare those lines, not whole messages: a commit-msg hook may append trailers, and git drops leading blank"
            " lines and trailing spaces from a message",
            "with the first non-empty line of its message compared to the first non-empty line of `/run/fix-<i>-commit.txt`,"
            " trailing whitespace ignored",
            "must differ from `v` and from every hash an earlier fix of this run reported",
            "its fixes count as `done` without a commit", "применено, не закоммичено: коммит не создан",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("--format=%s", text)        # git's subject joins the first paragraph into one line
        for old in ("the first line of", "compare first lines"):                # a leading blank line fails that check
            self.assertNotIn(old, text)

    def test_the_report_takes_the_runs_commits_from_git_only_while_the_branch_holds_the_start_head(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values.update(REPO="/repo", START_HEAD="abc123")
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        report = text[text.index("## Phase 6"):text.index("## Red flags")]
        for phrase in (
            'git -C "/repo" merge-base --is-ancestor abc123 HEAD',
            'git -C "/repo" log --oneline --first-parent --no-color --no-show-signature abc123..HEAD',   # a merge of the base brings none of its commits
            "«ветку переписали во время прогона (rebase, amend, reset или смена ветки) — git не отделит коммиты"
            " прогона; ниже коммиты фиксера по его отчётам»",
            "the fix commits you noted from the fixer's reports — the same hashes you pass to `run finish --commits`",
        ):
            self.assertIn(phrase, report)
        self.assertLess(report.index("merge-base --is-ancestor"), report.index("log --oneline --first-parent --no-color --no-show-signature abc123..HEAD"))

    def test_the_report_takes_the_runs_commits_from_git_only_on_the_branch_of_the_launch(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values.update(REPO="/repo", START_HEAD="abc123", BRANCH="feat")
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        report = text[text.index("## Phase 6"):text.index("## Red flags")]
        # a branch made from the branch of the launch still holds the HEAD of the launch: the ancestor check passes
        branch = '`git -C "/repo" rev-parse --abbrev-ref HEAD` prints `feat`'
        ancestor = '`git -C "/repo" merge-base --is-ancestor abc123 HEAD` succeeds'
        range_ = '`git -C "/repo" log --oneline --first-parent --no-color --no-show-signature abc123..HEAD`'
        for phrase in (branch, ancestor, range_, "Otherwise the branch was switched or rewritten during the run"):
            self.assertIn(phrase, report)
        self.assertLess(report.index(branch), report.index(range_))
        self.assertLess(report.index(ancestor), report.index(range_))

    def test_every_command_of_the_orchestrator_that_prints_commits_ignores_colour_and_signatures(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["REPO"] = "/repo"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        commands = [c for c in re.findall(r'`(git -C "/repo" [^`]*)`', text) if re.search(r" (log|show) ", c)]
        printing = [c for c in commands if " show HEAD:" not in c]         # a blob read prints no commit
        self.assertGreaterEqual(len(printing), 3)                           # the fix-commit checks and the report's list
        for command in printing:                                            # color.ui=always, log.showSignature=true
            with self.subTest(command=command):
                self.assertIn(" --no-color", command)
                self.assertIn(" --no-show-signature", command)

    def test_the_heavy_command_rules_reach_the_reviewer_and_the_fixer(self):
        runner = "/opt/hr/bin/herdr-review"
        values = {k: "v" for k in EXPECTED["reviewer.md"]}
        values["EXCLUSIVE_RULES"] = exclusive_rules(runner)
        reviewer = render_file(PROMPTS_DIR / "reviewer.md", values)
        texts = {"reviewer": reviewer}
        for kind in ("auto", "decision"):
            for scope in ("commits", "worktree"):
                texts[f"fixer-{kind} ({scope})"] = fixer_skeleton(kind, scope, "/run", runner)
        for name, text in texts.items():
            with self.subTest(name=name):
                self.assertIn('Run each one through `"/opt/hr/bin/herdr-review" exclusive -- <command> [args…]`', text)
                self.assertIn("`\"/opt/hr/bin/herdr-review\" exclusive -- sh -c 'npm ci && npm test'`", text)
                self.assertIn("a commit whose hooks build or test", text)
                self.assertIn("Exit code 75 with a `herdr-review exclusive: busy` line means the command did not run.", text)
                self.assertIn("never in a shell loop", text)
                self.assertIn("pass `--timeout <seconds>` before the `--`", text)
                self.assertIn("No server, container or watcher may outlive the call.", text)
                self.assertIn("Never run the command without it.", text)
                self.assertNotIn("{", text)
        self.assertIn("## Heavy Commands", reviewer)
        self.assertIn("running the project's own tests — through the wrapper that Heavy Commands below describes — are fine", reviewer)
        self.assertIn("Do not give up a check you need because the queue is busy.", reviewer)
        for kind in ("auto", "decision"):
            text = texts[f"fixer-{kind} (commits)"]
            self.assertIn("## Heavy commands", text)
            self.assertIn("run them as the section Heavy commands below says", text)
            self.assertIn("Never skip the tests because the queue is busy", text)

    def test_the_orchestrator_judges_a_wrapped_command_by_the_command_and_refuses_an_unwrapped_heavy_one(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values.update(RUNNER="/opt/hr/bin/herdr-review", RUN_DIR="/run")
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        for phrase in (
            'a command run through `"/opt/hr/bin/herdr-review" exclusive -- <command>` → judge `<command>` by the rules below',
            "the wrapper itself writes only its queue files in the runs directory and the run's `runner.log`",
            'Run builds, tests, dependency installs, servers and containers through \\"/opt/hr/bin/herdr-review\\" exclusive -- <command>, as your prompt says.',
            "the project's own tests or build run through the wrapper",
            'so is `herdr-review exclusive` waiting for its turn or running its command; `"/opt/hr/bin/herdr-review" status --run "/run"` names who holds the build queue',
            '| Confirming a heavy command that a reviewer or the fixer runs without `"/opt/hr/bin/herdr-review" exclusive` | Refuse; point the agent at the wrapper. |',
        ):
            self.assertIn(phrase, text)

    def test_the_orchestrator_carries_a_stopped_build_queue_command_into_the_report(self):
        values = {k: "v" for k in EXPECTED["orchestrator.md"]}
        values["RUN_DIR"] = "/run"
        text = render_file(PROMPTS_DIR / "orchestrator.md", values)
        rules = text[text.index("## Ground rules"):text.index("## Phase 1")]
        report = text[text.index("## Phase 6"):text.index("## Red flags")]
        self.assertIn("When the JSON of `run fail` or `run finish` carries `exclusive_stopped`, the runner stopped a command"
                      " of this run that held the build queue: note the value for the report (Phase 6).", rules)
        self.assertIn("**Очередь сборок**, если `run fail` или `run finish` вернули `exclusive_stopped`", report)
        self.assertIn("When its JSON carries `exclusive_stopped`, add it to the «Очередь сборок» bullet of `/run/report.md` now.",
                      report)


# a global config under which a plain `git diff HEAD | git apply` fails
HOSTILE_GIT_CONFIG = (
    "[color]\n\tui = always\n"
    "[diff]\n\texternal = echo external diff\n\tnoprefix = true\n"
    "[apply]\n\twhitespace = error\n"
)


def tree_files(top: Path) -> dict[str, tuple[bool, bytes]]:
    """Every file under <top> but `.git`, a directory in a clone and a file in a linked worktree: whether it is
    executable, and its bytes."""
    return {
        path.relative_to(top).as_posix(): (bool(path.stat().st_mode & stat.S_IXUSR), path.read_bytes())
        for path in top.rglob("*")
        if path.relative_to(top).parts[0] != ".git" and path.is_file()
    }


class ReviewerCopyCommandsTest(unittest.TestCase):
    """The reviewer's copy commands as the rendered prompt gives them, run in bash: the source is a linked worktree,
    both paths hold a space, and the global config breaks a plain `git diff HEAD | git apply`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "gitconfig").write_text(HOSTILE_GIT_CONFIG)
        # neither the owner's git config nor a GIT_* variable of the caller reaches git
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(HOME=str(self.root), GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
        main = self.root / "main repo"
        main.mkdir()
        self.git(main, "init", "-q", "-b", "master")
        self.git(main, "config", "user.email", "t@example.com")
        self.git(main, "config", "user.name", "T")
        (main / "notes.txt").write_text("one\n")
        (main / "image.bin").write_bytes(bytes(range(256)))
        (main / "old.txt").write_text("old\n")
        self.git(main, "add", "notes.txt", "image.bin", "old.txt")
        self.git(main, "commit", "-q", "-m", "init")
        self.source = self.root / "my worktree"                  # a linked worktree, on a branch with its own commit
        self.git(main, "worktree", "add", "-q", "-b", "feat", str(self.source))
        (self.source / "feat.txt").write_text("feat\n")
        self.git(self.source, "add", "feat.txt")
        self.git(self.source, "commit", "-q", "-m", "feat")
        (self.source / "untracked.txt").write_text("not in git\n")

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], env=self.env, check=True, capture_output=True)

    def bash(self, command: str) -> None:
        # pipefail: with --allow-empty, `git apply` alone would pass a failed `git diff` off as an empty patch
        done = subprocess.run(["bash", "-o", "pipefail", "-c", command], cwd=self.root, env=self.env,
                              capture_output=True, text=True, errors="replace")
        self.assertEqual(done.returncode, 0, f"{command}\n{done.stdout}{done.stderr}")

    def copy_the_source(self) -> Path:
        """Copy the source with the commands of the rendered prompt, then copy its untracked file over."""
        scratch = self.root / "scratch dir"
        scratch.mkdir()
        values = {k: "v" for k in EXPECTED["reviewer.md"]}
        values.update(REPO=str(self.source), SCRATCH_DIR=str(scratch))
        reviewer = render_file(PROMPTS_DIR / "reviewer.md", values)
        clone = re.search(r"Copy the repository with `([^`]+)`", reviewer)
        patch = re.search(r"bring it along with `([^`]+)`", reviewer)
        self.assertIsNotNone(clone)
        self.assertIsNotNone(patch)
        self.bash(clone.group(1))
        self.bash(patch.group(1))
        shutil.copy2(self.source / "untracked.txt", scratch / "repo" / "untracked.txt")
        return scratch / "repo"

    def test_a_reviewers_copy_of_a_linked_worktree_equals_it(self):
        (self.source / "notes.txt").write_text("one\ntrailing spaces   \n")       # unstaged; apply.whitespace=error
        (self.source / "image.bin").write_bytes(bytes(reversed(range(256))))
        self.git(self.source, "add", "image.bin")                                   # staged, binary
        (self.source / "old.txt").unlink()
        copy = self.copy_the_source()
        source = tree_files(self.source)
        self.assertEqual(sorted(source), ["feat.txt", "image.bin", "notes.txt", "untracked.txt"])
        self.assertEqual(tree_files(copy), source)

    def test_the_patch_step_passes_when_only_untracked_files_are_uncommitted(self):
        copy = self.copy_the_source()                                              # `git diff HEAD` prints nothing
        self.assertEqual(tree_files(copy), tree_files(self.source))


if __name__ == "__main__":
    unittest.main()
