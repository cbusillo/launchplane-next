from typing import Annotated

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class Publisher:
    repository: Annotated[str, Field(pattern=r"^[\w.-]+/[\w.-]+$")]
    workflow: str
    image: str
