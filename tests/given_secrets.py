from dataclasses import dataclass


@dataclass
class GivenSecrets:
    seen: frozenset[str] = frozenset()

    def secrets_seen(self) -> frozenset[str]:
        return self.seen


NO_SECRETS = GivenSecrets()
