import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from scripts import inspection
from scripts.inspection import InspectionUnknownError, read_findings, require_canary_caught, unanalyzed_files


def pycharm_problem(file: str, line: int, inspection_id: str, description: str) -> dict[str, Any]:
    return {
        "file": f"file://$PROJECT_DIR$/{file}",
        "line": line,
        "problem_class": {"name": "", "id": inspection_id, "severity": "WARNING"},
        "description": description,
    }


def write_pycharm_results(output: Path, problems: list[dict[str, Any]]) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    (output / ".descriptions.json").write_text("{}")
    for problem in problems:
        report = output / f"{problem['problem_class']['id']}.json"
        report.write_text(json.dumps({"problems": [problem]}))
    return output


def assert_answers_unknown() -> None:
    errors = StringIO()
    with redirect_stderr(errors):
        exit_code = inspection.main()
    assert exit_code == 2
    assert errors.getvalue().startswith("UNKNOWN:")


PLANTED_CANARY_PROBLEM = pycharm_problem(
    ".inspection-canary/canary.py",
    5,
    "PyUnresolvedReferencesInspection",
    "Unresolved attribute reference 'no_such_method' for class 'Path'",
)


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


def test_an_unresolved_standard_library_import_does_not_pass_as_the_canary(tmp_path: Path) -> None:
    stdlib_missing = pycharm_problem(
        ".inspection-canary/canary.py", 1, "PyUnresolvedReferencesInspection", "Unresolved reference 'pathlib'"
    )
    findings = read_findings(write_pycharm_results(tmp_path, [stdlib_missing]))

    with pytest.raises(InspectionUnknownError):
        require_canary_caught(findings)


def test_the_planted_canary_defect_as_pycharm_reports_it_passes(tmp_path: Path) -> None:
    require_canary_caught(read_findings(write_pycharm_results(tmp_path, [PLANTED_CANARY_PROBLEM])))


def test_a_finding_reaches_the_output_and_fails_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mistake = pycharm_problem(
        "src/launchplane/mistake.py", 3, "PyTypeCheckerInspection", "Expected type 'int', got 'Literal[\"three\"]' instead"
    )
    results = write_pycharm_results(tmp_path, [mistake])
    monkeypatch.setattr(inspection, "inspect_repository", lambda: read_findings(results))

    output = StringIO()
    with redirect_stdout(output):
        exit_code = inspection.main()

    assert exit_code == 1
    assert "src/launchplane/mistake.py:3: Expected type 'int'" in output.getvalue()


def test_missing_pycharm_answers_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inspection, "INSPECTOR", tmp_path / "inspect.sh")

    assert_answers_unknown()


def test_an_unmounted_inspection_home_answers_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "inspect.sh").touch()
    monkeypatch.setattr(inspection, "INSPECTOR", tmp_path / "inspect.sh")
    monkeypatch.setattr(inspection, "TYPESHED_STDLIB", tmp_path)
    monkeypatch.setattr(inspection, "INSPECTOR_HOME", tmp_path / "unmounted-volume" / "launchplane-pycharm")

    assert_answers_unknown()


def test_caches_that_cannot_be_cleared_answer_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    caches = tmp_path / "system"
    (caches / "index").mkdir(parents=True)
    (caches / "index" / "stale").touch()
    monkeypatch.setattr(inspection, "INSPECTOR_CACHES", caches)
    (caches / "index").chmod(0o500)
    try:
        with pytest.raises(InspectionUnknownError):
            inspection.forget_earlier_inspections()
    finally:
        (caches / "index").chmod(0o700)


def test_existing_project_settings_keep_their_interpreter_and_other_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / ".idea" / "misc.xml"
    settings.parent.mkdir()
    original = (
        '<project version="4"><component name="Black"><option name="enabled" value="true" /></component>'
        '<component name="ProjectRootManager" version="2" project-jdk-name="uv (launchplane-next)" project-jdk-type="Python SDK" />'
        "</project>"
    )
    settings.write_text(original)
    monkeypatch.setattr(inspection, "PROJECT_SETTINGS", settings)

    assert inspection.project_interpreter_name() == "uv (launchplane-next)"
    assert settings.read_text() == original


def test_project_settings_without_an_interpreter_gain_one_and_keep_the_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = tmp_path / ".idea" / "misc.xml"
    settings.parent.mkdir()
    settings.write_text('<project version="4"><component name="Black"><option name="enabled" value="true" /></component></project>')
    monkeypatch.setattr(inspection, "PROJECT_SETTINGS", settings)

    name = inspection.project_interpreter_name()

    assert inspection.project_interpreter_name() == name
    assert 'name="Black"' in settings.read_text()
