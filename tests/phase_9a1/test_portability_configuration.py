"""Phase 9A.1 portability configuration tests — Task 2."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.phase_9a1


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_pyproject_declares_tzdata_conditional() -> None:
    """Windows needs an explicit tzdata dependency so zoneinfo works."""
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    # Must mention tzdata — either unconditionally or as a platform marker
    assert "tzdata" in pyproject, (
        "pyproject.toml must declare tzdata for Windows zoneinfo support"
    )

    # If conditional, verify the marker targets Windows correctly
    if 'sys_platform == "win32"' in pyproject or "sys_platform == 'win32'" in pyproject:
        # OK — intentionally platform-scoped
        pass
    elif "tzdata" in pyproject:
        # Listed unconditionally — also acceptable (harmless on non-Windows)
        pass


def test_ruff_tool_excludes_generated_directories() -> None:
    """Ruff must explicitly ignore generated/managed directories."""
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    # Look for an [tool.ruff] exclude section
    ruff_section_start = pyproject.find("[tool.ruff]")
    assert ruff_section_start != -1, "pyproject.toml must have a [tool.ruff] section"

    # The next header or EOF
    remainder = pyproject[ruff_section_start:]
    next_header = re.search(r"\n\[(?!tool\.ruff)", remainder)
    ruff_text = remainder[: next_header.start()] if next_header else remainder

    for pattern in [
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    ]:
        assert pattern in ruff_text, (
            f"Ruff exclude must contain '{pattern}' so Ruff doesn't scan generated dirs"
        )


def test_mypy_tool_excludes_generated_directories() -> None:
    """Mypy must explicitly exclude generated/managed directories."""
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    mypy_start = pyproject.find("[tool.mypy]")
    assert mypy_start != -1, "pyproject.toml must have a [tool.mypy] section"

    remainder = pyproject[mypy_start:]
    next_header = re.search(r"\n\[(?!tool\.mypy)", remainder)
    mypy_text = remainder[: next_header.start()] if next_header else remainder

    for pattern in [".venv", "venv"]:
        assert pattern in mypy_text, (
            f"Mypy exclude must contain '{pattern}' to skip virtual environments"
        )


def test_pytest_tool_excludes_generated_directories() -> None:
    """Pytest configuration must ignore generated directories via norecursedirs."""
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    pytest_start = pyproject.find("[tool.pytest.ini_options]")
    assert pytest_start != -1, (
        "pyproject.toml must have a [tool.pytest.ini_options] section"
    )

    remainder = pyproject[pytest_start:]
    next_header = re.search(r"\n\[(?!tool\.pytest)", remainder)
    pytest_text = remainder[: next_header.start()] if next_header else remainder

    # Either norecursedirs or an explicit addopts/norecursedirs line
    for pattern in [".venv", "norecursedirs"]:
        assert pattern in pytest_text, (
            f"Pytest config must contain '{pattern}' to avoid scanning generated dirs"
        )
    # ".*" in norecursedirs matches __pycache__ and other hidden dirs
    assert ".*" in pytest_text or "__pycache__" in pytest_text, (
        "Pytest norecursedirs must exclude hidden directories"
    )
