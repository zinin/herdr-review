import unittest

from herdr_review import PROMPTS_DIR
from herdr_review.render import placeholders, render_file

EXPECTED = {
    "terminal.md": {"FILE"},
    "reviewer.md": {"DESCRIPTION", "PLAN_REFERENCE", "REPO", "BASE_REF", "MERGE_BASE", "RESULT_PATH", "REVIEWER"},
    "fixer-auto.md": {"RUN_DIR"},
    "fixer-decision.md": {"RUN_DIR"},
    "scope-commits.md": {"MERGE_BASE", "UNCOMMITTED"},
    "scope-worktree.md": {"MERGE_BASE", "UNTRACKED"},
    "orchestrator.md": {
        "RUN_DIR", "RUNNER", "RUN_ID", "REPO", "BRANCH", "BASE_REF", "MERGE_BASE", "REVIEWERS", "ORCH_NAME",
        "FIXER_NAME", "FIXER_PROFILE", "AUTODECIDE", "LAYOUT", "CHECKIN_SEC", "DESCRIPTION", "PLAN_REFERENCE",
        "FIXER_AUTO_SKELETON", "FIXER_DECISION_SKELETON",
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
        text = render_file(PROMPTS_DIR / "reviewer.md", {k: "v" for k in EXPECTED["reviewer.md"]})
        for heading in ("### Strengths", "### Critical Issues", "### Important Issues", "### Minor Issues", "### Assessment"):
            self.assertIn(heading, text)
        self.assertIn("git diff v --", text)
        self.assertIn("DONE", text)
        self.assertIn("Do NOT modify", text)
        self.assertIn("gitignored", text)

    def test_fixer_skeletons_mention_report_and_done(self):
        for name in ("fixer-auto.md", "fixer-decision.md"):
            text = render_file(PROMPTS_DIR / name, {"RUN_DIR": "/run"})
            self.assertIn("/run/", text)
            self.assertIn("DONE", text)
            self.assertIn("Do not push", text)

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

if __name__ == "__main__":
    unittest.main()
