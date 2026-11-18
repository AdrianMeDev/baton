from pathlib import Path

import pytest

from baton.roadmap import (
    RoadmapError,
    append_changelog,
    find_roadmap,
    load_roadmap,
    milestone_text,
    next_milestone,
    parse_roadmap,
    save_roadmap,
    section_text,
    set_milestone_status,
)

FIXTURES = Path(__file__).parent / "fixtures" / "roadmaps"
REPO = Path(__file__).parent.parent


def read(name: str) -> str:
    with (FIXTURES / name).open(encoding="utf-8", newline="") as f:
        return f.read()


def test_basic_roadmap_fields():
    roadmap = load_roadmap(FIXTURES / "basic.md")
    assert roadmap.title == "Demo"
    assert [m.id for m in roadmap.milestones] == ["M0", "M1", "M2", "M3"]
    assert [m.status for m in roadmap.milestones] == ["done", "pending", "open", "open"]
    m0, m1, m2, m3 = roadmap.milestones
    assert (m0.name, m0.goal, m0.includes, m0.done_when) == ("Skeleton", "Runs.", "a, b", "it runs")
    assert m0.depends_on == () and m0.size == "S"
    assert m1.goal == "Parse things\nand more."
    assert m2.depends_on == ("M0", "M1") and m2.size == "L"
    assert m3.size is None
    assert roadmap.changelog == ("2026-11-18: roadmap created",)


def test_project_roadmap_parses():
    roadmap = load_roadmap(REPO / "docs" / "ROADMAP.md")
    assert roadmap.get("M0") is not None
    assert all(m.size in ("S", "M", "L") for m in roadmap.milestones)
    assert roadmap.changelog


@pytest.mark.parametrize(
    ("name", "fragment"),
    [
        ("bad_marker.md", "invalid milestone heading"),
        ("unknown_dep.md", "unknown milestone M9"),
        ("duplicate.md", "duplicate milestone M0"),
    ],
)
def test_malformed_roadmaps(name: str, fragment: str):
    with pytest.raises(RoadmapError, match=fragment) as exc:
        parse_roadmap(read(name))
    assert exc.value.line is not None


def test_missing_sections():
    with pytest.raises(RoadmapError, match="Milestones"):
        parse_roadmap("# Roadmap: x\n")
    with pytest.raises(RoadmapError, match="no milestones"):
        parse_roadmap("## Milestones\n\ntext\n")


def test_next_milestone_respects_dependencies():
    roadmap = parse_roadmap(read("basic.md"))
    # M2 is first open but needs M1 (pending); M3 only needs M0
    assert next_milestone(roadmap).id == "M3"
    text = set_milestone_status(read("basic.md"), "M1", "done")
    assert next_milestone(parse_roadmap(text)).id == "M2"


def test_next_milestone_none_when_all_done_or_blocked():
    text = read("basic.md")
    text = set_milestone_status(text, "M3", "done")
    assert next_milestone(parse_roadmap(text)) is None


def test_set_status_changes_only_the_heading():
    text = read("basic.md")
    out = set_milestone_status(text, "M2", "pending")
    assert out.replace("### M2 [~] Blocked", "### M2 [ ] Blocked") == text
    assert parse_roadmap(out).get("M2").status == "pending"
    assert set_milestone_status(text, "M1", "pending") == text  # no-op is byte-identical


def test_set_status_unknown_milestone():
    with pytest.raises(RoadmapError, match="no milestone M9"):
        set_milestone_status(read("basic.md"), "M9", "done")


def test_append_changelog_goes_to_the_end_of_the_section():
    out = append_changelog(read("basic.md"), "2026-11-19: M1 done")
    assert out.endswith("- 2026-11-18: roadmap created\n- 2026-11-19: M1 done\n")
    assert parse_roadmap(out).changelog[-1] == "2026-11-19: M1 done"


def test_append_changelog_creates_section():
    out = append_changelog("## Milestones\n\n### M0 [ ] A\n", "created")
    assert out.endswith("\n\n## Changelog\n- created\n")


def test_crlf_is_preserved():
    text = read("crlf.md")
    out = set_milestone_status(text, "M0", "done")
    out = append_changelog(out, "M0 done")
    assert "\n" not in out.replace("\r\n", "")
    assert "### M0 [x] First\r\n" in out
    assert out.endswith("- created\r\n- M0 done\r\n")


@pytest.mark.parametrize("name", ["basic.md", "crlf.md"])
def test_save_round_trip_is_byte_identical(tmp_path: Path, name: str):
    path = tmp_path / "ROADMAP.md"
    save_roadmap(path, read(name))
    assert path.read_bytes() == (FIXTURES / name).read_bytes()


def test_save_rejects_invalid_text(tmp_path: Path):
    with pytest.raises(RoadmapError):
        save_roadmap(tmp_path / "ROADMAP.md", "nothing")
    assert not (tmp_path / "ROADMAP.md").exists()


def test_find_roadmap(tmp_path: Path):
    assert find_roadmap(tmp_path) is None
    (tmp_path / "ROADMAP.md").write_text("x")
    assert find_roadmap(tmp_path) == tmp_path / "ROADMAP.md"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ROADMAP.md").write_text("x")
    assert find_roadmap(tmp_path) == tmp_path / "docs" / "ROADMAP.md"


def test_section_text_matches_heading_prefix():
    text = read("planning.md")
    assert section_text(text, "Decisions") == "## Decisions\n- D1: widgets are immutable."
    assert section_text(text, "Definition of done") == (
        "## Definition of done (every milestone)\n- tests green, lint clean"
    )
    assert section_text(text, "Risks") is None


def test_milestone_text_is_the_raw_section():
    text = read("planning.md")
    milestone = parse_roadmap(text).get("M2")
    assert milestone is not None
    assert milestone_text(text, milestone) == (
        "### M2 [ ] Gadgets\n- **Goal:** Gadgets exist.\n- **Depends on:** M1\n"
        "- **Size:** L, ~6 commits"
    )
