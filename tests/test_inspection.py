from pathlib import Path

import pytest

from scripts.inspection import InspectionUnknownError, read_findings, require_canary_caught, unanalyzed_files


def test_results_without_the_index_are_unknown_not_clean(tmp_path: Path) -> None:
    with pytest.raises(InspectionUnknownError):
        read_findings(tmp_path)


def test_a_file_the_inspector_skipped_is_reported(tmp_path: Path) -> None:
    folder = tmp_path / "src"
    (folder / "launchplane").mkdir(parents=True)
    (folder / "launchplane" / "seen.py").touch()
    (folder / "launchplane" / "skipped.py").touch()
    inspector_log = "Analyzing code in …/src/launchplane/seen.py\n"

    assert unanalyzed_files(folder, inspector_log) == ["src/launchplane/skipped.py"]


def test_a_run_that_misses_the_canary_defect_is_unknown() -> None:
    with pytest.raises(InspectionUnknownError):
        require_canary_caught([])
