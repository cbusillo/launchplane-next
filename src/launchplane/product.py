import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from launchplane.instance import TESTING, Run, new_password
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

    # What the kind runs once to initialize the data of this lane, where the product declares that and the lane is one
    # that is initialized: with no command, an instance starts on its data set as it is.
    def first_start(self, lane: str) -> tuple[str, ...]:
        return self.odoo.first_start_command if self.odoo is not None and lane == TESTING else ()

    # What the kind runs in an instance of this lane to sign in as its testing login, where it has one.
    def sign_in(self, lane: str) -> tuple[str, ...]:
        return self.odoo.sign_in_command if self.odoo is not None and lane == TESTING else ()

    @property
    def test_password_variable(self) -> str:
        return TEST_PASSWORD_VARIABLE


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


def on_disk(path: Path) -> None:
    opened = os.open(path, os.O_RDONLY)
    try:
        os.fsync(opened)
    finally:
        os.close(opened)


# The password of each lane's testing login, one file each, which only Launchplane's own user reads. Each is told to
# `note` before it is given to anyone, so nothing Launchplane answers, stores or logs carries it.
@dataclass(frozen=True)
class SecretFolder:
    folder: Path
    note: Callable[[Iterable[str]], None]

    def path(self, product: str, lane: str) -> Path:
        return self.folder / product / lane / "test-login"

    def note_all(self) -> None:
        self.note(path.read_text() for path in self.folder.glob("*/*/test-login"))

    # Only looked up, for whoever proves the login: None where the lane has none kept.
    def kept(self, product: str, lane: str) -> str | None:
        try:
            password = self.path(product, lane).read_text()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ProductError(f"Reading the password of the testing login of {lane} of {product} failed: {error}") from error
        self.note([password])
        return password

    # For a first start alone. A new one is on the disk, under the name it is found by, before anybody is given it.
    def made(self, product: str, lane: str) -> str:
        path = self.path(product, lane)
        try:
            if not path.exists():
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fresh = path.with_name("test-login.new")
                fresh.touch(mode=0o600)
                fresh.write_text(new_password())
                on_disk(fresh)
                fresh.replace(path)
                on_disk(path.parent)
        except OSError as error:
            raise ProductError(f"Keeping the password of the testing login of {lane} of {product} failed: {error}") from error
        return self.kept(product, lane) or ""
