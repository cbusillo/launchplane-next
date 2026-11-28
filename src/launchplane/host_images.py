import asyncio
import json
import logging
import os
import ssl
import time
from contextlib import suppress

from aiodocker import Docker, DockerError
from aiohttp import ClientError
from docker import DockerClient
from docker.auth import resolve_repository_name
from docker.errors import ImageNotFound

from launchplane.docker_answers import ContainerHostError, ContainerHostRefused, attempting
from launchplane.host_secrets import HostSecrets
from launchplane.secret_boundary import cleaned

PULL_SECONDS = 180.0


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
        action = f"Fetching image {image} on {self.address}"
        with attempting(f"Looking up image {image}", self.address), suppress(ImageNotFound):
            self.client.images.get(image)
            return
        started = time.monotonic()
        try:
            asyncio.run(self.pull(image, published=published))
            with attempting(f"Verifying fetched image {image}", self.address):
                self.client.images.get(image)
        except TimeoutError:
            raise ContainerHostRefused(f"{action} exceeded its {PULL_SECONDS:g} s deadline.") from None
        except (DockerError, ClientError, ContainerHostError, OSError, json.JSONDecodeError) as error:
            raise ContainerHostRefused(f"{action} failed: {cleaned(str(error), self.secrets.seen)}") from None
        logging.getLogger(__name__).info("Fetched %s in %.2f s", image, time.monotonic() - started)

    async def pull(self, image: str, *, published: bool) -> None:
        api = self.client.api
        tls = None
        if isinstance(api.cert, tuple):
            tls = ssl.create_default_context(cafile=api.verify if isinstance(api.verify, str) else None)
            tls.load_cert_chain(*api.cert)
        registry, _ = resolve_repository_name(image)
        login = self.login if published and registry == self.registry else None
        async with asyncio.timeout(PULL_SECONDS), Docker(url=api.base_url, ssl_context=tls) as docker:
            async for _ in docker.images.pull(image, auth=login, stream=True):
                pass
