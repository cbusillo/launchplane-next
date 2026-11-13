import argparse
import os
import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass

CONTROL_SHELL = shlex.split(os.environ.get("LAUNCHPLANE_CONTROL_SHELL", "lp-control"))
SCRATCH_SHELL = shlex.split(os.environ.get("LAUNCHPLANE_SCRATCH_SHELL", "lp-scratch"))
SCRATCH_HOST_NAME = "docker-lp-scratch.shiny"
DOCKER_TLS_PORT = 2376
DOCKER_OVERRIDE = "/etc/systemd/system/docker.service.d/launchplane-tls.conf"

VALID_DAYS = 3650
RENEW_WITHIN_SECONDS = 365 * 24 * 60 * 60
AUTHORITY_DIRECTORY = "/var/lib/launchplane/ca"
SERVICE_USER = "launchplane"

NEW_EC_KEY = "openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256"

ENSURE_AUTHORITY = f"""
set -eu
install -d -o {SERVICE_USER} -g {SERVICE_USER} -m 700 {AUTHORITY_DIRECTORY}
cd {AUTHORITY_DIRECTORY}
authority_is_good() {{
  [ -f ca-key.pem ] && [ -f ca.pem ] || return 1
  [ "$(openssl pkey -in ca-key.pem -pubout)" = "$(openssl x509 -in ca.pem -noout -pubkey)" ] || return 1
  openssl x509 -in ca.pem -noout -checkend {RENEW_WITHIN_SECONDS} >/dev/null
}}
if [ "$REPLACE" = 1 ] || ! authority_is_good; then
  runuser -u {SERVICE_USER} -- sh -c 'set -eu; umask 077
    {NEW_EC_KEY} -out ca-key.pem.new
    openssl req -x509 -new -key ca-key.pem.new -days {VALID_DAYS} -subj "/CN=Launchplane test bed CA" \\
      -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca.pem.new
    mv ca-key.pem.new ca-key.pem; mv ca.pem.new ca.pem'
  echo issued >&2
else
  echo kept >&2
fi
chmod 600 ca-key.pem; chmod 644 ca.pem
cat ca.pem
"""

PREPARE_LEAF = f"""
set -eu
install -d -o "$OWNER" -g "$OWNER" -m 700 "$DIRECTORY"
cd "$DIRECTORY"
printf '%s\\n' "$AUTHORITY" > ca.pem.new
if cmp -s ca.pem.new ca.pem; then rm ca.pem.new; else mv ca.pem.new ca.pem; fi
if [ "$REPLACE" = 1 ] || [ ! -f key.pem ]; then (umask 077; {NEW_EC_KEY} -out key.pem); fi
chown "$OWNER:$OWNER" ca.pem key.pem; chmod 644 ca.pem; chmod 600 key.pem
certificate_is_good() {{
  [ -f cert.pem ] || return 1
  openssl verify -CAfile ca.pem cert.pem >/dev/null 2>&1 || return 1
  [ "$(openssl pkey -in key.pem -pubout)" = "$(openssl x509 -in cert.pem -noout -pubkey)" ] || return 1
  openssl x509 -in cert.pem -noout -checkend {RENEW_WITHIN_SECONDS} >/dev/null || return 1
  [ -z "$CHECK_IP" ] || openssl x509 -in cert.pem -noout -checkip "$CHECK_IP" | grep -q "does match"
}}
if ! certificate_is_good; then openssl req -new -key key.pem -subj "/CN=$COMMON_NAME"; fi
"""

SIGN_REQUEST = f"""
set -eu
cd {AUTHORITY_DIRECTORY}
cat > request.csr
printf '%b' "$EXTENSIONS" > request.ext
chown {SERVICE_USER}:{SERVICE_USER} request.csr request.ext
runuser -u {SERVICE_USER} -- openssl x509 -req -in request.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial \\
  -days {VALID_DAYS} -extfile request.ext 2>/dev/null
rm request.csr request.ext
"""

INSTALL_CERTIFICATE = """
set -eu
cd "$DIRECTORY"
cat > cert.pem
chown "$OWNER:$OWNER" cert.pem; chmod 644 cert.pem
"""

ENSURE_DOCKER_LISTENS = """
set -eu
mkdir -p "$(dirname "$OVERRIDE_PATH")"
override="$OVERRIDE_PATH"
printf '%s\\n' "$OVERRIDE" > "$override.new"
if cmp -s "$override.new" "$override"; then rm "$override.new"; else mv "$override.new" "$override"; echo changed; fi
"""

RESTART_IF_OLDER_THAN_FILES = """
set -eu
systemctl is-enabled --quiet "$SERVICE" 2>/dev/null || exit 0
started=$(date -d "$(systemctl show -p ActiveEnterTimestamp --value "$SERVICE")" +%s 2>/dev/null || echo 0)
newest=$(stat -c %Y $FILES | sort -n | tail -1)
if [ "$newest" -gt "$started" ] || [ "$(systemctl show -p NeedDaemonReload --value "$SERVICE")" = yes ]; then
  systemctl daemon-reload; systemctl restart "$SERVICE"; systemctl is-active --quiet "$SERVICE"; echo restarted
fi
"""


