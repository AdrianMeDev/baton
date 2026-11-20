import json
import os
import subprocess
from pathlib import Path

import pytest

from baton.audit import (
    MAX_FILE_CHARS,
    AuditError,
    AuditFinding,
    InventoryFile,
    build_inventory,
    next_batch,
    parse_audit_reply,
    parse_base,
    parse_findings,
    parse_inventory,
    parse_title,
    read_file,
    record_batch,
    render_audit,
    set_file_status,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    files = {
        "README.md": "# hi\n",
        "src/app.py": "print('hi')\n",
        "src/util/helpers.py": "x = 1\n",
        "src/util/more.py": "y = 2\n",
        ".env": "TOKEN=abc\n",
        ".env.local": "TOKEN=def\n",
        "keys/server.pem": "-----BEGIN-----\n",
        "keys/id_rsa": "private\n",
        "uv.lock": "lock\n",
        "package-lock.json": "{}\n",
        "big.txt": "a" * (MAX_FILE_CHARS + 1),
        "ünï.txt": "ünïcödé\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0")
    (tmp_path / "latin.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    os.symlink("README.md", tmp_path / "link.md")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def by_path(files: list[InventoryFile]) -> dict[str, InventoryFile]:
    return {f.path: f for f in files}


def test_every_tracked_file_appears_exactly_once(repo: Path) -> None:
    files = build_inventory(repo, "HEAD")
    tracked = git(repo, "-c", "core.quotePath=false", "ls-files").splitlines()
    assert sorted(f.path for f in files) == sorted(tracked)


def test_skip_reasons(repo: Path) -> None:
    files = by_path(build_inventory(repo, "HEAD"))
    expected = {
        ".env": "possible secret",
        ".env.local": "possible secret",
        "keys/server.pem": "possible secret",
        "keys/id_rsa": "possible secret",
        "uv.lock": "lockfile",
        "package-lock.json": "lockfile",
        "big.txt": "too large",
        "logo.png": "binary or not UTF-8",
        "latin.txt": "binary or not UTF-8",
        "link.md": "symlink",
    }
    for path, reason in expected.items():
        assert files[path].status == "skipped", path
        assert files[path].note == reason, path
    for path in ("README.md", "src/app.py", "src/util/helpers.py", "ünï.txt"):
        assert files[path] == InventoryFile(path)


def test_sorted_by_folder_then_path(repo: Path) -> None:
    files = build_inventory(repo, "HEAD")
    assert [f.folder for f in files] == sorted(f.folder for f in files)
    assert files[0].folder == "./"


def test_inventory_reads_the_base_commit_not_the_work_tree(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "new.py").write_text("z = 3\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "more")
    assert "new.py" not in by_path(build_inventory(repo, base))
    assert "new.py" in by_path(build_inventory(repo, "HEAD"))


def test_unknown_base_raises(repo: Path) -> None:
    with pytest.raises(Exception, match="ls-tree"):
        build_inventory(repo, "does-not-exist")


def test_read_file_reads_from_base(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("changed\n")
    assert read_file(repo, base, "README.md") == "# hi\n"
    with pytest.raises(AuditError, match="UTF-8"):
        read_file(repo, base, "logo.png")


def test_round_trip(repo: Path) -> None:
    files = build_inventory(repo, "HEAD")
    text = render_audit(files, base="abc1234", date="2026-11-20", title="demo")
    assert parse_inventory(text) == files
    assert parse_base(text) == "abc1234"
    assert parse_title(text) == "demo"
    assert "## Scope" in text
    for folder in {f.folder for f in files}:
        assert f"### {folder} [" in text


def test_folder_markers_follow_the_files() -> None:
    files = [
        InventoryFile("a/one.py"),
        InventoryFile("a/two.py"),
        InventoryFile("b/lock.lock", "skipped", "lockfile"),
        InventoryFile("b/x.py"),
    ]
    text = render_audit(files, base="abc", date="2026-11-20", title="t")
    assert "### a/ [ ]" in text
    assert "### b/ [ ]" in text
    text = set_file_status(text, ["a/one.py"], "done", "2 findings")
    assert "### a/ [~]" in text
    assert "- [x] `a/one.py` – 2 findings" in text
    text = set_file_status(text, ["a/two.py"], "done", "0 findings")
    text = set_file_status(text, ["b/x.py"], "done", "0 findings")
    assert "### a/ [x]" in text
    assert "### b/ [x]" in text
    assert parse_inventory(text)[2] == InventoryFile("b/lock.lock", "skipped", "lockfile")


def test_set_file_status_keeps_the_other_sections() -> None:
    files = [InventoryFile("a.py"), InventoryFile("b.py")]
    text = render_audit(files, base="abc", date="2026-11-20", title="t")
    text += "\n## Findings\n\nF1 · critical · security · a.py:1 – bad\n"
    updated = set_file_status(text, ["a.py"], "done", "1 finding")
    assert updated.endswith("\n## Findings\n\nF1 · critical · security · a.py:1 – bad\n")
    assert updated.startswith(text.split("## Inventory")[0])
    assert set_file_status(updated, ["a.py"], "done", "1 finding") == updated


def test_set_file_status_rejects_unknown_paths() -> None:
    text = render_audit([InventoryFile("a.py")], base="abc", date="2026-11-20", title="t")
    with pytest.raises(AuditError, match="not in the inventory: nope.py"):
        set_file_status(text, ["nope.py"], "done")


def test_parse_rejects_damaged_inventory() -> None:
    text = render_audit([InventoryFile("a.py")], base="abc", date="2026-11-20", title="t")
    with pytest.raises(AuditError, match="unreadable"):
        parse_inventory(text + "- junk\n")
    with pytest.raises(AuditError, match="Inventory"):
        parse_inventory("# Audit: t\n")
    with pytest.raises(AuditError, match="listed under"):
        parse_inventory(text.replace("### ./", "### other/"))
    with pytest.raises(AuditError, match="without a reason"):
        parse_inventory(text.replace("- [ ] `a.py`", "- [-] `a.py`"))


def test_batches_never_mix_folders_or_exceed_the_budget() -> None:
    files = [
        InventoryFile("a/1.py"),
        InventoryFile("a/2.py"),
        InventoryFile("a/3.py"),
        InventoryFile("b/1.py"),
    ]
    sizes = {"a/1.py": 40, "a/2.py": 40, "a/3.py": 40, "b/1.py": 10}
    first = next_batch(files, 100, sizes)
    assert [f.path for f in first] == ["a/1.py", "a/2.py"]
    rest = [InventoryFile(f.path, "done") if f in first else f for f in files]
    assert [f.path for f in next_batch(rest, 100, sizes)] == ["a/3.py"]
    rest[2] = InventoryFile("a/3.py", "done")
    assert [f.path for f in next_batch(rest, 100, sizes)] == ["b/1.py"]
    rest[3] = InventoryFile("b/1.py", "done")
    assert next_batch(rest, 100, sizes) == []


def test_batch_skips_closed_files_and_takes_an_oversized_first_file() -> None:
    files = [
        InventoryFile("a/1.py", "skipped", "lockfile"),
        InventoryFile("a/2.py"),
        InventoryFile("a/3.py"),
    ]
    assert [f.path for f in next_batch(files, 10, {"a/2.py": 50, "a/3.py": 1})] == ["a/2.py"]
    assert [f.path for f in next_batch(files, 10)] == ["a/2.py", "a/3.py"]


def test_batches_cover_the_whole_inventory(repo: Path) -> None:
    files = build_inventory(repo, "HEAD")
    seen: list[str] = []
    while batch := next_batch(files, 1_000_000):
        assert len({f.folder for f in batch}) == 1
        seen += [f.path for f in batch]
        files = [InventoryFile(f.path, "done") if f in batch else f for f in files]
    assert sorted(seen) == sorted(f.path for f in files if f.status == "done")
    assert "src/app.py" in seen
    assert ".env" not in seen


def _reply(*items: dict) -> str:
    return json.dumps({"findings": list(items)})


def _item(**over: object) -> dict:
    item = {"severity": "warning", "category": "smell", "message": "too long", "path": "a.py"}
    return {**item, **over}


def test_parse_audit_reply_accepts_valid_findings() -> None:
    reply = _reply(
        _item(line=7, message="  too\nlong  "),
        _item(severity="critical", category="security", message="eval", path="b.py"),
    )
    assert parse_audit_reply(reply, ["a.py", "b.py"]) == (
        AuditFinding("smell", "warning", "a.py", "too long", 7),
        AuditFinding("security", "critical", "b.py", "eval"),
    )
    assert parse_audit_reply('{"findings": []}', ["a.py"]) == ()


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ("nope", "Invalid audit JSON"),
        ("[]", "only 'findings'"),
        ('{"findings": {}}', "must be an array"),
        ('{"findings": [], "findings": []}', "Duplicate"),
        (_reply(_item(severity="high")), "severity"),
        (_reply(_item(category="style")), "category"),
        (_reply(_item(message=" ")), "message"),
        (_reply(_item(path="other.py")), "not a file of this batch"),
        (_reply({k: v for k, v in _item().items() if k != "path"}), "not a file of this batch"),
        (_reply(_item(line=0)), "line"),
        (_reply(_item(line="3")), "line"),
        (_reply(_item(line=True)), "line"),
        (_reply(_item(extra=1)), "Each finding"),
        ("[1]".join(['{"findings": ', "}"]), "Each finding"),
    ],
)
def test_parse_audit_reply_rejects(reply: str, error: str) -> None:
    with pytest.raises(AuditError, match=error):
        parse_audit_reply(reply, ["a.py", "b.py"])


def test_record_batch_numbers_findings_and_marks_files() -> None:
    files = [InventoryFile("a.py"), InventoryFile("b.py"), InventoryFile("c/d.py")]
    text = render_audit(files, base="abc", date="2026-11-20", title="t")
    first = parse_audit_reply(
        _reply(_item(line=3), _item(category="security", severity="critical", message="eval")),
        ["a.py", "b.py"],
    )
    text = record_batch(text, files[:2], first)
    assert "- [x] `a.py` – 2 findings" in text
    assert "- [x] `b.py` – 0 findings" in text
    assert "- [ ] `c/d.py`" in text
    assert "## Findings\n\nF1 · warning · smell · a.py:3 – too long\n" in text
    assert text.endswith("F2 · critical · security · a.py – eval\n")

    more = parse_audit_reply(_reply(_item(path="c/d.py", message="dup")), ["c/d.py"])
    text = record_batch(text, files[2:], more)
    assert [f.id for f in parse_findings(text)] == [1, 2, 3]
    assert parse_findings(text)[2] == AuditFinding("smell", "warning", "c/d.py", "dup", None, 3)
    assert parse_findings(text)[0].line == 3
    assert all(f.status == "done" for f in parse_inventory(text))


def test_record_batch_without_findings_creates_no_section() -> None:
    files = [InventoryFile("a.py")]
    text = render_audit(files, base="abc", date="2026-11-20", title="t")
    text = record_batch(text, files, ())
    assert "- [x] `a.py` – 0 findings" in text
    assert "## Findings" not in text
    assert parse_findings(text) == []


def test_record_batch_keeps_later_sections_after_findings() -> None:
    files = [InventoryFile("a.py"), InventoryFile("b.py")]
    text = render_audit(files, base="abc", date="2026-11-20", title="t")
    text = record_batch(text, files[:1], parse_audit_reply(_reply(_item()), ["a.py"]))
    text += "\n## Milestones\n\n### M1 [ ] Audit: x\n"
    text = record_batch(text, files[1:], parse_audit_reply(_reply(_item(path="b.py")), ["b.py"]))
    assert [f.id for f in parse_findings(text)] == [1, 2]
    assert text.endswith("\n## Milestones\n\n### M1 [ ] Audit: x\n")
    assert text.index("F2 ·") < text.index("## Milestones")


def test_record_batch_rejects_foreign_paths() -> None:
    files = [InventoryFile("a.py"), InventoryFile("b.py")]
    text = render_audit(files, base="abc", date="2026-11-20", title="t")
    with pytest.raises(AuditError, match="outside the batch: b.py"):
        record_batch(text, files[:1], [AuditFinding("smell", "warning", "b.py", "x")])


def test_parse_findings_rejects_garbage_and_handles_colons() -> None:
    files = [InventoryFile("odd:1.py")]
    text = render_audit(files, base="abc", date="2026-11-20", title="t")
    found = parse_audit_reply(_reply(_item(path="odd:1.py", line=2)), ["odd:1.py"])
    text = record_batch(text, files, found)
    assert parse_findings(text) == [AuditFinding("smell", "warning", "odd:1.py", "too long", 2, 1)]
    with pytest.raises(AuditError, match="unreadable finding"):
        parse_findings(text + "F2 nonsense\n")
