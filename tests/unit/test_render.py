import tempfile
import unittest
from pathlib import Path

from herdr_review.render import RenderError, placeholders, render, render_file


class RenderTest(unittest.TestCase):
    def test_substitutes_uppercase_placeholders(self):
        self.assertEqual(render("Read {FILE} now, {RUN_DIR}/x", {"FILE": "/a.md", "RUN_DIR": "/r"}), "Read /a.md now, /r/x")

    def test_leaves_json_and_lowercase_braces_alone(self):
        tpl = '{"agents": {"x": 1}} and {name} and {Mixed}'
        self.assertEqual(render(tpl, {}), tpl)

    def test_missing_placeholder_raises_with_names(self):
        with self.assertRaises(RenderError) as ctx:
            render("{A} {B} {A}", {"A": "1"})
        self.assertIn("B", str(ctx.exception))
        self.assertNotIn("A", str(ctx.exception).split(":")[-1])

    def test_extra_values_are_ignored(self):
        self.assertEqual(render("{A}", {"A": "1", "B": "2"}), "1")

    def test_values_are_stringified(self):
        self.assertEqual(render("{N}", {"N": 5}), "5")

    def test_placeholders_lists_unique_names(self):
        self.assertEqual(placeholders("{A} {B} {A} {c}"), {"A", "B"})

    def test_render_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.md"
            p.write_text("hello {WHO}\n", encoding="utf-8")
            self.assertEqual(render_file(p, {"WHO": "world"}), "hello world\n")


if __name__ == "__main__":
    unittest.main()
