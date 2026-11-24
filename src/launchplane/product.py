from dataclasses import dataclass
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from launchplane import odoo
from launchplane.instance import PLAIN, Run
from launchplane.steps import Check


class ProductError(Exception):
    pass


class Described(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Database(Described):
    image: str


class BackgroundService(Described):
    command: list[str] = Field(min_length=1)


# Where a build comes from: a run of this workflow in this repository, which publishes to this image repository.
class BuildSource(Described):
    repository: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    workflow: str
    image: str


class Attachment(Described):
    name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


# What a product of the Odoo kind declares for itself. How the kind runs with it is in `odoo`.
class Odoo(Described):
    addons_path: list[str] = Field(min_length=1)
    modules: list[str] = Field(min_length=1)
    attachment: Attachment


class Product(Described):
    health_path: str = Field(default="/", pattern=r"^/[^\s]*$")
    database: Database | None = None
    background_service: BackgroundService | None = None
    build_from: BuildSource | None = None
    odoo: Odoo | None = None

    @model_validator(mode="after")
    def odoo_has_what_it_runs_on(self) -> Self:
        if self.odoo is not None and (self.database is None or self.build_from is None):
            raise ValueError("a product of the Odoo kind must describe its database and where its builds come from")
        return self

    @property
    def run(self) -> Run:
        return PLAIN if self.odoo is None else odoo.run(self.odoo.addons_path)

    def check(self, container: str) -> Check:
        if self.odoo is None:
            return Check(container, self.health_path)
        attachment = self.odoo.attachment
        return Check(container, self.health_path, odoo.check_command(self.odoo.modules, attachment.name, attachment.sha256))


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
