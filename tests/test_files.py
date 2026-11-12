"""Workspace confinement, file tools and archive creation."""

from __future__ import annotations

import os
import zipfile

import pytest

from hermes.tools.files import Workspace, WorkspaceError


def test_write_then_read_roundtrip(workspace):
    message = workspace.write_text("src/app.py", "print('hello')\n")
    assert "app.py" in message
    body = workspace.read_text("src/app.py")
    assert "print('hello')" in body
    assert "Created" in message or "Updated" in message


def test_write_reports_updated_for_existing_file(workspace):
    workspace.write_text("notes.txt", "one")
    assert "Updated" in workspace.write_text("notes.txt", "two")


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        "../../etc/passwd",
        "sub/../../escape.txt",
        "/etc/passwd",
        "/tmp/absolute.txt",
    ],
)
def test_paths_outside_workspace_are_rejected(workspace, path):
    with pytest.raises(WorkspaceError):
        workspace.write_text(path, "nope")
    with pytest.raises(WorkspaceError):
        workspace.read_text(path)


def test_symlink_escape_is_rejected(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret", encoding="utf-8")
    link = workspace.root / "link"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable in this environment")
    with pytest.raises(WorkspaceError):
        workspace.read_text("link/secret.txt")
    with pytest.raises(WorkspaceError):
        workspace.write_text("link/new.txt", "nope")


def test_nul_byte_is_rejected(workspace):
    with pytest.raises(WorkspaceError):
        workspace.read_text("bad\x00name.txt")


def test_size_limit_is_enforced(tmp_path):
    small = Workspace(tmp_path / "ws", max_file_bytes=50)
    with pytest.raises(WorkspaceError):
        small.write_text("big.txt", "x" * 500)


def test_git_directory_writes_are_refused(workspace):
    (workspace.root / ".git").mkdir()
    with pytest.raises(WorkspaceError):
        workspace.write_text(".git/config", "[core]")


def test_missing_file_returns_message(workspace):
    assert "does not exist" in workspace.read_text("nope.txt").lower()


def test_read_line_window(workspace):
    workspace.write_text("lines.txt", "\n".join(f"line {i}" for i in range(1, 21)))
    body = workspace.read_text("lines.txt", start_line=5, max_lines=3)
    assert "line 5" in body and "line 7" in body and "line 8" not in body


def test_replace_in_file(workspace):
    workspace.write_text("app.py", "DEBUG = False\nDEBUG = False\n")
    message = workspace.replace_in_file("app.py", "DEBUG = False", "DEBUG = True", count=1)
    assert "1 occurrence" in message
    assert workspace.read_text("app.py").count("DEBUG = True") == 1


def test_replace_reports_missing_text(workspace):
    workspace.write_text("app.py", "x = 1\n")
    with pytest.raises(WorkspaceError):
        workspace.replace_in_file("app.py", "not there", "y")


def test_delete_file_and_non_empty_directory(workspace):
    workspace.write_text("dir/file.txt", "x")
    with pytest.raises(WorkspaceError):
        workspace.delete("dir")
    assert "Deleted" in workspace.delete("dir/file.txt")
    assert "Removed empty directory" in workspace.delete("dir")


def test_list_and_tree(workspace):
    workspace.write_text("a.py", "x")
    workspace.write_text("pkg/b.js", "y")
    listing = workspace.list_files(".")
    assert "a.py" in listing and "pkg/" in listing
    tree = workspace.tree(".", max_depth=2)
    assert "pkg" in tree and "b.js" in tree


def test_search_files(workspace):
    workspace.write_text("one.py", "import os\nTOKEN = 'abc'\n")
    workspace.write_text("two.js", "const x = 1\n")
    hits = workspace.search("TOKEN", glob="*.py")
    assert "one.py:2" in hits
    assert workspace.search("TOKEN", glob="*.js") == "No matches."


def test_make_zip_contains_relative_paths(workspace):
    workspace.write_text("site/index.html", "<h1>hi</h1>")
    workspace.write_text("site/app.js", "console.log(1)")
    message = workspace.make_zip("build.zip", ["site"])
    assert "2 file(s)" in message
    with zipfile.ZipFile(workspace.root / "build.zip") as archive:
        assert sorted(archive.namelist()) == ["site/app.js", "site/index.html"]


def test_workspace_info(workspace):
    workspace.write_text("f.txt", "12345")
    info = workspace.info()
    assert info["files"] == 1
    assert info["size_bytes"] == 5
    assert info["writable"] is True
