import os
import shlex
import socket
import subprocess
import sys

CONTROL_SHELL = shlex.split(os.environ.get("LAUNCHPLANE_CONTROL_SHELL", "lp-control"))
SCRATCH_SHELL = shlex.split(os.environ.get("LAUNCHPLANE_SCRATCH_SHELL", "lp-scratch"))
SCRATCH_HOST_NAME = "docker-lp-scratch.shiny"
NEW_KEY = "openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256"

ENSURE_AUTHORITY = f"""
set -eu; umask 077; mkdir -p /root/launchplane-ca; cd /root/launchplane-ca
[ -f ca-key.pem ] || {NEW_KEY} -out ca-key.pem
[ -f ca.pem ] || openssl req -x509 -new -key ca-key.pem -days 3650 -subj "/CN=Launchplane test bed CA" \\
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -out ca.pem
cat ca.pem
"""

REQUEST_IF_MISSING = f"""
set -eu; install -d -o "$OWNER" -g "$OWNER" -m 700 "$DIRECTORY"; cd "$DIRECTORY"
printf '%s\\n' "$AUTHORITY" > ca.pem
[ -f key.pem ] || (umask 077; {NEW_KEY} -out key.pem)
chown "$OWNER:$OWNER" ca.pem key.pem
[ -f cert.pem ] || openssl req -new -key key.pem -subj "/CN=$COMMON_NAME"
"""

SIGN = """
set -eu; cd /root/launchplane-ca; printf '%b' "$EXTENSIONS" > request.ext
openssl x509 -req -in /dev/stdin -CA ca.pem -CAkey ca-key.pem -CAcreateserial -days 3650 -extfile request.ext 2>/dev/null
"""

INSTALL = 'set -eu; cd "$DIRECTORY"; cat > cert.pem; chown "$OWNER:$OWNER" cert.pem; chmod 644 cert.pem'

LISTEN_ON_PRIVATE_ADDRESS = """
set -eu; override=/etc/systemd/system/docker.service.d/launchplane-tls.conf; mkdir -p "$(dirname "$override")"
printf '[Service]\\nExecStart=\\nExecStart=%s\\n' "$DOCKERD" > "$override.new"
if cmp -s "$override.new" "$override"; then rm "$override.new"; exit; fi
mv "$override.new" "$override"; systemctl daemon-reload; systemctl restart docker; echo restarted
"""


class IssueError(Exception):
    pass


def run_remote(shell: list[str], script: str, environment: dict[str, str], stdin: str = "") -> str:
    exports = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in environment.items())
    result = subprocess.run([*shell, exports + script], input=stdin, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise IssueError(f"{' '.join(shell)} failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout


def issue_once(shell: list[str], directory: str, owner: str, common_name: str, extensions: str, authority: str) -> str:
    place = {"DIRECTORY": directory, "OWNER": owner}
    request = run_remote(shell, REQUEST_IF_MISSING, {**place, "AUTHORITY": authority, "COMMON_NAME": common_name})
    if not request.strip():
        return "kept"
    certificate = run_remote(CONTROL_SHELL, SIGN, {"EXTENSIONS": extensions}, stdin=request)
    run_remote(shell, INSTALL, place, stdin=certificate)
    return "issued"


def dockerd_command(address: str) -> str:
    return (
        f"/usr/bin/dockerd -H fd:// -H tcp://{address}:2376 --tlsverify --tlscacert=/etc/docker/tls/ca.pem"
        " --tlscert=/etc/docker/tls/cert.pem --tlskey=/etc/docker/tls/key.pem --containerd=/run/containerd/containerd.sock"
    )


def issue() -> None:
    address = socket.gethostbyname(SCRATCH_HOST_NAME)
    authority = run_remote(CONTROL_SHELL, ENSURE_AUTHORITY, {}).strip()
    server_extensions = f"subjectAltName=DNS:{SCRATCH_HOST_NAME},IP:{address}\\nextendedKeyUsage=serverAuth\\n"
    server = issue_once(SCRATCH_SHELL, "/etc/docker/tls", "root", SCRATCH_HOST_NAME, server_extensions, authority)
    sys.stdout.write(f"server certificate: {server}\n")
    client_extensions = "extendedKeyUsage=clientAuth\\n"
    client = issue_once(CONTROL_SHELL, "/var/lib/launchplane/tls", "launchplane", "launchplane", client_extensions, authority)
    sys.stdout.write(f"client certificate: {client}\n")
    restarted = run_remote(SCRATCH_SHELL, LISTEN_ON_PRIVATE_ADDRESS, {"DOCKERD": dockerd_command(address)}).strip()
    sys.stdout.write(f"Docker on {address}:2376: {restarted or 'kept'}\n")


def main() -> int:
    try:
        issue()
    except IssueError as error:
        sys.stderr.write(f"Issuing the test bed's certificates stopped: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
