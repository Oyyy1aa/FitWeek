from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.phase9a1_formal_files import (
    FormalFileSelectionError,
    create_zip,
    iter_formal_files,
)

pytestmark = pytest.mark.phase_9a1


def test_formal_file_selector_excludes_generated_and_environment_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "fitweek.egg-info").mkdir()
    (tmp_path / "fitweek.egg-info" / "PKG-INFO").write_text(
        "metadata\n", encoding="utf-8"
    )
    (tmp_path / ".venv.pre-recovery-1").mkdir()
    (tmp_path / ".venv.pre-recovery-1" / "python.exe").write_text("", encoding="utf-8")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "nodeids").write_text("[]\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")

    selected = [item.relative_path.as_posix() for item in iter_formal_files(tmp_path)]

    assert selected == ["app/main.py"]


def test_formal_file_selector_is_sorted_and_rejects_escape_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "z.txt").write_text("z\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")

    selected = list(iter_formal_files(tmp_path))

    assert [item.relative_path.as_posix() for item in selected] == ["a.txt", "z.txt"]
    with pytest.raises(FormalFileSelectionError, match="missing or not a directory"):
        next(iter_formal_files(tmp_path / "missing"))


def test_zip_uses_selector_and_excludes_requested_output_paths(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text("generated\n", encoding="utf-8")
    archive_path = tmp_path.parent / "formal.zip"

    create_zip(
        tmp_path,
        archive_path,
        exclude_relative_paths=frozenset({manifest.relative_to(tmp_path)}),
    )

    with ZipFile(archive_path) as archive:
        assert archive.namelist() == ["app.py"]


def test_formal_file_selector_excludes_pytest_basetemp_directories(
    tmp_path: Path,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / ".pytest-basetemp-full").mkdir()
    (tmp_path / ".pytest-basetemp-full" / "memory.jsonl").write_text(
        "[]\n", encoding="utf-8"
    )
    (tmp_path / ".pytest-basetemp-phase9a").mkdir()
    (tmp_path / ".pytest-basetemp-phase9a" / "ablations.yaml").write_text(
        "x: 1\n", encoding="utf-8"
    )

    selected = [item.relative_path.as_posix() for item in iter_formal_files(tmp_path)]

    assert selected == ["app/main.py"]
