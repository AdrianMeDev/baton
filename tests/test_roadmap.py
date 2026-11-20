from pathlib import Path

import pytest

from baton.roadmap import (
    DEFAULT_ANSWER,
    RoadmapError,
    append_changelog,
    assemble_roadmap,
    find_roadmap,
    load_roadmap,
    milestone_text,
    next_milestone,
    parse_questions,
    parse_roadmap,
    resolve_roadmap,
    save_roadmap,
    section_text,
    set_milestone_status,
    validate_roadmap,
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


VALID_ROADMAP = """\
# Roadmap: Todo
Updated: 1999-01-01

## Vision
A todo app.

## Milestones

### M0 [ ] Walking skeleton
- **Goal:** One page works.
- **Includes:** scaffold
- **Done when:** the page renders
- **Depends on:** -
- **Size:** S, ~4 commits

### M1 [ ] Tasks
- **Goal:** Add tasks.
- **Includes:** api, ui
- **Done when:** a task can be added
- **Depends on:** M0
- **Size:** M, ~6 commits

## Definition of done (every milestone)
- Builds, tests green

## Changelog
- 2026-11-19: roadmap created
"""


def test_valid_roadmap_has_no_errors_and_round_trips():
    assert validate_roadmap(VALID_ROADMAP) == []
    assert [m.id for m in parse_roadmap(VALID_ROADMAP).milestones] == ["M0", "M1"]


def test_roadmap_may_start_at_m1():
    text = VALID_ROADMAP.replace("### M1 [ ] Tasks", "### M2 [ ] Tasks")
    text = text.replace("### M0 [ ]", "### M1 [ ]").replace("Depends on:** M0", "Depends on:** M1")

    assert validate_roadmap(text) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("- **Goal:** One page works.\n", "", "M0: missing '**Goal:**'"),
        ("- **Done when:** a task can be added\n", "", "M1: missing '**Done when:**'"),
        ("S, ~4 commits", "~4 commits", "M0: '**Size:**' must start with S, M or L"),
        ("### M0 [ ]", "### M0 [x]", "M0: must be open '[ ]', found done"),
        (
            "~6 commits",
            "~12 commits",
            "M1: a milestone is 3-8 commits, found ~12; split or merge it",
        ),
        ("~4 commits", "~1 commits", "M0: a milestone is 3-8 commits, found ~1; split or merge it"),
        (
            "## Definition of done (every milestone)",
            "## Other",
            "missing '## Definition of done' section",
        ),
        ("# Roadmap: Todo", "# Something", "missing '# Roadmap: <name>' heading"),
        (
            "### M1 [ ] Tasks",
            "### M3 [ ] Tasks",
            "milestones must be numbered M0.. or M1.. in order, found [0, 3]",
        ),
    ],
)
def test_invalid_roadmaps_report_specific_errors(old: str, new: str, expected: str):
    text = VALID_ROADMAP.replace(old, new)

    assert expected in validate_roadmap(text)


def test_roadmap_without_milestones_is_reported_as_unparsable():
    errors = validate_roadmap("# Roadmap: x\n\nnothing\n")

    assert errors == ["roadmap does not parse: missing '## Milestones' section"]


def test_assemble_roadmap_drops_prose_and_fence_and_sets_the_date():
    reply = "Here you go:\n\n```markdown\n" + VALID_ROADMAP + "```\nEnjoy.\n"

    text = assemble_roadmap(reply, "2026-11-19")

    assert text.startswith("# Roadmap: Todo\nUpdated: 2026-11-19\n\n## Vision")
    assert "1999-01-01" not in text and "Enjoy" not in text and "```" not in text
    assert validate_roadmap(text) == []


def test_assemble_roadmap_inserts_missing_date():
    text = assemble_roadmap(VALID_ROADMAP.replace("Updated: 1999-01-01\n", ""), "2026-11-19")

    assert text.splitlines()[:2] == ["# Roadmap: Todo", "Updated: 2026-11-19"]


def test_assemble_roadmap_without_heading_is_left_for_validation():
    text = assemble_roadmap("I cannot do that", "2026-11-19")

    assert text == "I cannot do that\n"
    assert validate_roadmap(text)[0].startswith("roadmap does not parse")


def test_parse_questions_reads_defaults_and_fills_missing_ones():
    reply = (
        "Some thinking\n"
        "BATON-QUESTION: Who uses it? | default: just me\n"
        "BATON-QUESTION: Which stack?\n"
        "  BATON-QUESTION:   Hosting? |  default:  none \n"
        "see `BATON-QUESTION: x` inline\n"
    )

    questions = parse_questions(reply)

    assert [(q.text, q.default) for q in questions] == [
        ("Who uses it?", "just me"),
        ("Which stack?", DEFAULT_ANSWER),
        ("Hosting?", "none"),
    ]


def test_parse_questions_caps_at_six_and_accepts_none():
    reply = "\n".join(f"BATON-QUESTION: q{n} | default: a{n}" for n in range(9))

    assert [q.text for q in parse_questions(reply)] == [f"q{n}" for n in range(6)]
    assert parse_questions("none") == []


def test_resolve_roadmap_default_and_explicit(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ROADMAP.md").write_text("# Roadmap: a\n")
    (tmp_path / "docs" / "AUDIT.md").write_text("# Audit: a\n")
    assert resolve_roadmap(tmp_path) == tmp_path / "docs" / "ROADMAP.md"
    assert resolve_roadmap(tmp_path, "docs/AUDIT.md") == tmp_path / "docs" / "AUDIT.md"
    assert resolve_roadmap(tmp_path, "./docs/AUDIT.md") == tmp_path / "docs" / "AUDIT.md"
    assert resolve_roadmap(tmp_path, "docs/NOPE.md") is None
    assert resolve_roadmap(tmp_path, "docs") is None  # a directory is not a roadmap


def test_resolve_roadmap_without_any_roadmap(tmp_path: Path) -> None:
    assert resolve_roadmap(tmp_path) is None


def test_resolve_roadmap_rejects_paths_leaving_the_checkout(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Roadmap: x\n")
    (root / "link.md").symlink_to(outside)
    (root / "dirlink").symlink_to(tmp_path)
    for bad in (
        str(outside),
        "../outside.md",
        "docs/../../outside.md",
        "link.md",
        "dirlink/outside.md",
    ):
        with pytest.raises(RoadmapError):
            resolve_roadmap(root, bad)


def test_resolve_roadmap_accepts_a_symlink_inside_the_checkout(tmp_path: Path) -> None:
    (tmp_path / "real.md").write_text("# Roadmap: x\n")
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")
    assert resolve_roadmap(tmp_path, "alias.md") == tmp_path / "alias.md"
