import json
import logging
import sys
from collections.abc import Collection
from typing import Protocol, TextIO, cast, override

SECRET_REMOVED = "[a secret Launchplane gave this lane, removed]"  # noqa: S105 what replaces a secret, not one
JSON = "application/json"


class Secrets(Protocol):
    def read_secrets(self) -> frozenset[str]: ...
    def secrets_seen(self) -> frozenset[str]: ...


def cleaned(text: str, secrets: Collection[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, SECRET_REMOVED)
    return text


def cleaned_value(value: object, secrets: Collection[str]) -> object:
    if isinstance(value, str):
        return cleaned(value, secrets)
    if isinstance(value, list):
        return [cleaned_value(each, secrets) for each in cast("list[object]", value)]
    if isinstance(value, dict):
        return {cleaned(key, secrets): cleaned_value(each, secrets) for key, each in cast("dict[str, object]", value).items()}
    return value


def cleaned_body(body: bytes, media_type: str, secrets: Collection[str]) -> bytes:
    text = body.decode(errors="replace")
    if media_type.startswith(JSON):
        return json.dumps(cleaned_value(json.loads(text), secrets)).encode()
    return cleaned(text, secrets).encode()


class CleanedStream(logging.StreamHandler[TextIO]):
    def __init__(self, secrets: Secrets, stream: TextIO) -> None:
        super().__init__(stream)
        self.secrets = secrets

    @override
    def format(self, record: logging.LogRecord) -> str:
        return cleaned(super().format(record), self.secrets.secrets_seen())

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        sys.stderr.write(f"A log line from {record.name} could not be written, and is not repeated here uncleaned.\n")
