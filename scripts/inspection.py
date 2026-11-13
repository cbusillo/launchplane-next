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
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

REPOSITORY = Path(__file__).resolve().parent.parent
INSPECTOR = Path("/Users/cbusillo/Applications/PyCharm.app/Contents/bin/inspect.sh")
INSPECTOR_HOME = Path("/Volumes/Developer-Artifacts/host-caches/launchplane-pycharm")
PROFILE = REPOSITORY / "scripts" / "inspection-profile.xml"
INSPECTED_FOLDERS = ("src", "tests", "scripts")
ANALYZED_LINE_PREFIX = "Analyzing code in …/"
RESULTS_INDEX = ".descriptions.json"
TYPESHED_STDLIB = INSPECTOR.parents[1] / "plugins" / "python-ce" / "helpers" / "typeshed" / "stdlib"
DEFAULT_INTERPRETER_NAME = "launchplane inspection"
PROJECT_SETTINGS = REPOSITORY / ".idea" / "misc.xml"
INSPECTOR_OPTIONS = INSPECTOR_HOME / "config" / "options"
INSPECTOR_CACHES = INSPECTOR_HOME / "system"
CANARY_FOLDER = REPOSITORY / ".inspection-canary"
CANARY_SOURCE = "from pathlib import Path\n\n\ndef canary() -> None:\n    Path().no_such_method()\n"


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


CANARY_FINDING = Finding(
    file=f"{CANARY_FOLDER.name}/canary.py",
    line=5,
    inspection="PyUnresolvedReferencesInspection",
    description="Unresolved attribute reference 'no_such_method' for class 'Path'",
)


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


def require_inspector_installed() -> None:
    for required in (INSPECTOR, TYPESHED_STDLIB):
        if not required.exists():
            raise InspectionUnknownError(f"PyCharm 2026.3 is not installed where this script expects it: {required} is missing.")


def require_inspector_home_reachable() -> None:
    if not INSPECTOR_HOME.parent.is_dir():
        raise InspectionUnknownError(
            f"The folder that holds PyCharm's inspection home, {INSPECTOR_HOME.parent}, does not exist. "
            "Mount the Developer-Artifacts volume, then run ./scripts/inspect again."
        )


@contextmanager
def exclusive_inspector_home() -> Generator[None]:
    try:
        INSPECTOR_HOME.mkdir(exist_ok=True)
        lock = (INSPECTOR_HOME / "inspection.lock").open("w")
    except OSError as error:
        raise InspectionUnknownError(f"Could not open PyCharm's inspection home {INSPECTOR_HOME}: {error}") from error
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InspectionUnknownError(
                f"Another inspection is using {INSPECTOR_HOME}. Wait for it to finish, then run ./scripts/inspect again."
            ) from error
        yield


def prepare_inspection() -> Path:
    forget_earlier_inspections()
    try:
        trust_repository()
        register_interpreter(project_interpreter_name())
        CANARY_FOLDER.mkdir(exist_ok=True)
        (CANARY_FOLDER / "canary.py").write_text(CANARY_SOURCE)
        return write_inspector_properties()
    except (OSError, ElementTree.ParseError) as error:
        raise InspectionUnknownError(f"Could not prepare PyCharm to inspect this repository: {error}") from error


def write_inspector_properties() -> Path:
    properties = INSPECTOR_HOME / "idea.properties"
    properties.write_text("".join(f"idea.{kind}.path={INSPECTOR_HOME / kind}\n" for kind in ("config", "system", "plugins", "log")))
    return properties


def forget_earlier_inspections() -> None:
    try:
        shutil.rmtree(INSPECTOR_CACHES)
    except FileNotFoundError:
        return
    except OSError as error:
        raise InspectionUnknownError(
            f"Could not clear PyCharm's caches in {INSPECTOR_CACHES}, so this run could reuse a broken project state: {error}. "
            "Remove that folder by hand, then run ./scripts/inspect again."
        ) from error


def trust_repository() -> None:
    INSPECTOR_OPTIONS.mkdir(parents=True, exist_ok=True)
    (INSPECTOR_OPTIONS / "trusted-paths.xml").write_text(
        '<application><component name="Trusted.Paths"><option name="TRUSTED_PROJECT_PATHS"><map>'
        f'<entry key={quoteattr(str(REPOSITORY))} value="true" /></map></option></component></application>\n'
    )


def register_interpreter(name: str) -> None:
    library_roots = (
        Path(sysconfig.get_path("stdlib")),
        Path(sysconfig.get_path("stdlib")) / "lib-dynload",
        Path(sysconfig.get_path("purelib")),
        TYPESHED_STDLIB,
    )
    roots = "".join(f'<root url={quoteattr(root.as_uri())} type="simple" />' for root in library_roots)
    INSPECTOR_OPTIONS.mkdir(parents=True, exist_ok=True)
    (INSPECTOR_OPTIONS / "jdk.table.xml").write_text(
        f'<application><component name="ProjectJdkTable"><jdk version="2"><name value={quoteattr(name)} />'
        f'<type value="Python SDK" /><homePath value={quoteattr(sys.executable)} />'
        f'<roots><classPath><root type="composite">{roots}</root></classPath>'
        '<sourcePath><root type="composite" /></sourcePath></roots></jdk></component></application>\n'
    )


def project_interpreter_name() -> str:
    settings = read_project_settings()
    root_manager = settings.find("component[@name='ProjectRootManager']")
    if root_manager is None:
        root_manager = ElementTree.SubElement(settings, "component", name="ProjectRootManager", version="2")
    if (name := root_manager.get("project-jdk-name")) is not None:
        return name
    root_manager.set("project-jdk-name", DEFAULT_INTERPRETER_NAME)
    root_manager.set("project-jdk-type", "Python SDK")
    PROJECT_SETTINGS.parent.mkdir(exist_ok=True)
    ElementTree.ElementTree(settings).write(PROJECT_SETTINGS, encoding="UTF-8", xml_declaration=True)
    return DEFAULT_INTERPRETER_NAME


def read_project_settings() -> ElementTree.Element:
    if not PROJECT_SETTINGS.exists():
        return ElementTree.Element("project", version="4")
    return ElementTree.parse(PROJECT_SETTINGS).getroot()  # noqa: S314 PyCharm's own settings file in this checkout


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
    if CANARY_FINDING not in canary_findings:
        raise InspectionUnknownError(
            f"PyCharm did not report '{CANARY_FINDING}' in {CANARY_FOLDER}, so it is not resolving the standard library "
            f"and a clean answer would be false. Every run starts from fresh caches, so look for the cause in "
            f"{INSPECTOR_HOME / 'log' / 'idea.log'} and check that {TYPESHED_STDLIB} exists."
        )


def inspect_repository() -> list[Finding]:
    require_inspector_installed()
    require_inspector_home_reachable()
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
