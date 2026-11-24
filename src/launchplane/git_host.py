import io
import os
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from githubkit import GitHub
from githubkit.exception import GitHubException
from pydantic import AliasPath, BaseModel, Field, ValidationError

from launchplane.product import Product

TOKEN_VARIABLE = "LAUNCHPLANE_GIT_HOST_TOKEN"  # noqa: S105 the name of the variable, not a token


class GitHostError(Exception):
    pass


# The git host answered, and what was asked for is not a build of the product.
class NotABuild(GitHostError):
    pass


class Manifest(BaseModel):
    source_commit: str
    repository: str = Field(validation_alias=AliasPath("image", "repository"))
    digest: str = Field(validation_alias=AliasPath("image", "digest"), pattern=r"^sha256:[0-9a-f]{64}$")


def from_environment() -> GitHub[Any]:
    return GitHub(os.environ.get(TOKEN_VARIABLE), base_url=os.environ.get("LAUNCHPLANE_GIT_HOST"), timeout=20)


# What a move runs. A product that says where its builds come from runs only the image one run there published.
@dataclass(frozen=True)
class Builds:
    read_product: Callable[[str], Product] = lambda _product: Product()
    git_host: GitHub[Any] = field(default_factory=from_environment)

    def asked(self, product: str, build: str | None, run: int | None) -> str:
        source = self.read_product(product).odoo
        if source is not None and run is not None and build is None:
            return self.published(source.repository, source.workflow, source.image, run)
        if source is None and build is not None and run is None:
            return build
        named = '"build": "<image>"' if source is None else f'"run": <number>, of a run of {source.workflow} in {source.repository}'
        raise NotABuild(f"A move of {product} names its build as {{{named}}}, and in no other way. Nothing was changed.")

    # A run can pass and publish nothing. One that published has a manifest for its attempt, which names the image by digest.
    def published(self, repository: str, workflow: str, image: str, run: int) -> str:
        actions, (owner, name) = self.git_host.rest.actions, repository.split("/")
        try:
            found = actions.get_workflow_run(owner, name, run).parsed_data
            artifacts = actions.list_workflow_run_artifacts(owner, name, run).parsed_data.artifacts
            manifests = [each.id for each in artifacts if each.name == f"artifact-manifest-{found.run_attempt}"]
            if not manifests:
                raise NotABuild(f"Run {run} of {repository} published no image: it has no manifest for attempt {found.run_attempt}.")
            archive = zipfile.ZipFile(io.BytesIO(actions.download_artifact(owner, name, manifests[0], "zip").content))
            manifest = Manifest.model_validate_json(archive.read("artifact-manifest.json"))
        except (GitHubException, zipfile.BadZipFile, KeyError, ValidationError) as error:
            raise GitHostError(
                f"Reading run {run} of {repository} and its manifest failed, so no build was verified and nothing was changed: "
                f"{error}. Launchplane reads the git host with the key in {TOKEN_VARIABLE}."
            ) from error
        facts = {
            "its repository": (found.repository.full_name, repository),
            "the repository of its commit": (found.head_repository.full_name, repository),
            "its workflow": (found.path, workflow),
            "its conclusion": (found.conclusion, "success"),
            "the commit in its manifest": (manifest.source_commit, found.head_sha),
            "the image repository in its manifest": (manifest.repository, image),
        }
        wrong = [f"{what} is {said}, not {must}" for what, (said, must) in facts.items() if said != must]
        if wrong:
            raise NotABuild(f"Run {run} is not a build of this product, so it was refused: {'; '.join(wrong)}. Nothing was changed.")
        return f"{image}@{manifest.digest}"
