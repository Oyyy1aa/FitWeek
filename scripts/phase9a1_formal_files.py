"""Deterministic source-file selection used by portability tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


class FormalFileSelectionError(ValueError):
    """Raised when a source root cannot be selected safely."""


@dataclass(frozen=True, slots=True)
class FormalFile:
    absolute_path: Path
    relative_path: Path


_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hypothesis",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".pyright",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "artifacts",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
        "venv",
    }
)
_EXCLUDED_FILE_NAMES = frozenset({".coverage", ".env"})
_EXCLUDED_SUFFIXES = frozenset(
    {".bak", ".ics", ".log", ".orig", ".pyc", ".pyo", ".rej", ".tmp", ".zip"}
)


def _excluded(relative_path: Path) -> bool:
    parts = relative_path.parts
    if any(
        part in _EXCLUDED_DIRECTORY_NAMES
        or part.startswith(".venv")
        or part.startswith(".pytest-basetemp")
        or part.endswith(".egg-info")
        for part in parts[:-1]
    ):
        return True
    name = relative_path.name
    return (
        name in _EXCLUDED_FILE_NAMES
        or name.endswith("~")
        or relative_path.suffix.casefold() in _EXCLUDED_SUFFIXES
    )


def iter_formal_files(root: Path):
    """Yield safe repository files in stable relative-path order."""

    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise FormalFileSelectionError("source root is missing or not a directory")
    selected: list[FormalFile] = []
    for candidate in resolved_root.rglob("*"):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(resolved_root)
        if not _excluded(relative):
            selected.append(FormalFile(candidate, relative))
    yield from sorted(selected, key=lambda item: item.relative_path.as_posix())


def create_zip(
    root: Path,
    archive_path: Path,
    *,
    exclude_relative_paths: frozenset[Path] = frozenset(),
) -> None:
    """Create a deterministic-entry-order ZIP from the formal file set."""

    excluded = {path.as_posix() for path in exclude_relative_paths}
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for item in iter_formal_files(root):
            archive_name = item.relative_path.as_posix()
            if archive_name not in excluded:
                archive.write(item.absolute_path, archive_name)
