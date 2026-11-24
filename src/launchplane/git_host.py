import io
import os
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from githubkit import GitHub
from githubkit.exception import GitHubException
from pydantic import BaseModel, Field, ValidationError

from launchplane.product import BuildSource, Product

TOKEN_VARIABLE = "LAUNCHPLANE_GIT_HOST_TOKEN"  # noqa: S105 the name of the variable, not a token
ADDRESS_VARIABLE = "LAUNCHPLANE_GIT_HOST"
ANSWER_TIMEOUT_SECONDS = 20


class GitHostError(Exception):
    pass


# The git host answered, and what was asked for is not a build of the product.
class NotABuild(GitHostError):
    pass


class PublishedImage(BaseModel):
    repository: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class Manifest(BaseModel):
    source_commit: str
    image: PublishedImage


def at(address: str | None, token: str | None = None) -> GitHub[Any]:
    return GitHub(token, base_url=address, timeout=ANSWER_TIMEOUT_SECONDS)


@dataclass(frozen=True)
class GitHost:
    client: GitHub[Any] = field(default_factory=lambda: at(os.environ.get(ADDRESS_VARIABLE), os.environ.get(TOKEN_VARIABLE)))

    # The image one run of the product's workflow published, named by its digest. A run can pass and publish nothing.
    def published(self, source: BuildSource, run: int) -> str:
        actions = self.client.rest.actions
        owner, name = source.repository.split("/")
        try:
            found = actions.get_workflow_run(owner, name, run).parsed_data
            manifests = [
                each.id
                for each in actions.list_workflow_run_artifacts(owner, name, run).parsed_data.artifacts
                if each.name == f"artifact-manifest-{found.run_attempt}"
            ]
            if not manifests:
                raise NotABuild(
                    f"Run {run} of {source.repository} has no manifest of attempt {found.run_attempt}: it published no image."
                )
            archive = zipfile.ZipFile(io.BytesIO(actions.download_artifact(owner, name, manifests[0], "zip").content))
            manifest = Manifest.model_validate_json(archive.read("artifact-manifest.json"))
        except (GitHubException, zipfile.BadZipFile, KeyError, ValidationError) as error:
            raise GitHostError(
                f"Reading run {run} of {source.repository} and its manifest failed, so no build was verified and nothing was "
                f"changed: {error}. Launchplane reads the git host with the key in {TOKEN_VARIABLE}."
            ) from error
        facts = {
            "its repository": (found.repository.full_name, source.repository),
            "the repository of its commit": (found.head_repository.full_name, source.repository),
            "its workflow": (found.path, source.workflow),
            "its conclusion": (found.conclusion, "success"),
            "the commit in its manifest": (manifest.source_commit, found.head_sha),
            "the image repository in its manifest": (manifest.image.repository, source.image),
        }
        wrong = [f"{what} is {said}, not {must}" for what, (said, must) in facts.items() if said != must]
        if wrong:
            raise NotABuild(f"Run {run} is not a build of this product, so it was refused: {'; '.join(wrong)}. Nothing was changed.")
        return f"{source.image}@{manifest.image.digest}"


# What a move runs. A product that says where its builds come from runs only what a run there published.
@dataclass(frozen=True)
class Builds:
    read_product: Callable[[str], Product] = lambda _product: Product()
    git_host: GitHost = field(default_factory=GitHost)

    def asked(self, product: str, build: str | None, run: int | None) -> str:
        source = self.read_product(product).build_from
        if source is not None and run is not None and build is None:
            return self.git_host.published(source, run)
        if source is None and build is not None and run is None:
            return build
        named = (
            '{"build": "<image>"}' if source is None else f'{{"run": <number>}}, a run of {source.workflow} in {source.repository}'
        )
        raise NotABuild(f"A move of {product} names its build as {named}, and nothing else. Nothing was changed.")