class IssueError(Exception):
    pass


@dataclass(frozen=True)
class Leaf:
    label: str
    shell: list[str]
    directory: str
    owner: str
    common_name: str
    check_ip: str
    extensions: str


def run_remote(shell: list[str], script: str, environment: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    exports = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in environment.items())
    result = subprocess.run([*shell, exports + script], input=stdin, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise IssueError(f"{' '.join(shell)} failed (exit {result.returncode}): {result.stderr.strip()}")
    return result


def ensure_authority(replace: bool) -> str:
    result = run_remote(CONTROL_SHELL, ENSURE_AUTHORITY, {"REPLACE": str(int(replace))})
    report("certificate authority", result.stderr.strip())
    return result.stdout.strip()


def ensure_leaf(leaf: Leaf, authority: str, replace: bool) -> None:
    environment = {
        "DIRECTORY": leaf.directory,
        "OWNER": leaf.owner,
        "AUTHORITY": authority,
        "REPLACE": str(int(replace)),
        "COMMON_NAME": leaf.common_name,
        "CHECK_IP": leaf.check_ip,
    }
    request = run_remote(leaf.shell, PREPARE_LEAF, environment).stdout
    if not request.strip():
        report(leaf.label, "kept")
        return
    certificate = run_remote(CONTROL_SHELL, SIGN_REQUEST, {"EXTENSIONS": leaf.extensions}, stdin=request).stdout
    run_remote(leaf.shell, INSTALL_CERTIFICATE, {"DIRECTORY": leaf.directory, "OWNER": leaf.owner}, stdin=certificate)
    report(leaf.label, "issued")


def docker_override(address: str) -> str:
    return "\n".join(
        [
            "[Service]",
            "ExecStart=",
            f"ExecStart=/usr/bin/dockerd -H fd:// -H tcp://{address}:{DOCKER_TLS_PORT} --tlsverify"
            " --tlscacert=/etc/docker/tls/ca.pem --tlscert=/etc/docker/tls/cert.pem --tlskey=/etc/docker/tls/key.pem"
            " --containerd=/run/containerd/containerd.sock",
        ]
    )


def ensure_docker_listens(address: str) -> None:
    changed = bool(
        run_remote(
            SCRATCH_SHELL, ENSURE_DOCKER_LISTENS, {"OVERRIDE": docker_override(address), "OVERRIDE_PATH": DOCKER_OVERRIDE}
        ).stdout.strip()
    )
    report(f"Docker listening on {address}:{DOCKER_TLS_PORT}", "changed" if changed else "kept")


def restart_if_older_than(shell: list[str], service: str, files: str) -> None:
    restarted = bool(run_remote(shell, RESTART_IF_OLDER_THAN_FILES, {"SERVICE": service, "FILES": files}).stdout.strip())
    report(f"{service} service", "restarted" if restarted else "kept")


def report(item: str, outcome: str) -> None:
    sys.stdout.write(f"{item}: {outcome}\n")


def issue(replace: bool) -> None:
    scratch_address = socket.gethostbyname(SCRATCH_HOST_NAME)
    authority = ensure_authority(replace)
    server = Leaf(
        label=f"{SCRATCH_HOST_NAME} server certificate",
        shell=SCRATCH_SHELL,
        directory="/etc/docker/tls",
        owner="root",
        common_name=SCRATCH_HOST_NAME,
        check_ip=scratch_address,
        extensions=f"subjectAltName=DNS:{SCRATCH_HOST_NAME},IP:{scratch_address}\\nextendedKeyUsage=serverAuth\\n"
        "keyUsage=critical,digitalSignature\\n",
    )
    client = Leaf(
        label="launchplane client certificate",
        shell=CONTROL_SHELL,
        directory="/var/lib/launchplane/tls",
        owner=SERVICE_USER,
        common_name=SERVICE_USER,
        check_ip="",
        extensions="extendedKeyUsage=clientAuth\\nkeyUsage=critical,digitalSignature\\n",
    )
    ensure_leaf(server, authority, replace)
    ensure_docker_listens(scratch_address)
    restart_if_older_than(SCRATCH_SHELL, "docker", f"/etc/docker/tls/*.pem {DOCKER_OVERRIDE}")
    ensure_leaf(client, authority, replace)
    restart_if_older_than(CONTROL_SHELL, "launchplane", "/var/lib/launchplane/tls/*.pem")


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue the test bed's certificates. A second run changes nothing.")
    parser.add_argument("--replace", action="store_true", help="make new keys and certificates everywhere, including the authority")
    try:
        issue(parser.parse_args().replace)
    except IssueError as error:
        sys.stderr.write(f"Issuing the test bed's certificates stopped: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
