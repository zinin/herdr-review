"""Pure planning of the grid layout (herdr pane splits) for N reviewers."""
from __future__ import annotations

import math
from dataclasses import dataclass

ORCH_SHARE = 0.35  # width kept by the orchestrator pane when the reviewer column is split off


@dataclass(frozen=True)
class SplitStep:
    target: str      # symbolic name of the pane to split ("orch" = orchestrator pane)
    direction: str   # "right" | "down"
    ratio: float     # share kept by the existing (target) pane
    result: str      # symbolic name of the new pane


def plan_grid(n: int) -> tuple[list[SplitStep], list[str]]:
    """Return (split steps, pane per reviewer) for n reviewers in the orchestrator's tab."""
    if n < 1:
        raise ValueError("plan_grid needs at least one reviewer")
    rows = math.ceil(n / 2)
    steps = [SplitStep("orch", "right", ORCH_SHARE, "row1")]
    for i in range(1, rows):
        # split the last row so that the rows come out equal: keep 1/(rows-i+1) for it
        steps.append(SplitStep(f"row{i}", "down", 1 / (rows - i + 1), f"row{i + 1}"))
    assign: list[str] = []
    for r in range(1, rows + 1):
        assign.append(f"row{r}")
        if len(assign) < n:
            steps.append(SplitStep(f"row{r}", "right", 0.5, f"r{r}b"))
            assign.append(f"r{r}b")
    return steps, assign


def fixer_split() -> SplitStep:
    return SplitStep("orch", "down", 0.5, "fixer")
