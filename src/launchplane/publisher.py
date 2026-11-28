from pydantic import BaseModel, Field


class Publisher(BaseModel, extra="forbid", frozen=True):
    repository: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    workflow: str
    image: str
