from dataclasses import dataclass, field

from launchplane.container_host import ContainerHostError


@dataclass
class GivenSecrets:
    on_host: frozenset[str] = frozenset()
    seen: frozenset[str] = frozenset()
    readable: bool = True
    reads: list[str] = field(default_factory=list[str])

    def read_secrets(self) -> frozenset[str]:
        self.reads.append("read")
        if not self.readable:
            raise ContainerHostError("Reading the secrets on test failed: Connection refused")
        self.seen |= self.on_host
        return self.seen

    def secrets_seen(self) -> frozenset[str]:
        return self.seen


NO_SECRETS = GivenSecrets()
