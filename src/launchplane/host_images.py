import logging
import os
import time
from collections.abc import Iterable
from contextlib import suppress

from docker import DockerClient
from docker.auth import resolve_repository_name
from docker.errors import APIError, ImageNotFound

from launchplane.docker_answers import ContainerHostRefused, attempting
from launchplane.host_secrets import HostSecrets
from launchplane.secret_boundary import cleaned


class HostImages:
    def __init__(self, client: DockerClient, address: str, secrets: HostSecrets) -> None:
        self.client = client
        self.address = address
        self.secrets = secrets
        self.registry = os.environ.get("LAUNCHPLANE_REGISTRY")
        self.login = {
            "username": os.environ.get("LAUNCHPLANE_REGISTRY_USER", ""),
            "password": os.environ.get("LAUNCHPLANE_REGISTRY_PASSWORD", ""),
        }
        if self.login["password"]:
            secrets.note([self.login["password"]])

    def ensure(self, image: str, *, published: bool = False) -> None:
        with suppress(ImageNotFound):
            self.client.images.get(image)
            return
        action = f"Fetching image {image}"
        started = time.monotonic()
        try:
            with attempting(action, self.address):
                for event in self.pull(image, published=published):
                    if reason := event.get("error"):
                        raise APIError(reason)
                self.client.images.get(image)
        except ContainerHostRefused as error:
            raise ContainerHostRefused(cleaned(str(error), self.secrets.seen)) from None
        logging.getLogger(__name__).info("Fetched %s in %.2f s", image, time.monotonic() - started)

    def pull(self, image: str, *, published: bool) -> Iterable[dict[str, str]]:
        registry, _ = resolve_repository_name(image)
        login = self.login if published and registry == self.registry else {}
        # Docker streams the registry's answer with a separate timeout, including refusals inside an HTTP 200.
        return self.client.api.pull(image, stream=True, decode=True, auth_config=login)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
