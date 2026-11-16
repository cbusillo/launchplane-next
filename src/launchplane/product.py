from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ProductError(Exception):
    pass


class Database(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: list[str] = Field(min_length=1)


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    health_path: str = Field(default="/", pattern=r"^/[^\s]*$")
    database: Database | None = None
    job: Job | None = None


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
