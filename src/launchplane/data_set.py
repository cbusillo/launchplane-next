from dataclasses import dataclass

from launchplane.product import Database

DATABASE_ADDRESS = "database"


@dataclass(frozen=True)
class DatabaseServer:
    image: str
    data_path: str
    environment: dict[str, str]
    ready_command: list[str]


def postgres_server(database: Database) -> DatabaseServer:
    return DatabaseServer(
        image=database.image,
        data_path="/var/lib/postgresql",
        environment={"POSTGRES_HOST_AUTH_METHOD": "trust"},
        ready_command=["pg_isready", "--host=127.0.0.1", "--username=postgres", "--quiet"],
    )
