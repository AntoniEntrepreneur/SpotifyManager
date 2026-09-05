"""The plan's printed form, and the boundary that keeps the planner pure.

Planning must cost nothing: no request, no file, no clock. The cheapest way to keep
that true over time is to check that the package cannot reach the I/O shell at all.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from spotify_manager.commands.dedupe_cmd import format_plan
from spotify_manager.dedupe.models import snapshot_from_raw
from spotify_manager.dedupe.planner import plan

DEDUPE_PACKAGE = Path(__file__).parents[1] / "src" / "spotify_manager" / "dedupe"
FIXTURE = Path(__file__).parent / "fixtures" / "library_snapshot.redacted.json"


@pytest.fixture(scope="module")
def real_plan():
    return plan(snapshot_from_raw(json.loads(FIXTURE.read_text(encoding="utf-8"))))


FORBIDDEN_IMPORTS = ("infra", "requests", "spotipy", "urllib", "pathlib", "time", "datetime")

#: `execute.py` is the one module in this package that is deliberately impure: it
#: writes the restore file and issues the deletions, which is exactly the work that
#: cannot be done without a clock, a path and the API. It is exempt by name rather
#: than by a looser rule, so that adding a *second* impure module to a package whose
#: whole value is being pure has to be an explicit decision.
IMPURE_BY_DESIGN = ("execute.py",)


@pytest.mark.parametrize(
    "module",
    sorted(p.name for p in DEDUPE_PACKAGE.glob("*.py") if p.name not in IMPURE_BY_DESIGN),
)
def test_the_planner_cannot_reach_the_io_shell(module):
    tree = ast.parse((DEDUPE_PACKAGE / module).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
    for name in imported:
        head = name.split(".")[0]
        assert head not in FORBIDDEN_IMPORTS, f"{module} imports {name!r}"


def test_the_printed_plan_shows_every_group_with_its_reasoning(real_plan):
    text = format_plan(real_plan)

    assert f"Scanned {real_plan.total_albums_scanned} albums" in text
    assert "Excluded from analysis: 24 (compilation)." in text
    assert text.count("Group ") == len(real_plan.groups)

    for group in real_plan.groups:
        assert f'key="{group.key.normalized_title}"' in text
        for member in group.members:
            assert member.album.id in text
    assert "[KEEP]" in text
    assert "[remove]" in text
    assert "Donda (Deluxe)" in text
    assert "ignored decorations: Deluxe" in text


def test_an_empty_plan_says_so():
    text = format_plan(plan(snapshot_from_raw({"fetched_at": "", "albums": []})))
    assert "No duplicate editions found." in text
    assert "Scanned 0 albums" in text
