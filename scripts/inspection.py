import fcntl
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import quoteattr

REPOSITORY = Path(__file__).resolve().parent.parent
INSPECTOR = Path("/Users/cbusillo/Applications/PyCharm.app/Contents/bin/inspect.sh")
INSPECTOR_HOME = Path("/Volumes/Developer-Artifacts/host-caches/launchplane-pycharm")
PROFILE = REPOSITORY / "scripts" / "inspection-profile.xml"
INSPECTED_FOLDERS = ("src", "tests", "scripts")
ANALYZED_LINE_PREFIX = "Analyzing code in …/"
RESULTS_INDEX = ".descriptions.json"
TYPESHED_STDLIB = INSPECTOR.parents[1] / "plugins" / "python-ce" / "helpers" / "typeshed" / "stdlib"
SDK_NAME = f"launchplane inspection of {REPOSITORY}"
SDK_TABLE = INSPECTOR_HOME / "config" / "options" / "jdk.table.xml"
SDK_TABLE_CACHE = INSPECTOR_HOME / "system" / "global-model-cache"
CANARY_FOLDER = REPOSITORY / ".inspection-canary"
CANARY_SOURCE = "from pathlib import Path\n\n\ndef canary() -> None:\n    Path().no_such_method()\n"
CANARY_INSPECTION = "PyUnresolvedReferencesInspection"


class InspectionUnknownError(Exception):
    pass


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    inspection: str
    description: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.description} [{self.inspection}]"


def read_findings(output: Path) -> list[Finding]:
    if not (output / RESULTS_INDEX).is_file():
        raise InspectionUnknownError(f"PyCharm wrote no results index to {output}, so the inspection did not finish.")
    findings: list[Finding] = []
    for report in sorted(output.glob("*.json")):
        if report.name == RESULTS_INDEX:
            continue
        findings.extend(
            Finding(
                file=problem["file"].removeprefix("file://$PROJECT_DIR$/"),
                line=problem.get("line", 0),
                inspection=problem["problem_class"]["id"],
                description=problem["description"],
            )
            for problem in json.loads(report.read_text())["problems"]
        )
    return findings


def unanalyzed_files(folder: Path, inspector_log: str) -> list[str]:
    analyzed = {
        line.removeprefix(ANALYZED_LINE_PREFIX) for line in inspector_log.splitlines() if line.startswith(ANALYZED_LINE_PREFIX)
    }
    expected = sorted(
        source.relative_to(folder.parent).as_posix() for source in folder.rglob("*.py") if "__pycache__" not in source.parts
    )
    return [source for source in expected if source not in analyzed]


@contextmanager
def exclusive_inspector_home() -> Generator[None]:
    INSPECTOR_HOME.mkdir(parents=True, exist_ok=True)
    with (INSPECTOR_HOME / "inspection.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InspectionUnknownError(
                f"Another inspection is using {INSPECTOR_HOME}. Wait for it to finish, then run ./scripts/inspect again."
            ) from error
        yield


def prepare_inspection() -> Path:
    if not is_interpreter_registered():
        shutil.rmtree(SDK_TABLE_CACHE, ignore_errors=True)
        register_interpreter()
    point_project_at_interpreter()
    CANARY_FOLDER.mkdir(exist_ok=True)
    (CANARY_FOLDER / "canary.py").write_text(CANARY_SOURCE)
    return write_inspector_properties()


def write_inspector_properties() -> Path:
    properties = INSPECTOR_HOME / "idea.properties"
    properties.write_text("".join(f"idea.{kind}.path={INSPECTOR_HOME / kind}\n" for kind in ("config", "system", "plugins", "log")))
    return properties


def is_interpreter_registered() -> bool:
    return SDK_TABLE.exists() and SDK_NAME in SDK_TABLE.read_text()


def register_interpreter() -> None:
    library_roots = (
        Path(sysconfig.get_path("stdlib")),
        Path(sysconfig.get_path("stdlib")) / "lib-dynload",
        Path(sysconfig.get_path("purelib")),
        TYPESHED_STDLIB,
    )
    roots = "".join(f'<root url={quoteattr(root.as_uri())} type="simple" />' for root in library_roots)
    SDK_TABLE.parent.mkdir(parents=True, exist_ok=True)
    SDK_TABLE.write_text(
        f'<application><component name="ProjectJdkTable"><jdk version="2"><name value={quoteattr(SDK_NAME)} />'
        f'<type value="Python SDK" /><homePath value={quoteattr(sys.executable)} />'
        f'<roots><classPath><root type="composite">{roots}</root></classPath>'
        '<sourcePath><root type="composite" /></sourcePath></roots></jdk></component></application>\n'
    )


def point_project_at_interpreter() -> None:
    (REPOSITORY / ".idea").mkdir(exist_ok=True)
    (REPOSITORY / ".idea" / "misc.xml").write_text(
        f'<project version="4"><component name="ProjectRootManager" version="2" project-jdk-name={quoteattr(SDK_NAME)}'
        ' project-jdk-type="Python SDK" /></project>\n'
    )


def run_inspector(folder: Path, properties: Path) -> list[Finding]:
    with tempfile.TemporaryDirectory() as output:
        command = [INSPECTOR, REPOSITORY, PROFILE, output, "-d", folder, "-v2", "-format", "json"]
        result = subprocess.run(  # noqa: S603 the command is built only from this file's fixed paths
            command,
            cwd=INSPECTOR_HOME,
            env=os.environ | {"PYCHARM_PROPERTIES": str(properties)},
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            last_lines = "\n".join((result.stdout + result.stderr).splitlines()[-20:])
            raise InspectionUnknownError(f"PyCharm's inspector exited {result.returncode} on {folder}:\n{last_lines}")
        missing = unanalyzed_files(folder, result.stdout)
        if missing:
            raise InspectionUnknownError(f"PyCharm finished {folder} without analyzing: {', '.join(missing)}")
        return read_findings(Path(output))


def require_canary_caught(canary_findings: list[Finding]) -> None:
    if not any(finding.inspection == CANARY_INSPECTION for finding in canary_findings):
        raise InspectionUnknownError(
            f"PyCharm missed the known defect in {CANARY_FOLDER}/canary.py, so it is not resolving the standard library "
            f"and a clean answer would be false. Delete {SDK_TABLE} so the next run registers the interpreter again."
        )


def inspect_repository() -> list[Finding]:
    with exclusive_inspector_home():
        properties = prepare_inspection()
        require_canary_caught(run_inspector(CANARY_FOLDER, properties))
        return [finding for name in INSPECTED_FOLDERS for finding in run_inspector(REPOSITORY / name, properties)]


def main() -> int:
    try:
        findings = inspect_repository()
    except InspectionUnknownError as error:
        sys.stderr.write(f"UNKNOWN: {error}\n")
        return 2
    sys.stdout.writelines(f"{finding}\n" for finding in findings)
    sys.stdout.write(f"{len(findings)} findings\n" if findings else f"clean: {', '.join(INSPECTED_FOLDERS)}\n")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
