"""Release bookkeeping: the changelog and the version must agree."""

import re

import pytest

import app
from scripts import release_notes

CHANGELOG = """# Changelog

## [Unreleased]
### Added
- Something new

## [0.2.0] - 2026-12-01
### Added
- Caching

## [0.1.0] - 2026-11-17
### Added
- First version

[Unreleased]: https://example.com/compare/v0.2.0...HEAD
[0.2.0]: https://example.com/compare/v0.1.0...v0.2.0
"""


def test_extracts_one_section() -> None:
    assert release_notes.section(CHANGELOG, "0.2.0") == "### Added\n- Caching"


def test_last_section_stops_before_link_definitions() -> None:
    assert release_notes.section(CHANGELOG, "0.1.0") == "### Added\n- First version"


def test_missing_or_empty_section_is_an_error() -> None:
    with pytest.raises(LookupError, match="no"):
        release_notes.section(CHANGELOG, "9.9.9")
    with pytest.raises(LookupError, match="empty"):
        release_notes.section("## [1.0.0]\n\n## [0.9.0]\n- x\n", "1.0.0")


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", app.__version__)


def test_current_version_has_changelog_notes() -> None:
    # Bumping app.__version__ without writing release notes fails this test.
    notes = release_notes.section(release_notes.CHANGELOG.read_text("utf-8"), app.__version__)
    assert notes


def test_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert release_notes.main([app.__version__]) == 0
    assert capsys.readouterr().out.strip()
    assert release_notes.main(["9.9.9"]) == 1
    assert release_notes.main([]) == 2
