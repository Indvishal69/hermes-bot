"""Workspace-confined file tools.

Every path is resolved and checked against the workspace root, so ``../``,
absolute paths and symlinks that point outside cannot be used to read or
write anything else on the host.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..utils import human_bytes, truncate

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


class WorkspaceError(ValueError):
    """Raised for any file operation that would break workspace confinement."""


@dataclass
class FileInfo:
    path: str
    size: int
    is_dir: bool


class Workspace:
    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = 200_000,
        max_output_chars: int = 7_000,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._root = self.root.resolve()
        self.max_file_bytes = max_file_bytes
        self.max_output_chars = max_output_chars

    # -- paths --------------------------------------------------------------
    def resolve(self, relative: str | Path | None, *, must_exist: bool = False) -> Path:
        raw = str(relative if relative is not None else ".").strip() or "."
        if "\x00" in raw:
            raise WorkspaceError("Path contains a NUL byte.")
        candidate_path = Path(raw)
        if candidate_path.is_absolute():
            raise WorkspaceError("Absolute paths are not allowed; use a workspace-relative path.")
        try:
            candidate = (self._root / candidate_path).resolve()
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(f"Unresolvable path: {exc}") from exc
        if candidate != self._root and self._root not in candidate.parents:
            raise WorkspaceError("Path is outside the Hermes workspace.")
        if must_exist and not candidate.exists():
            raise WorkspaceError(f"Path does not exist: {raw}")
        return candidate

    def relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self._root)) or "."
        except ValueError:
            return str(path)

    # -- read ---------------------------------------------------------------
    def list_files(self, relative: str = ".", *, max_entries: int = 200) -> str:
        folder = self.resolve(relative, must_exist=True)
        if not folder.is_dir():
            return f"Not a directory: {relative}"
        entries = sorted(
            folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
        )[:max_entries]
        if not entries:
            return "(empty directory)"
        lines = []
        for item in entries:
            if item.is_dir():
                lines.append(f"{item.name}/")
            else:
                try:
                    size = human_bytes(item.stat().st_size)
                except OSError:
                    size = "?"
                lines.append(f"{item.name}  ({size})")
        return "\n".join(lines)

    def tree(self, relative: str = ".", *, max_depth: int = 2, max_entries: int = 200) -> str:
        base = self.resolve(relative, must_exist=True)
        if not base.is_dir():
            return f"Not a directory: {relative}"
        lines: list[str] = []
        count = 0

        def walk(folder: Path, depth: int) -> None:
            nonlocal count
            if depth > max_depth or count >= max_entries:
                return
            try:
                items = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                return
            for item in items:
                if item.name in SKIP_DIRS:
                    continue
                if count >= max_entries:
                    lines.append("…[truncated]")
                    return
                indent = "  " * depth
                lines.append(f"{indent}{item.name}{'/' if item.is_dir() else ''}")
                count += 1
                if item.is_dir():
                    walk(item, depth + 1)

        walk(base, 0)
        return "\n".join(lines) or "(empty directory)"

    def read_text(
        self, relative: str, *, start_line: int = 1, max_lines: int = 0
    ) -> str:
        path = self.resolve(relative)
        if not path.exists():
            return f"File does not exist: {relative}"
        if not path.is_file():
            return f"Not a file: {relative}"
        size = path.stat().st_size
        if size > self.max_file_bytes:
            return (
                f"Refusing to read {relative}: {human_bytes(size)} exceeds the "
                f"{human_bytes(self.max_file_bytes)} read limit."
            )
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"{relative} is binary or not UTF-8 text."
        lines = text.splitlines()
        start = max(1, int(start_line))
        selected = lines[start - 1 :]
        if max_lines and max_lines > 0:
            selected = selected[:max_lines]
        body = "\n".join(selected)
        header = f"{relative} ({len(lines)} lines, {human_bytes(size)})"
        if start > 1 or (max_lines and len(lines) > start - 1 + max_lines):
            header += f" — showing lines {start}-{start + len(selected) - 1}"
        return f"{header}\n{truncate(body, self.max_output_chars)}"

    # -- write --------------------------------------------------------------
    def write_text(self, relative: str, content: str) -> str:
        path = self.resolve(relative)
        if path == self._root:
            raise WorkspaceError("Refusing to write to the workspace root.")
        if ".git" in path.relative_to(self._root).parts:
            raise WorkspaceError("Refusing to write inside .git.")
        existed = path.exists()
        data = (content or "").encode("utf-8")
        if len(data) > self.max_file_bytes:
            raise WorkspaceError(
                f"Content is {human_bytes(len(data))}, over the "
                f"{human_bytes(self.max_file_bytes)} per-file limit."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".hermes-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        action = "Updated" if existed else "Created"
        return f"{action} {self.relative(path)} ({human_bytes(len(data))})."

    def replace_in_file(
        self, relative: str, old: str, new: str, *, count: int = 1
    ) -> str:
        if not old:
            raise WorkspaceError("'old' text must not be empty.")
        path = self.resolve(relative, must_exist=True)
        if not path.is_file():
            raise WorkspaceError(f"Not a file: {relative}")
        text = path.read_text(encoding="utf-8")
        occurrences = text.count(old)
        if occurrences == 0:
            raise WorkspaceError("The 'old' text was not found in the file.")
        replaced = text.replace(old, new, count if count > 0 else occurrences)
        self.write_text(relative, replaced)
        return f"Replaced {min(occurrences, count if count > 0 else occurrences)} occurrence(s) in {relative}."

    def append_text(self, relative: str, content: str) -> str:
        path = self.resolve(relative)
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        return self.write_text(relative, existing + (content or ""))

    def delete(self, relative: str) -> str:
        path = self.resolve(relative, must_exist=True)
        if path == self._root:
            raise WorkspaceError("Refusing to delete the workspace root.")
        if path.is_dir():
            if any(path.iterdir()):
                raise WorkspaceError(
                    f"Directory {relative} is not empty. Delete its files individually."
                )
            path.rmdir()
            return f"Removed empty directory {relative}."
        path.unlink()
        return f"Deleted file {relative}."

    # -- search / archive ---------------------------------------------------
    def search(
        self,
        pattern: str,
        *,
        glob: str = "*",
        max_results: int = 50,
        ignore_case: bool = True,
    ) -> str:
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise WorkspaceError(f"Invalid search pattern: {exc}") from exc
        hits: list[str] = []
        for path in self._walk_files(glob):
            if len(hits) >= max_results:
                hits.append("…[truncated]")
                break
            try:
                if path.stat().st_size > self.max_file_bytes:
                    continue
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{self.relative(path)}:{number}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        break
        return "\n".join(hits) if hits else "No matches."

    def _walk_files(self, glob: str) -> Iterable[Path]:
        for current, dirs, files in os.walk(self._root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if fnmatch.fnmatch(name, glob):
                    yield Path(current) / name

    def make_zip(self, archive_name: str, paths: Iterable[str] | None = None) -> str:
        if not archive_name.lower().endswith(".zip"):
            archive_name += ".zip"
        target = self.resolve(archive_name)
        if target.parent != self._root and self._root not in target.parents:
            raise WorkspaceError("Archive path is outside the workspace.")
        target.parent.mkdir(parents=True, exist_ok=True)
        selected: list[Path] = []
        if paths:
            for item in paths:
                selected.append(self.resolve(item, must_exist=True))
        else:
            selected = [p for p in self._walk_files("*") if p.name != target.name]
        written = 0
        total = 0
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in selected:
                files = [path] if path.is_file() else [
                    p for p in path.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.parts)
                ]
                for file_path in files:
                    if file_path.resolve() == target:
                        continue
                    total += file_path.stat().st_size
                    if total > self.max_file_bytes * 20:
                        archive.close()
                        target.unlink(missing_ok=True)
                        raise WorkspaceError("Archive would exceed the size limit.")
                    archive.write(file_path, file_path.relative_to(self._root).as_posix())
                    written += 1
        return (
            f"Created {self.relative(target)} with {written} file(s), "
            f"{human_bytes(target.stat().st_size)}."
        )

    # -- introspection ------------------------------------------------------
    def info(self) -> dict[str, object]:
        files = 0
        total = 0
        for path in self._walk_files("*"):
            try:
                total += path.stat().st_size
                files += 1
            except OSError:
                continue
        return {
            "root": str(self._root),
            "files": files,
            "size_bytes": total,
            "writable": os.access(self._root, os.W_OK),
        }
