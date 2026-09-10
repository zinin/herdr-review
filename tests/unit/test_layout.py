import unittest

from herdr_review.layout import SplitStep, fixer_split, plan_grid


class PlanGridTest(unittest.TestCase):
    def test_one_reviewer(self):
        steps, assign = plan_grid(1)
        self.assertEqual(steps, [SplitStep("orch", "right", 0.35, "row1")])
        self.assertEqual(assign, ["row1"])

    def test_two_reviewers_share_one_row(self):
        steps, assign = plan_grid(2)
        self.assertEqual(steps, [SplitStep("orch", "right", 0.35, "row1"), SplitStep("row1", "right", 0.5, "r1b")])
        self.assertEqual(assign, ["row1", "r1b"])

    def test_three_reviewers_two_rows(self):
        steps, assign = plan_grid(3)
        self.assertEqual(steps, [
            SplitStep("orch", "right", 0.35, "row1"),
            SplitStep("row1", "down", 0.5, "row2"),
            SplitStep("row1", "right", 0.5, "r1b"),
        ])
        self.assertEqual(assign, ["row1", "r1b", "row2"])

    def test_five_reviewers_three_equal_rows(self):
        steps, assign = plan_grid(5)
        ratios = [s.ratio for s in steps if s.direction == "down"]
        self.assertEqual([round(r, 3) for r in ratios], [0.333, 0.5])
        self.assertEqual([s.target for s in steps if s.direction == "down"], ["row1", "row2"])
        self.assertEqual(assign, ["row1", "r1b", "row2", "r2b", "row3"])
        self.assertEqual(len(steps), 1 + 2 + 2)

    def test_every_target_exists_before_use(self):
        for n in range(1, 9):
            steps, assign = plan_grid(n)
            known = {"orch"}
            for s in steps:
                self.assertIn(s.target, known, f"n={n}: {s}")
                self.assertNotIn(s.result, known)
                known.add(s.result)
            self.assertEqual(len(assign), n)
            self.assertEqual(len(set(assign)), n)
            for a in assign:
                self.assertIn(a, known)

    def test_invalid_n(self):
        with self.assertRaises(ValueError):
            plan_grid(0)

    def test_fixer_split(self):
        self.assertEqual(fixer_split(), SplitStep("orch", "down", 0.5, "fixer"))


if __name__ == "__main__":
    unittest.main()
