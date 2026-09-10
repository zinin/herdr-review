import importlib.machinery
import importlib.util
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

from herdr_review import PACKAGE_ROOT, __version__

ENTRY = PACKAGE_ROOT / "bin" / "herdr-review"


def load_entry_point():
    """Import bin/herdr-review without running it: it has no .py suffix and no package."""
    loader = importlib.machinery.SourceFileLoader("herdr_review_entry", str(ENTRY))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class EntryPointTest(unittest.TestCase):
    def setUp(self):
        self.entry = load_entry_point()

    def test_the_entry_point_runs_on_this_interpreter(self):
        p = subprocess.run([sys.executable, str(ENTRY), "--version"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), f"herdr-review {__version__}")

    def test_a_new_enough_interpreter_passes_the_check(self):
        self.assertIsNone(self.entry.check_requirements((3, 11, 0, "final", 0)))
        self.assertIsNone(self.entry.check_requirements((3, 14, 4, "final", 0)))

    def test_an_old_interpreter_is_reported(self):
        message = self.entry.check_requirements((3, 10, 7, "final", 0))
        self.assertIn("3.11", message)
        self.assertIn("3.10.7", message)
        self.assertIn("Requirements", message)
        self.assertNotIn("\n", message)

    def test_a_failed_requirement_is_one_stderr_line_and_exit_1(self):
        err = io.StringIO()
        with mock.patch.object(self.entry, "check_requirements", return_value="nope"), redirect_stderr(err):
            code = self.entry.run()
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "nope\n")

    def test_missing_pyyaml_is_reported_and_exits_1(self):
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"yaml": None}):     # `import yaml` then raises ImportError
            message = self.entry.check_requirements()
            with redirect_stderr(err):
                code = self.entry.run()
        self.assertIn("PyYAML", message)
        self.assertIn("Requirements", message)
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue().splitlines(), [message])


if __name__ == "__main__":
    unittest.main()
