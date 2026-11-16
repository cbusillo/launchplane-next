from pathlib import PurePosixPath

DATABASE_ADDRESS = "database"
DATABASE_PORT = 5432
DATA_PATH = "/var/lib/postgresql"
PASSWORD_PATH = PurePosixPath(DATA_PATH) / "launchplane-password"
SERVER_ENVIRONMENT = {"POSTGRES_PASSWORD_FILE": str(PASSWORD_PATH)}
DATA_FOLDER_VARIABLE = "PGDATA"
# PostgreSQL writes this file when it first makes a database, so it tells data that exists from a new volume.
MADE_WITH_THE_DATA = "PG_VERSION"
READY_COMMAND = ["pg_isready", "--host=127.0.0.1", "--username=postgres", "--quiet"]
CLIENT_PASSWORD_VARIABLE = "PGPASSWORD"  # noqa: S105 the name of the variable, not a password


def variable_value(environment: list[str], variable: str) -> str | None:
    prefix = f"{variable}="
    return next((entry.removeprefix(prefix) for entry in environment if entry.startswith(prefix)), None)


def client_environment(password: str) -> dict[str, str]:
    return {
        "PGHOST": DATABASE_ADDRESS,
        "PGPORT": str(DATABASE_PORT),
        "PGUSER": "postgres",
        "PGDATABASE": "postgres",
        CLIENT_PASSWORD_VARIABLE: password,
    }
