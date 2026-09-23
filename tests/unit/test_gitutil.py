import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_review import gitutil


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_repo(d: Path) -> Path:
    repo = d / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "init")
    return repo


class GitUtilTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_root_and_not_a_repo(self):
        sub = self.repo / "sub"
        sub.mkdir()
        self.assertEqual(gitutil.repo_root(sub).resolve(), self.repo.resolve())
        with self.assertRaises(gitutil.GitError):
            gitutil.repo_root(Path(self.tmp.name))

    def test_detect_base_prefers_master_then_main(self):
        self.assertEqual(gitutil.detect_base(self.repo), "master")
        git(self.repo, "branch", "-m", "master", "main")
        self.assertEqual(gitutil.detect_base(self.repo), "main")
        git(self.repo, "branch", "-m", "main", "trunk")
        with self.assertRaises(gitutil.GitError):
            gitutil.detect_base(self.repo)

    def test_detect_base_uses_origin_head(self):
        git(self.repo, "remote", "add", "origin", str(self.repo))
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
        self.assertEqual(gitutil.detect_base(self.repo), "origin/master")

    def test_merge_base_and_current_branch(self):
        git(self.repo, "switch", "-q", "-c", "feat")
        base = gitutil.merge_base(self.repo, "master")
        self.assertEqual(len(base), 40)
        (self.repo / "a.txt").write_text("two\n")
        git(self.repo, "commit", "-q", "-am", "change")
        self.assertEqual(gitutil.merge_base(self.repo, "master"), base)
        self.assertEqual(gitutil.current_branch(self.repo), "feat")
        with self.assertRaises(gitutil.GitError):
            gitutil.merge_base(self.repo, "no-such-branch")

    def test_tree_hash_changes_with_untracked_and_modified_files(self):
        h0 = gitutil.tree_hash(self.repo)
        (self.repo / "new.txt").write_text("x\n")
        h1 = gitutil.tree_hash(self.repo)
        self.assertNotEqual(h0, h1)
        (self.repo / "new.txt").unlink()
        self.assertEqual(gitutil.tree_hash(self.repo), h0)
        (self.repo / "a.txt").write_text("changed\n")
        self.assertNotEqual(gitutil.tree_hash(self.repo), h0)
        self.assertIn("a.txt", gitutil.status_short(self.repo))

    def test_tree_hash_notices_a_same_size_same_mtime_rewrite(self):
        path = self.repo / "new.txt"
        path.write_text("aaa\n")
        stamp = path.stat().st_mtime_ns
        h0 = gitutil.tree_hash(self.repo)
        path.write_text("bbb\n")                            # same size
        os.utime(path, ns=(stamp, stamp))                   # and the same mtime
        self.assertEqual(path.stat().st_size, 4)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertNotEqual(gitutil.tree_hash(self.repo), h0)

    def test_touching_an_untracked_file_is_not_a_change(self):
        path = self.repo / "new.txt"
        path.write_text("aaa\n")
        h0 = gitutil.tree_hash(self.repo)
        stamp = path.stat().st_mtime_ns + 10**9
        os.utime(path, ns=(stamp, stamp))
        self.assertEqual(gitutil.tree_hash(self.repo), h0)

    def test_tree_hash_ignores_ignored_files(self):
        (self.repo / ".gitignore").write_text("build/\n")
        h0 = gitutil.tree_hash(self.repo)
        (self.repo / "build").mkdir()
        (self.repo / "build" / "out.o").write_text("junk\n")
        self.assertEqual(gitutil.tree_hash(self.repo), h0)

    def test_tree_hash_asks_git_only_when_there_is_something_untracked(self):
        with mock.patch.object(gitutil, "_run_stdin", side_effect=AssertionError("hash-object called")) as stdin_run:
            gitutil.tree_hash(self.repo)
            self.assertEqual(stdin_run.call_count, 0)
        (self.repo / "new.txt").write_text("aaa\n")
        with mock.patch.object(gitutil, "_run_stdin", wraps=gitutil._run_stdin) as stdin_run:
            gitutil.tree_hash(self.repo)
        self.assertEqual(stdin_run.call_count, 1)

    def test_tree_hash_survives_a_file_that_vanished(self):
        (self.repo / "new.txt").write_text("aaa\n")
        real = gitutil._untracked

        def with_a_ghost(repo):
            return sorted([*real(repo), "gone.txt"])

        with mock.patch.object(gitutil, "_untracked", with_a_ghost):
            self.assertNotEqual(gitutil.tree_hash(self.repo), "")

    def test_tree_hash_changes_when_a_commit_lands_on_a_clean_tree(self):
        h0 = gitutil.tree_hash(self.repo)
        (self.repo / "a.txt").write_text("committed\n")
        git(self.repo, "commit", "-q", "-am", "reviewer commit")
        self.assertNotEqual(gitutil.tree_hash(self.repo), h0)

    def test_log_oneline(self):
        git(self.repo, "switch", "-q", "-c", "feat")
        (self.repo / "a.txt").write_text("two\n")
        git(self.repo, "commit", "-q", "-am", "second commit")
        out = gitutil.log_oneline(self.repo, "master..HEAD")
        self.assertIn("second commit", out)

    def test_head_commit(self):
        out = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(gitutil.head_commit(self.repo), out)

    def test_committed_changes_compare_head_with_the_base(self):
        base = gitutil.merge_base(self.repo, "master")
        self.assertFalse(gitutil.committed_changes(self.repo, base))
        (self.repo / "a.txt").write_text("uncommitted\n")
        self.assertFalse(gitutil.committed_changes(self.repo, base))      # uncommitted edits do not count
        git(self.repo, "commit", "-q", "-am", "change")
        self.assertTrue(gitutil.committed_changes(self.repo, base))
        with self.assertRaises(gitutil.GitError):
            gitutil.committed_changes(self.repo, "not-a-commit")

    def test_status_lines_collapse_untracked_directories(self):
        (self.repo / "a.txt").write_text("edited\n")
        (self.repo / "notes").mkdir()
        (self.repo / "notes" / "one.md").write_text("x\n")
        (self.repo / "notes" / "two.md").write_text("y\n")
        (self.repo / "loose.txt").write_text("z\n")
        self.assertEqual(gitutil.status_lines(self.repo), [" M a.txt", "?? loose.txt", "?? notes/"])

    def test_status_lines_of_a_clean_tree_are_empty(self):
        self.assertEqual(gitutil.status_lines(self.repo), [])

    def test_status_lines_survive_the_users_git_config(self):
        git(self.repo, "config", "status.showUntrackedFiles", "no")
        git(self.repo, "config", "color.ui", "always")
        git(self.repo, "config", "status.branch", "true")
        self.assertEqual(gitutil.status_lines(self.repo), [])             # a clean tree: no `## master` line
        (self.repo / "заметка.md").write_text("x\n")
        self.assertEqual(gitutil.status_lines(self.repo), ["?? заметка.md"])
        self.assertEqual(gitutil.status_short(self.repo), "?? заметка.md\n")

    def test_untracked_files_mark_what_a_reviewer_should_skip(self):
        (self.repo / "src").mkdir()
        (self.repo / "src" / "new.py").write_text("print(1)\n")
        (self.repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0")
        (self.repo / "big.diff").write_text("x" * (gitutil.UNTRACKED_READ_LIMIT_BYTES + 1))
        os.symlink("a.txt", self.repo / "link.txt")
        files = {f.path: f for f in gitutil.untracked_files(self.repo)}
        self.assertEqual(sorted(files), ["big.diff", "link.txt", "logo.png", "src/new.py"])
        self.assertEqual(files["src/new.py"], gitutil.UntrackedFile("src/new.py", 9))
        self.assertEqual(files["logo.png"].skip, "binary")
        self.assertEqual(files["big.diff"].skip, "larger than 256 KB")
        self.assertEqual(files["link.txt"].skip, "symlink")

    def test_untracked_files_leave_out_ignored_ones(self):
        (self.repo / ".gitignore").write_text("build/\n")
        (self.repo / "build").mkdir()
        (self.repo / "build" / "out.o").write_text("junk\n")
        self.assertEqual([f.path for f in gitutil.untracked_files(self.repo)], [".gitignore"])

    def test_untracked_files_mark_a_nested_repository(self):
        nested = self.repo / "vendor"
        nested.mkdir()
        git(nested, "init", "-q")
        (nested / "lib.py").write_text("print(1)\n")
        self.assertEqual([(f.path, f.skip) for f in gitutil.untracked_files(self.repo)],
                         [("vendor/", "nested git repository")])

    def test_status_lines_keep_a_name_with_a_line_separator_whole(self):
        (self.repo / "a\u2028b.txt").write_text("x\n")
        self.assertEqual(gitutil.status_lines(self.repo), ["?? a\u2028b.txt"])

    def test_log_oneline_is_colour_free_under_color_ui_always(self):
        git(self.repo, "config", "color.ui", "always")
        short = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        out = gitutil.log_oneline(self.repo, "HEAD")
        self.assertNotIn("\x1b", out)
        self.assertTrue(out.startswith(short + " "), out)

    def test_log_oneline_is_one_line_per_commit_under_log_show_signature(self):
        if shutil.which("ssh-keygen") is None:
            self.skipTest("ssh-keygen is not installed")
        key = Path(self.tmp.name) / "signing-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, capture_output=True)
        git(self.repo, "config", "gpg.format", "ssh")
        git(self.repo, "config", "user.signingKey", str(key))
        (self.repo / "a.txt").write_text("signed\n")
        git(self.repo, "commit", "-q", "-S", "-am", "signed commit")
        git(self.repo, "config", "log.showSignature", "true")
        out = gitutil.log_oneline(self.repo, "HEAD")
        self.assertEqual(len(out.splitlines()), 2, out)                    # init and the signed commit


if __name__ == "__main__":
    unittest.main()
