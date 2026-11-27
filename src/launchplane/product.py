from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from launchplane.instance import Run, new_password
from launchplane.odoo import TEST_PASSWORD_VARIABLE, Odoo
from launchplane.steps import Check


class ProductError(Exception):
    pass


class Database(BaseModel, extra="forbid", frozen=True):
    image: str


class BackgroundService(BaseModel, extra="forbid", frozen=True):
    command: list[str] = Field(min_length=1)


class Product(BaseModel, extra="forbid", frozen=True):
    health_path: str = Field(default="/", pattern=r"^/[^\s]*$")
    database: Database | None = None
    background_service: BackgroundService | None = None
    odoo: Odoo | None = None

    @property
    def run(self) -> Run:
        return Run() if self.odoo is None else self.odoo.run

    def check(self, container: str) -> Check:
        return Check(container, self.health_path, () if self.odoo is None else self.odoo.check)

    # A product whose releases change its data says how. With no command, a release is made on the data its lane has.
    @property
    def update(self) -> tuple[str, ...]:
        return self.odoo.update_command if self.odoo is not None and self.odoo.update else ()

    # What the kind runs once on a data set before its first web start, and the variable it reads the password of a
    # testing lane's login from. With no command, the first instance starts on the data set as it is.
    @property
    def first_start(self) -> tuple[str, ...]:
        return () if self.odoo is None else self.odoo.first_start_command

    @property
    def test_password_variable(self) -> str | None:
        return TEST_PASSWORD_VARIABLE if self.first_start else None


@dataclass(frozen=True)
class ProductFolder:
    folder: Path

    def read(self, product: str) -> Product:
        path = self.folder / f"{product}.json"
        try:
            text = path.read_text()
        except FileNotFoundError:
            return Product()
        except OSError as error:
            raise ProductError(f"Reading the description of {product} from {path} failed: {error}") from error
        try:
            return Product.model_validate_json(text)
        except ValidationError as error:
            raise ProductError(f"The description of {product} in {path} is not one Launchplane reads: {error}") from error


# The secrets Launchplane keeps for a lane, one file each, which only its own user reads. Each is told to `note` before
# it is given to anyone, so nothing Launchplane answers, stores or logs carries it.
@dataclass(frozen=True)
class SecretFolder:
    folder: Path
    note: Callable[[Iterable[str]], None]

    def note_all(self) -> None:
        self.note(path.read_text() for path in self.folder.glob("*/*/test-login"))

    # The password of one lane's testing login: made the first time it is asked for, and the same from then on.
    def test_password(self, product: str, lane: str) -> str:
        path = self.folder / product / lane / "test-login"
        try:
            if not path.exists():
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fresh = path.with_name("test-login.new")
                fresh.touch(mode=0o600)
                fresh.write_text(new_password())
                fresh.replace(path)
            password = path.read_text()
        except OSError as error:
            raise ProductError(f"Keeping the password of the testing login of {lane} of {product} failed: {error}") from error
        self.note([password])
        return password
