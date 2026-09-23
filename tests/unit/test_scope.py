import unittest
from pathlib import Path

from herdr_review import scope
from herdr_review.gitutil import UntrackedFile

MB = "abcdef0123456789" + "0" * 24


class ResolveScopeTest(unittest.TestCase):
    def test_auto_prefers_the_commits(self):
        self.assertEqual(scope.resolve_scope("auto", True, True, "master", MB), "commits")
        self.assertEqual(scope.resolve_scope("auto", True, False, "master", MB), "commits")

    def test_auto_falls_back_to_the_working_tree(self):
        self.assertEqual(scope.resolve_scope("auto", False, True, "master", MB), "worktree")

    def test_explicit_scopes(self):
        self.assertEqual(scope.resolve_scope("worktree", True, True, "master", MB), "worktree")
        self.assertEqual(scope.resolve_scope("worktree", True, False, "master", MB), "worktree")
        self.assertEqual(scope.resolve_scope("commits", True, True, "master", MB), "commits")

    def test_commits_without_a_commit_names_the_way_out(self):
        with self.assertRaises(scope.ScopeError) as ctx:
            scope.resolve_scope("commits", False, True, "master", MB)
        self.assertIn("nothing committed on this branch since master (abcdef012345)", str(ctx.exception))
        self.assertIn("--scope worktree", str(ctx.exception))

    def test_nothing_at_all(self):
        for requested in ("auto", "commits", "worktree"):
            with self.subTest(requested=requested):
                with self.assertRaises(scope.ScopeError) as ctx:
                    scope.resolve_scope(requested, False, False, "master", MB)
                self.assertIn("nothing to review: the working tree equals master (abcdef012345)", str(ctx.exception))

    def test_unknown_scope(self):
        with self.assertRaises(scope.ScopeError):
            scope.resolve_scope("everything", True, True, "master", MB)


class BlocksTest(unittest.TestCase):
    def test_uncommitted_block_inlines_up_to_the_limit(self):
        listing = Path("/run/uncommitted.txt")
        self.assertIn("clean", scope.uncommitted_block([], listing))
        short = scope.uncommitted_block([" M a.txt", "?? notes/"], listing)
        self.assertIn("```\n M a.txt\n?? notes/\n```", short)
        self.assertNotIn("more", short)
        many = [f"?? f{i}.txt" for i in range(scope.UNCOMMITTED_INLINE + 7)]
        text = scope.uncommitted_block(many, listing)
        self.assertIn(f"?? f{scope.UNCOMMITTED_INLINE - 1}.txt", text)
        self.assertNotIn(f"?? f{scope.UNCOMMITTED_INLINE}.txt", text)
        self.assertIn("…and 7 more: /run/uncommitted.txt lists them all.", text)

    def test_untracked_block_marks_skipped_files(self):
        text = scope.untracked_block([
            UntrackedFile("src/new.py", 900),
            UntrackedFile("review.diff", 408 * 1024, "larger than 256 KB"),
            UntrackedFile("logo.png", 2048, "binary"),
        ])
        self.assertIn("- `src/new.py` (900 B)", text)
        self.assertIn("- `review.diff` (408 KB) — skip: larger than 256 KB", text)
        self.assertIn("- `logo.png` (2 KB) — skip: binary", text)
        self.assertEqual(scope.untracked_block([]), "There are none.")

    def test_untracked_block_caps_the_list(self):
        files = [UntrackedFile(f"f{i}.txt", 1) for i in range(scope.UNTRACKED_INLINE + 3)]
        text = scope.untracked_block(files)
        self.assertIn("…and 3 more", text)
        self.assertIn("git ls-files --others --exclude-standard", text)

    def test_counts_and_sizes(self):
        self.assertEqual(scope.uncommitted_counts([" M a.txt", "M  b.txt", "?? notes/", "?? x"]), (2, 2))
        self.assertEqual(scope.uncommitted_counts([]), (0, 0))
        self.assertEqual(scope.human_size(3 * 1024 * 1024 // 2), "1.5 MB")


class ReviewerStepsTest(unittest.TestCase):
    def test_commits_steps(self):
        text = scope.reviewer_steps("commits", MB, [" M x.txt"], [], Path("/run/uncommitted.txt"))
        self.assertTrue(text.startswith(f"1. Run `git diff {MB} HEAD --`"))
        self.assertIn("git show HEAD:<path>", text)
        self.assertIn(" M x.txt", text)
        self.assertNotIn("{", text)

    def test_worktree_steps(self):
        text = scope.reviewer_steps("worktree", MB, [], [UntrackedFile("new.py", 10)], Path("/run/uncommitted.txt"))
        self.assertTrue(text.startswith(f"1. Run `git diff {MB} --`"))
        self.assertIn("- `new.py` (10 B)", text)
        self.assertIn("do not `git add` anything", text)
        self.assertNotIn("{", text)


if __name__ == "__main__":
    unittest.main()
