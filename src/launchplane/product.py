from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from launchplane.instance import Run
from launchplane.odoo import Odoo
from launchplane.steps import Check


class ProductError(Exception):
    pass


class Database(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str


class BackgroundService(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: list[str] = Field(min_length=1)


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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
