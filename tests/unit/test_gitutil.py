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


def write_bytes_name(test: unittest.TestCase, repo: Path, name: bytes, content: bytes) -> Path:
    """Create <repo>/<name> through a bytes path, for a name that is not UTF-8; skip <test> where the filesystem
    refuses such a name."""
    try:
        with open(os.path.join(os.fsencode(repo), name), "wb") as f:
            f.write(content)
    except OSError as e:
        test.skipTest(f"the filesystem refuses the name {name!r}: {e}")
    return repo / os.fsdecode(name)


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

    def test_tree_hash_reads_untracked_files_itself_and_runs_no_clean_filter(self):
        (self.repo / "new.dat").write_text("aaa\n")
        blob = subprocess.run(["git", "-C", str(self.repo), "hash-object", "new.dat"], capture_output=True, text=True, check=True).stdout.strip()
        # git's own blob id: a run launched while git hashed these files sees no drift
        self.assertEqual(gitutil._untracked_meta(self.repo, ["new.dat"]), f"new.dat\0{blob}")
        ran = Path(self.tmp.name) / "clean-filter-ran"
        (self.repo / ".git" / "info" / "attributes").write_text("*.dat filter=probe\n")
        git(self.repo, "config", "filter.probe.clean", f"touch '{ran}'; cat")
        gitutil.tree_hash(self.repo)
        self.assertFalse(ran.exists())

    def test_tree_hash_survives_an_untracked_file_it_cannot_read(self):
        locked = self.repo / "secret.txt"                   # like a root-owned 0600 file from a Docker bind mount
        locked.write_text("s3cret\n")
        locked.chmod(0)
        try:
            first = gitutil.tree_hash(self.repo)            # root can read it: hashed by content, still the same twice
            self.assertEqual(gitutil.tree_hash(self.repo), first)
        finally:
            locked.chmod(0o644)

    def test_tree_hash_reads_a_name_that_starts_with_a_quote(self):
        path = self.repo / '"note".txt'                     # `hash-object --stdin-paths` C-unquoted it into `note`
        path.write_text("aaa\n")
        stamp = path.stat().st_mtime_ns
        h0 = gitutil.tree_hash(self.repo)
        path.write_text("bbb\n")                            # same size
        os.utime(path, ns=(stamp, stamp))                   # and the same mtime: only its content tells
        self.assertNotEqual(gitutil.tree_hash(self.repo), h0)

    def test_tree_hash_survives_a_file_deleted_between_its_lstat_and_its_read(self):
        path = self.repo / "new.txt"
        path.write_text("aaa\n")
        real = os.lstat

        def lstat_then_delete(p, *args, **kwargs):
            st = real(p, *args, **kwargs)
            if p == path:
                path.unlink()
            return st

        with mock.patch.object(gitutil.os, "lstat", lstat_then_delete):
            self.assertNotEqual(gitutil.tree_hash(self.repo), "")

    def test_tree_hash_survives_a_file_that_vanished(self):
        (self.repo / "new.txt").write_text("aaa\n")
        real = gitutil._untracked

        def with_a_ghost(repo):
            return sorted([*real(repo), "gone.txt"])

        with mock.patch.object(gitutil, "_untracked", with_a_ghost):
            self.assertNotEqual(gitutil.tree_hash(self.repo), "")

    def test_tree_hash_survives_a_nested_repository_and_a_link_to_a_directory(self):
        nested = self.repo / "vendor"                       # git lists it as one `vendor/` entry
        nested.mkdir()
        git(nested, "init", "-q")
        (nested / "lib.py").write_text("print(1)\n")
        outside = Path(self.tmp.name) / "shared"
        outside.mkdir()
        os.symlink(outside, self.repo / "shared")
        first = gitutil.tree_hash(self.repo)
        self.assertEqual(gitutil.tree_hash(self.repo), first)

    def test_tree_hash_runs_no_external_diff(self):
        noisy = Path(self.tmp.name) / "noisy.sh"            # a new line on every call, like git's temp paths
        noisy.write_text('#!/bin/sh\nn=$(cat "$0.count" 2>/dev/null || echo 0)\nn=$((n + 1))\necho "$n" > "$0.count"\necho "external diff call $n"\n')
        noisy.chmod(0o755)
        git(self.repo, "config", "diff.external", str(noisy))
        (self.repo / "a.txt").write_text("edited\n")
        self.assertEqual(gitutil.tree_hash(self.repo), gitutil.tree_hash(self.repo))

    def test_tree_hash_changes_when_a_commit_lands_on_a_clean_tree(self):
        h0 = gitutil.tree_hash(self.repo)
        (self.repo / "a.txt").write_text("committed\n")
        git(self.repo, "commit", "-q", "-am", "reviewer commit")
        self.assertNotEqual(gitutil.tree_hash(self.repo), h0)

    def short(self, rev: str = "HEAD") -> str:
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--short", rev], capture_output=True, text=True, check=True).stdout.strip()

    def test_commit_hashes(self):
        git(self.repo, "switch", "-q", "-c", "feat")
        (self.repo / "a.txt").write_text("two\n")
        git(self.repo, "commit", "-q", "-am", "second commit")
        self.assertEqual(gitutil.commit_hashes(self.repo, "master..HEAD"), [self.short()])
        self.assertEqual(gitutil.commit_hashes(self.repo, "HEAD..HEAD"), [])

    def test_is_ancestor_tells_whether_head_still_holds_a_commit(self):
        first = gitutil.head_commit(self.repo)
        (self.repo / "a.txt").write_text("two\n")
        git(self.repo, "commit", "-q", "-am", "second")
        second = gitutil.head_commit(self.repo)
        self.assertTrue(gitutil.is_ancestor(self.repo, first))
        self.assertTrue(gitutil.is_ancestor(self.repo, second))           # HEAD itself
        git(self.repo, "switch", "-q", "-c", "side", first)
        (self.repo / "b.txt").write_text("side\n")
        git(self.repo, "add", "b.txt")
        git(self.repo, "commit", "-q", "-m", "side")
        self.assertFalse(gitutil.is_ancestor(self.repo, second))          # another line of history
        for bad in ("0" * 40, "-x"):
            with self.subTest(commit=bad), self.assertRaises(gitutil.GitError):
                gitutil.is_ancestor(self.repo, bad)

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

    def test_committed_changes_ignore_an_external_diff_that_calls_everything_equal(self):
        base = gitutil.merge_base(self.repo, "master")
        same = Path(self.tmp.name) / "same.sh"
        same.write_text("#!/bin/sh\nexit 0\n")
        same.chmod(0o755)
        git(self.repo, "config", "diff.external", str(same))
        git(self.repo, "config", "diff.trustExitCode", "true")          # git diff --quiet would take its word
        self.assertFalse(gitutil.committed_changes(self.repo, base))      # equal trees
        (self.repo / "a.txt").write_text("change\n")
        git(self.repo, "commit", "-q", "-am", "change")
        self.assertTrue(gitutil.committed_changes(self.repo, base))

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

    def test_untracked_files_keep_a_name_that_is_not_utf8_or_holds_a_cr(self):
        write_bytes_name(self, self.repo, b"caf\xe9.py", b"aaa\n")        # a legacy-encoded name
        (self.repo / "Icon\r").write_bytes(b"icon")                        # macOS's folder-icon file
        self.assertEqual({f.path: f.size for f in gitutil.untracked_files(self.repo)},
                         {os.fsdecode(b"caf\xe9.py"): 4, "Icon\r": 4})

    def test_tree_hash_reads_a_name_that_is_not_utf8(self):
        path = write_bytes_name(self, self.repo, b"caf\xe9.py", b"aaa\n")
        stamp = path.stat().st_mtime_ns
        h0 = gitutil.tree_hash(self.repo)
        path.write_bytes(b"bbb\n")                                        # same size
        os.utime(path, ns=(stamp, stamp))                                   # and the same mtime
        self.assertNotEqual(gitutil.tree_hash(self.repo), h0)

    def test_quote_path_quotes_a_name_as_git_does_only_when_it_is_not_utf8_or_holds_a_control_character(self):
        self.assertEqual(gitutil.quote_path(os.fsdecode(b"caf\xe9.py")), '"caf\\351.py"')
        self.assertEqual(gitutil.quote_path("Icon\r"), '"Icon\\r"')
        self.assertEqual(gitutil.quote_path('a\tb\nc"d\\e\x01f\x7fg'), '"a\\tb\\nc\\"d\\\\e\\001f\\177g"')
        for name in ("заметка.md", "my file.py", 'q"uote.txt', "back\\slash.txt", "a\u2028b.txt"):
            with self.subTest(name=name):
                self.assertEqual(gitutil.quote_path(name), name)

    def test_status_lines_keep_a_name_with_a_line_separator_whole(self):
        (self.repo / "a\u2028b.txt").write_text("x\n")
        self.assertEqual(gitutil.status_lines(self.repo), ["?? a\u2028b.txt"])

    def test_status_paths_drop_the_quotes_and_split_a_rename(self):
        (self.repo / "a.txt").write_text("edited\n")
        git(self.repo, "mv", "a.txt", "b -> c.txt")                        # the new name holds the arrow itself
        (self.repo / "my dir").mkdir()
        (self.repo / "my dir" / "f.txt").write_text("x\n")
        (self.repo / 'q"uote.txt').write_text("y\n")
        lines = gitutil.status_lines(self.repo)
        self.assertEqual(lines, ['RM a.txt -> "b -> c.txt"', '?? "my dir/"', '?? "q\\"uote.txt"'])
        self.assertEqual([gitutil.status_paths(line) for line in lines],
                         [["a.txt", "b -> c.txt"], ["my dir/"], ['q\\"uote.txt']])    # an escape inside stays

    def test_commit_hashes_are_colour_free_under_color_ui_always(self):
        git(self.repo, "config", "color.ui", "always")
        self.assertEqual(gitutil.commit_hashes(self.repo, "HEAD"), [self.short()])

    def test_commit_hashes_are_one_per_commit_under_log_show_signature(self):
        if shutil.which("ssh-keygen") is None:
            self.skipTest("ssh-keygen is not installed")
        key = Path(self.tmp.name) / "signing-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, capture_output=True)
        git(self.repo, "config", "gpg.format", "ssh")
        git(self.repo, "config", "gpg.ssh.program", "ssh-keygen")         # over a global one such as 1Password's op-ssh-sign
        git(self.repo, "config", "user.signingKey", str(key))
        (self.repo / "a.txt").write_text("signed\n")
        signed = subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-S", "-am", "signed commit"],
                                capture_output=True, text=True)
        if signed.returncode != 0:                                         # git < 2.34 or OpenSSH < 8.1 cannot sign with SSH
            self.skipTest("SSH signing is unavailable: " + signed.stderr.strip())
        git(self.repo, "config", "log.showSignature", "true")
        self.assertEqual(gitutil.commit_hashes(self.repo, "HEAD"), [self.short(), self.short("HEAD~1")])   # the signed commit and init


if __name__ == "__main__":
    unittest.main()
