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

#: `ledger.py` is the one module in this package that owns a file, and it is the
#: exception on purpose: the judgements the user has already made have to live
#: somewhere. It is excluded from the import check below and constrained instead by
#: the two tests that follow -- nothing in the pure path may import it, so the pairs
#: it stores can only reach the planner as a plain argument passed by the command
#: layer.
IO_OWNING_MODULES = ("ledger.py",)


def _imports_of(module: str) -> set[str]:
    tree = ast.parse((DEDUPE_PACKAGE / module).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
    return imported


PURE_MODULES = sorted(
    p.name for p in DEDUPE_PACKAGE.glob("*.py") if p.name not in IO_OWNING_MODULES
)


@pytest.mark.parametrize("module", PURE_MODULES)
def test_the_planner_cannot_reach_the_io_shell(module):
    for name in _imports_of(module):
        head = name.split(".")[0]
        assert head not in FORBIDDEN_IMPORTS, f"{module} imports {name!r}"


@pytest.mark.parametrize("module", PURE_MODULES)
def test_nothing_in_the_pure_path_reads_the_ledger_file(module):
    """The planner takes asserted-distinct pairs as a value, never as a file.

    If `planner.py` ever imported `ledger`, every suppression test above would stop
    proving what it claims: the seam would depend on the state directory rather than
    on its arguments.
    """
    for name in _imports_of(module):
        assert "ledger" not in name.split("."), f"{module} imports {name!r}"


def test_the_ledger_is_the_only_module_in_the_package_that_owns_a_file():
    """A new I/O-owning module must be a deliberate decision, not a slow drift."""
    io_owning = {
        p.name
        for p in DEDUPE_PACKAGE.glob("*.py")
        if {"pathlib", "json"} & {n.split(".")[0] for n in _imports_of(p.name)}
    }
    assert io_owning == set(IO_OWNING_MODULES)


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
