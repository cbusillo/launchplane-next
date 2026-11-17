import base64
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from scramp import ScramMechanism  # pyright: ignore[reportMissingTypeStubs]

DATABASE_ADDRESS = "database"
DATABASE_PORT = 5432
DATABASE_NAME = "app"
DATA_PATH = "/var/lib/postgresql"
# The image runs what is in this folder once, where it finds no data yet.
SET_UP_PATH = PurePosixPath("/docker-entrypoint-initdb.d/launchplane.sh")
THROWAWAY_PASSWORD_VARIABLE = "POSTGRES_PASSWORD"  # noqa: S105 the name of the variable, not a password
LOGIN_VARIABLE = "PGUSER"
CLIENT_PASSWORD_VARIABLE = "PGPASSWORD"  # noqa: S105 the name of the variable, not a password
PSQL = "psql --no-psqlrc --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1 --username=postgres"
# While the image sets a new server up it listens on no address. Asking over the network instead would write a refused
# connection into the server's log each time.
READY_COMMAND = ["sh", "-c", f'test "$({PSQL} --dbname=postgres --command="SHOW listen_addresses")" = "*"']

type AccessRule = tuple[str, str, str, str | None, str]
type RuleRead = tuple[str, list[str], list[str], str | None, str, str | None]

ACCESS_RULES: tuple[AccessRule, ...] = (
    ("local", "all", "postgres", None, "trust"),
    ("host", "all", "postgres", "all", "reject"),
    ("host", "app", "+app", "all", "scram-sha-256"),
    ("host", "all", "all", "all", "reject"),
)
RULES_AS_READ: list[RuleRead] = [
    (kind, [database], [user], address, method, None) for kind, database, user, address, method in ACCESS_RULES
]
RULES_FILE = "\n".join(" ".join(part for part in rule if part is not None) for rule in ACCESS_RULES)

SET_UP_SCRIPT = f"""\
{PSQL} --dbname=postgres <<'SQL'
CREATE ROLE app NOLOGIN;
CREATE DATABASE app OWNER app;
ALTER ROLE postgres PASSWORD NULL;
SQL
cat > "$PGDATA/pg_hba.conf" <<'RULES'
{RULES_FILE}
RULES
"""


@dataclass(frozen=True)
class Ask:
    statements: str
    database: str = DATABASE_NAME
    in_one_transaction: bool = False


READ_RULES = Ask(
    "SELECT coalesce(json_agg(json_build_array(type, database, user_name, address, auth_method, error) ORDER BY line_number), '[]') "
    "FROM pg_hba_file_rules;",
    database="postgres",
)
READ_LOGINS = Ask(
    "SELECT coalesce(json_agg(rolname ORDER BY rolname), '[]') FROM pg_roles JOIN pg_auth_members ON member = pg_roles.oid "
    "WHERE roleid = 'app'::regrole AND NOT rolsuper;"
)
MAKE_LOGIN = Ask(
    """CREATE ROLE :"login" LOGIN IN ROLE app PASSWORD :'verifier';
ALTER ROLE :"login" SET role = 'app';""",
    in_one_transaction=True,
)
# Each statement stands by itself. A login is closed before its sessions are ended, or the instance connects again in
# between, and dropping a login ends no session. A login that owns anything cannot be dropped.
REMOVE_LOGIN = Ask(
    """ALTER ROLE :"login" NOLOGIN;
SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity WHERE usename = :'login';
REASSIGN OWNED BY :"login" TO app;
DROP OWNED BY :"login";
DROP ROLE :"login";"""
)


# psql fills in a variable only in statements it reads from its standard input.
def psql_command(ask: Ask, **variables: str) -> list[str]:
    transaction = " --single-transaction" if ask.in_one_transaction else ""
    script = f"exec {PSQL} --dbname={ask.database}{transaction} \"$@\" <<'SQL'\n{ask.statements}\nSQL\n"
    return ["sh", "-c", script, "psql", *(f"--set={name}={value}" for name, value in variables.items())]


def variable_value(environment: list[str], variable: str) -> str | None:
    prefix = f"{variable}="
    return next((entry.removeprefix(prefix) for entry in environment if entry.startswith(prefix)), None)


def server_environment(throwaway_password: str) -> dict[str, str]:
    # The image refuses to start without a password unless it lets everyone in, so it gets one the set-up then removes.
    return {THROWAWAY_PASSWORD_VARIABLE: throwaway_password, "POSTGRES_HOST_AUTH_METHOD": "reject"}


# PostgreSQL cuts a name at 63 bytes, which would cut the random end off a long container name.
def login_of(instance: str) -> str:
    return f"i_{instance.rsplit('-', 1)[-1]}"


def client_environment(login: str, password: str) -> dict[str, str]:
    return {
        "PGHOST": DATABASE_ADDRESS,
        "PGPORT": str(DATABASE_PORT),
        "PGDATABASE": DATABASE_NAME,
        LOGIN_VARIABLE: login,
        CLIENT_PASSWORD_VARIABLE: password,
    }


def verifier_of(password: str) -> str:
    made = cast("tuple[bytes, bytes, bytes, int]", ScramMechanism("SCRAM-SHA-256").make_auth_info(password))  # pyright: ignore[reportUnknownMemberType]
    salt, stored_key, server_key = (base64.b64encode(part).decode() for part in made[:3])
    return f"SCRAM-SHA-256${made[3]}:{salt}${stored_key}:{server_key}"
