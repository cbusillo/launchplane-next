from dataclasses import dataclass

from launchplane.product import Database

DATABASE_ADDRESS = "database"
DATABASE_PORT = 5432


def variable_value(environment: list[str], variable: str) -> str | None:
    prefix = f"{variable}="
    return next((entry.removeprefix(prefix) for entry in environment if entry.startswith(prefix)), None)


@dataclass(frozen=True)
class DatabaseServer:
    image: str
    data_path: str
    password_variable: str
    ready_command: list[str]
    user: str
    database: str

    def server_environment(self, password: str) -> dict[str, str]:
        return {self.password_variable: password}

    def password_in(self, environment: list[str]) -> str | None:
        return variable_value(environment, self.password_variable)

    def client_environment(self, password: str) -> dict[str, str]:
        return {
            "PGHOST": DATABASE_ADDRESS,
            "PGPORT": str(DATABASE_PORT),
            "PGUSER": self.user,
            "PGDATABASE": self.database,
            "PGPASSWORD": password,
        }


def postgres_server(database: Database) -> DatabaseServer:
    return DatabaseServer(
        image=database.image,
        data_path="/var/lib/postgresql",
        password_variable="POSTGRES_PASSWORD",  # noqa: S106 the name of the variable, not a password
        ready_command=["pg_isready", "--host=127.0.0.1", "--username=postgres", "--quiet"],
        user="postgres",
        database="postgres",
    )
