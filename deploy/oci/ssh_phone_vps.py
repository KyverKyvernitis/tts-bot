#!/usr/bin/python3
"""Provision Osaka's restricted SSH forwards without opening worker HTTP ports."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import stat
import struct
import subprocess
import sys
import time
import urllib.request

MARKER = "Managed by Osaka Phone SSH v1."
SCHEMA = "osaka-phone-ssh-v1"
USER = "osaka-phone-ssh"
CONFIG_PATH = Path("/etc/osaka/phone-ssh.json")
DROPIN_PATH = Path("/etc/ssh/sshd_config.d/90-osaka-phone.conf")
HOME_PATH = Path("/var/lib/osaka-phone-ssh")
BACKUP_PATH = Path("/etc/osaka/ssh-backups")
HOST_PUBLIC_KEY_PATH = Path("/etc/ssh/ssh_host_ed25519_key.pub")
WORKER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
ENV_KEYS = ("PHONE_WORKER_TRANSPORT", "PHONE_WORKER_HOST", "PHONE_WORKER_PORT",
            "PHONE_WORKER_BULK_PORT", "PHONE_WORKER_ID", "CORE_WORKER_SSH_LOCAL_PORT",
            "PHONE_WORKER_SCHEME", "PHONE_WORKER_ENABLED")


def run(args, *, check=True, timeout=10):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        # Do not echo command lines or server configuration into diagnostic output.
        raise RuntimeError(f"{Path(args[0]).name}: status {result.returncode}")
    return result


def public_key(text):
    """Accept a single Ed25519 public key; strip comments and reject injected options."""
    if not isinstance(text, str) or len(text) > 8192 or "\n" in text.strip() or "\r" in text:
        raise ValueError("chave pública deve ocupar uma linha")
    parts = text.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("use uma chave pública Ed25519")
    try:
        raw = base64.b64decode(parts[1], validate=True)
        fields = []
        offset = 0
        while offset < len(raw):
            size = struct.unpack("!I", raw[offset:offset + 4])[0]
            offset += 4
            fields.append(raw[offset:offset + size])
            offset += size
        if offset != len(raw) or fields[0] != b"ssh-ed25519" or len(fields) != 2 or len(fields[1]) != 32:
            raise ValueError
    except (ValueError, IndexError, struct.error):
        raise ValueError("conteúdo da chave Ed25519 inválido") from None
    return "ssh-ed25519 " + base64.b64encode(raw).decode("ascii")


def fingerprint(key):
    digest = hashlib.sha256(base64.b64decode(public_key(key).split()[1])).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def port(value):
    value = int(value)
    if not 1024 <= value <= 65535:
        raise ValueError("porta deve estar entre 1024 e 65535")
    return value


def validated_bundle(worker_id, host, *, ssh_port=22, remote_port=18766,
                     bulk_remote_port=18767, local_port=19000, vps_api_port=10000,
                     host_key):
    if not WORKER_RE.fullmatch(worker_id):
        raise ValueError("worker_id inválido")
    if not host or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host) or host.startswith("-"):
        raise ValueError("host SSH deve ser IP ou nome DNS, sem URL ou opções")
    ssh_port = int(ssh_port)
    if not 1 <= ssh_port <= 65535:
        raise ValueError("porta SSH inválida")
    remote_port, bulk_remote_port, local_port, vps_api_port = map(port, (
        remote_port, bulk_remote_port, local_port, vps_api_port))
    if len({remote_port, bulk_remote_port, local_port, vps_api_port, ssh_port}) != 5:
        raise ValueError("as portas configuradas precisam ser distintas")
    return {"schema": SCHEMA, "worker_id": worker_id, "ssh_host": host,
            "ssh_port": ssh_port, "ssh_user": USER, "ssh_host_key": public_key(host_key),
            "remote_port": remote_port, "bulk_remote_port": bulk_remote_port,
            "local_port": local_port, "vps_api_port": vps_api_port}


def guarded_path(path, *, uid=0):
    """Trusted system paths may not traverse symlinks or writable parents."""
    path = Path(path)
    for candidate in reversed((path, *path.parents)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        # Application environments normally belong to ubuntu. Their ancestors
        # may belong to root or that same user; no other owner is trusted.
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in {0, uid} or info.st_mode & 0o022:
            raise RuntimeError("caminho inseguro: " + str(candidate))
        if candidate != path and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("pai não é diretório: " + str(candidate))


@contextmanager
def parent_descriptor(path, *, uid=0, create=False):
    """Pin trusted ancestors; a renamed parent cannot redirect privileged I/O."""
    path = Path(os.path.abspath(path))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            info = os.fstat(descriptor)
            if info.st_uid not in {0, uid} or info.st_mode & 0o022:
                raise RuntimeError("diretório inseguro para a configuração")
        yield descriptor
    finally:
        os.close(descriptor)


def atomic_write(path, raw, *, mode=0o644, uid=0, gid=0, expected=None):
    path = Path(path)
    guarded_path(path, uid=uid)
    with parent_descriptor(path, uid=uid, create=True) as directory:
        if expected is not None:
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_mtime_ns", "st_size")
            if any(getattr(current, key) != getattr(expected, key) for key in fields):
                raise RuntimeError("o .env mudou durante a validação; execute novamente")
        temporary = ".osaka-" + secrets.token_hex(10)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.fchown(output.fileno(), uid, gid)
                os.fchmod(output.fileno(), mode)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def read_registry(path=None, *, guard=True):
    path = Path(path) if path is not None else CONFIG_PATH
    if guard:
        guarded_path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"marker": MARKER, "schema": 1, "workers": {}}
    if len(raw) > 256 * 1024:
        raise ValueError("registro SSH excede limite")
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("marker") != MARKER or result.get("schema") != 1 or not isinstance(result.get("workers"), dict):
        raise RuntimeError("registro SSH existente não pertence a este instalador")
    return result


@contextmanager
def installation_lock():
    """Serialize registry/key changes without a persistent privileged daemon."""
    path = CONFIG_PATH.parent / ".phone-ssh.lock"
    guarded_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
            raise RuntimeError("lock de instalação inseguro")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def dump_json(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def ssh_config(registry):
    records = list(registry["workers"].values())
    listens = sorted({f"127.0.0.1:{row[key]}" for row in records for key in ("remote_port", "bulk_remote_port")})
    opens = sorted({f"127.0.0.1:{row['vps_api_port']}" for row in records})
    return ("# " + MARKER + "\nMatch User " + USER + "\n"
            "    AuthenticationMethods publickey\n    PubkeyAuthentication yes\n"
            "    PasswordAuthentication no\n    KbdInteractiveAuthentication no\n"
            "    PermitEmptyPasswords no\n    AllowTcpForwarding yes\n"
            "    AllowStreamLocalForwarding no\n    GatewayPorts no\n"
            "    PermitListen " + " ".join(listens) + "\n"
            "    PermitOpen " + " ".join(opens) + "\n"
            "    PermitTTY no\n    X11Forwarding no\n    AllowAgentForwarding no\n"
            "    PermitTunnel no\n    PermitUserEnvironment no\n    PermitUserRC no\n"
            "    MaxSessions 0\n    ForceCommand /usr/sbin/nologin\n"
            "    AuthorizedKeysFile " + str(HOME_PATH / ".ssh/authorized_keys") + "\nMatch all\n").encode()


def authorized_keys(registry):
    lines = ["# " + MARKER]
    for worker_id, row in sorted(registry["workers"].items()):
        if not row.get("client_public_key"):
            continue
        key = public_key(row["client_public_key"])
        options = ('restrict,port-forwarding,command="/usr/sbin/nologin",'
                   f'permitopen="127.0.0.1:{row["vps_api_port"]}",'
                   f'permitlisten="127.0.0.1:{row["remote_port"]}",'
                   f'permitlisten="127.0.0.1:{row["bulk_remote_port"]}"')
        lines.append(options + " " + key + " osaka:" + worker_id)
    return ("\n".join(lines) + "\n").encode()


def verify_effective_ssh(registry, *, runner=run, sshd="/usr/sbin/sshd"):
    runner([sshd, "-t"])
    for ssh_port in {row["ssh_port"] for row in registry["workers"].values()}:
        result = runner([sshd, "-T", "-C", f"user={USER},host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport={ssh_port}"])
        values = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition(" ")
            values[key] = value.strip()
        expected = {"passwordauthentication": "no", "kbdinteractiveauthentication": "no",
                    "authenticationmethods": "publickey", "pubkeyauthentication": "yes",
                    "permittty": "no", "gatewayports": "no", "allowagentforwarding": "no",
                    "x11forwarding": "no", "forcecommand": "/usr/sbin/nologin",
                    "allowstreamlocalforwarding": "no", "permittunnel": "no", "maxsessions": "0",
                    "authorizedkeysfile": str(HOME_PATH / ".ssh/authorized_keys")}
        if any(values.get(k) != v for k, v in expected.items()):
            raise RuntimeError("sshd efetivo não aplicou as restrições; configuração restaurada")
        if values.get("allowtcpforwarding") not in {"yes", "all"}:
            raise RuntimeError("encaminhamento SSH não permitido na configuração efetiva")
        expected_listens = {f"127.0.0.1:{row[key]}" for row in registry["workers"].values() for key in ("remote_port", "bulk_remote_port")}
        expected_opens = {f"127.0.0.1:{row['vps_api_port']}" for row in registry["workers"].values()}
        if set(values.get("permitlisten", "").split()) != expected_listens or set(values.get("permitopen", "").split()) != expected_opens:
            raise RuntimeError("portas efetivas SSH divergem das portas privadas cadastradas")


def registry_changes(registry):
    return {CONFIG_PATH: dump_json(registry), DROPIN_PATH: ssh_config(registry),
            HOME_PATH / ".ssh/authorized_keys": authorized_keys(registry)}


def validate_registry_targets(registry):
    """Fail on unmanaged configuration before creating the dedicated account."""
    for path in registry_changes(registry):
        guarded_path(path)
        if path.exists() and path != CONFIG_PATH:
            lines = path.read_bytes().splitlines()
            if not lines or MARKER.encode() not in lines[0]:
                raise RuntimeError("arquivo existente não gerenciado: " + str(path))


def commit_registry(registry, *, runner=run):
    changes = registry_changes(registry)
    validate_registry_targets(registry)
    previous = {}
    updated = {}
    for path in changes:
        guarded_path(path)
        if path.exists():
            raw = path.read_bytes()
            previous[path] = (raw, stat.S_IMODE(path.stat().st_mode))
            if raw != changes[path]:
                updated[path] = changes[path]
        else:
            previous[path] = None
            updated[path] = changes[path]
    try:
        for path, raw in updated.items():
            atomic_write(path, raw)
        verify_effective_ssh(registry, runner=runner)
        if updated:
            runner(["systemctl", "reload", "ssh.service"])
    except Exception:
        for path in updated:
            old = previous[path]
            if old is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, old[0], mode=old[1])
        raise
    return bool(updated)


def ensure_account(*, runner=run):
    marker = HOME_PATH / ".osaka-managed"
    try:
        account = pwd.getpwnam(USER)
    except KeyError:
        account = None
    if account is not None:
        if not marker.exists() or marker.read_text().strip() != MARKER:
            raise RuntimeError("usuário SSH já existe e não pertence a este instalador")
        if account.pw_dir != str(HOME_PATH) or account.pw_shell != "/usr/sbin/nologin":
            raise RuntimeError("usuário SSH gerenciado possui home/shell inesperado")
    else:
        runner(["useradd", "--system", "--user-group", "--create-home", "--home-dir", str(HOME_PATH),
                "--shell", "/usr/sbin/nologin", USER])
        # A non-password marker avoids a locked-account rejection of public-key
        # forwarding on hosts without PAM. Password/interactive auth stay disabled.
        runner(["usermod", "--password", "*NP*", USER])
    HOME_PATH.mkdir(parents=True, exist_ok=True)
    os.chown(HOME_PATH, 0, 0)
    os.chmod(HOME_PATH, 0o755)
    (HOME_PATH / ".ssh").mkdir(exist_ok=True, mode=0o755)
    os.chown(HOME_PATH / ".ssh", 0, 0)
    os.chmod(HOME_PATH / ".ssh", 0o755)
    atomic_write(marker, (MARKER + "\n").encode())


def available_listeners(bundle):
    """Probe unused forwards before installing anything; existing peers stay up."""
    for name in ("remote_port", "bulk_remote_port"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", bundle[name]))
        except OSError:
            raise RuntimeError(f"porta privada {bundle[name]} já está em uso ou não pôde ser validada") from None


def prepare(args, *, runner=run):
    registry = read_registry()
    key_path = HOST_PUBLIC_KEY_PATH
    guarded_path(key_path)
    if not key_path.is_file():
        raise RuntimeError("a VPS precisa de host key Ed25519 existente; execute ssh-keygen -A no servidor e tente novamente")
    bundle = validated_bundle(args.worker_id, args.host, ssh_port=args.ssh_port,
        remote_port=args.remote_port, bulk_remote_port=args.bulk_remote_port,
        local_port=args.local_port, vps_api_port=args.vps_api_port, host_key=key_path.read_text())
    for worker_id, row in registry["workers"].items():
        if worker_id != args.worker_id and ({row["remote_port"], row["bulk_remote_port"]} & {bundle["remote_port"], bundle["bulk_remote_port"]}):
            raise ValueError("porta remota já reservada a outro worker")
    old = registry["workers"].get(args.worker_id, {})
    if old and any(old.get(key) != value for key, value in bundle.items() if key != "schema"):
        raise ValueError("configuração preparada difere da existente; não haverá alteração implícita")
    row = {**bundle, "bootstrap_origin": f"http://127.0.0.1:{bundle['local_port']}",
           "state": old.get("state", "prepared"), "prepared_at": old.get("prepared_at", int(time.time()))}
    if old.get("client_public_key"):
        row["client_public_key"] = old["client_public_key"]
    registry["workers"][args.worker_id] = row
    validate_registry_targets(registry)
    if not old:
        available_listeners(bundle)
    if not args.dry_run:
        ensure_account(runner=runner)
        commit_registry(registry, runner=runner)
    return {"ok": True, "dry_run": args.dry_run, "bundle": bundle,
            "host_fingerprint": fingerprint(bundle["ssh_host_key"]),
            "next": "No Termux: prepare --bundle <arquivo JSON contendo somente o objeto bundle>. Depois cadastre client_public_key com enroll."}


def enroll(args, *, runner=run):
    registry = read_registry()
    row = registry["workers"].get(args.worker_id)
    if not row:
        raise ValueError("execute prepare para este worker primeiro")
    text = Path(args.public_key_file).read_text() if args.public_key_file else args.public_key
    key = public_key(text)
    rotated = bool(row.get("client_public_key") and row["client_public_key"] != key)
    row.update(client_public_key=key, state="enrolled")
    if not args.dry_run:
        commit_registry(registry, runner=runner)
    return {"ok": True, "dry_run": args.dry_run, "worker_id": args.worker_id,
            "public_key_fingerprint": fingerprint(key), "key_rotated": rotated,
            "next": "Ative o transporte no Termux; depois execute activate na VPS para validar e ajustar a configuração do bot."}


def parse_env(raw):
    values = {}
    for line in raw.decode("utf-8").splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if match:
            values[match[1]] = match[2].strip().strip("\"'")
    return values


def environment_path(raw_path):
    # Do not resolve symlinks before checking: that would validate the target
    # while concealing an unsafe link in the user-supplied path.
    path = Path(raw_path).expanduser().absolute()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise RuntimeError("arquivo .env precisa ser regular e sem escrita para grupo/outros")
    guarded_path(path, uid=info.st_uid)
    if info.st_nlink != 1:
        raise RuntimeError("arquivo .env não pode possuir hard links")
    return path, info


def environment_bytes(path, info):
    with parent_descriptor(path, uid=info.st_uid) as directory:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            current = os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError("o .env mudou durante a validação; execute novamente")
            raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise RuntimeError("o arquivo .env excede o limite de leitura")
    return raw


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def updated_env(raw, values):
    text = raw.decode("utf-8")
    for key, value in values.items():
        text = re.sub(r"(?m)^[ \t]*(?:export[ \t]+)?" + re.escape(key) + r"[ \t]*=[^\r\n]*(?:\r?\n|$)", "", text)
    return (text.rstrip("\r\n") + "\n" + "".join(k + "=" + str(v) + "\n" for k, v in values.items())).encode()


def health_check(row, token, *, opener=None):
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    url = f"http://127.0.0.1:{row['remote_port']}/tts-agent/health"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with opener.open(req, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("worker não respondeu HTTP 200 pelo túnel")
        data = json.loads(response.read(256 * 1024 + 1))
    if not isinstance(data, dict) or not isinstance(data.get("tts_agent"), dict):
        raise RuntimeError("resposta de saúde do worker inválida")
    agent = data["tts_agent"]
    if (data.get("worker_id") != row["worker_id"] or data.get("runtime_kind") != "termux"
            or not data.get("ok") or not agent.get("ok")):
        raise RuntimeError("identidade ou prontidão do worker não confirmada")
    if "edge" not in agent.get("available_engines", []) or int(agent.get("stream_protocol", 0)) < 2:
        raise RuntimeError("Edge streaming v2 não foi confirmado no telefone")
    # Both private listeners must reach the same authenticated worker.
    bulk_req = urllib.request.Request(f"http://127.0.0.1:{row['bulk_remote_port']}/tts-agent/health",
                                     headers={"Authorization": "Bearer " + token})
    with opener.open(bulk_req, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("canal de transferências não respondeu HTTP 200")
        other = json.loads(response.read(256 * 1024 + 1))
    if (not isinstance(other, dict) or other.get("worker_id") != row["worker_id"]
            or other.get("runtime_kind") != "termux" or not other.get("ok")):
        raise RuntimeError("canal de transferências não confirmou o mesmo worker")
    for key in ("pid", "runtime_kind"):
        if data.get(key) is not None and other.get(key) is not None and data[key] != other[key]:
            raise RuntimeError("os canais SSH alcançaram processos diferentes")
    return {"worker_id": row["worker_id"], "edge": True, "stream_protocol": int(agent["stream_protocol"]), "both_channels": True}


def activate(args):
    registry = read_registry()
    row = registry["workers"].get(args.worker_id)
    if not row or not row.get("client_public_key"):
        raise ValueError("prepare e enroll precisam estar concluídos")
    env_path, info = environment_path(args.env_file)
    raw = environment_bytes(env_path, info)
    token = parse_env(raw).get("PHONE_WORKER_TOKEN", "")
    if not token:
        raise ValueError("PHONE_WORKER_TOKEN ausente; o token existente não será criado ou alterado")
    checked = health_check(row, token)
    changes = {"PHONE_WORKER_TRANSPORT": "ssh", "PHONE_WORKER_HOST": "127.0.0.1",
               "PHONE_WORKER_PORT": row["remote_port"], "PHONE_WORKER_BULK_PORT": row["bulk_remote_port"],
               "PHONE_WORKER_ID": row["worker_id"], "CORE_WORKER_SSH_LOCAL_PORT": row["local_port"],
               "PHONE_WORKER_SCHEME": "http", "PHONE_WORKER_ENABLED": "true"}
    new = updated_env(raw, changes)
    if not args.dry_run:
        guarded_path(BACKUP_PATH)
        BACKUP_PATH.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(BACKUP_PATH, 0o700)
        before = BACKUP_PATH / (args.worker_id + ".before-ssh.env")
        baseline = BACKUP_PATH / (args.worker_id + ".ssh-base.env")
        if not before.exists():
            atomic_write(before, raw, mode=0o600)
        atomic_write(env_path, new, mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid, expected=info)
        if not baseline.exists():
            atomic_write(baseline, new, mode=0o600)
        row["state"] = "active"
        atomic_write(CONFIG_PATH, dump_json(registry))
    return {"ok": True, "dry_run": args.dry_run, "health": checked, "changed_keys": list(changes),
            "bot_restart_required": raw != new,
            "note": "Bot, rede e música não foram reiniciados. Reinicie tts-bot em uma janela sem reprodução ativa para carregar o .env."}


def rollback(args):
    if not WORKER_RE.fullmatch(args.worker_id):
        raise ValueError("worker_id inválido")
    baseline = BACKUP_PATH / (args.worker_id + ".ssh-base.env")
    guarded_path(baseline)
    values = parse_env(baseline.read_bytes())
    if values.get("PHONE_WORKER_TRANSPORT") != "ssh" or values.get("PHONE_WORKER_HOST") != "127.0.0.1":
        raise RuntimeError("baseline não é compatível com SSH; restauração recusada")
    if not all(key in values for key in ENV_KEYS):
        raise RuntimeError("baseline SSH incompleta; restauração recusada")
    env_path, info = environment_path(args.env_file)
    raw = updated_env(environment_bytes(env_path, info), {key: values[key] for key in ENV_KEYS})
    if not args.dry_run:
        atomic_write(env_path, raw, mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid, expected=info)
    return {"ok": True, "dry_run": args.dry_run, "restored": "SSH-compatible environment baseline", "bot_restart_required": True}


def execute(args):
    if args.command == "prepare":
        return prepare(args)
    if args.command == "enroll":
        return enroll(args)
    if args.command == "status":
        row = read_registry()["workers"].get(args.worker_id)
        return {"ok": bool(row), "worker_id": args.worker_id, "state": (row or {}).get("state", "not_configured"),
                "configured_ports": {k: (row or {}).get(k) for k in ("remote_port", "bulk_remote_port", "local_port", "vps_api_port")}}
    if args.command == "activate":
        return activate(args)
    return rollback(args)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Osaka: instalação SSH privada e verificável para Phone Worker")
    parser.add_argument("command", choices=("prepare", "enroll", "status", "activate", "rollback"))
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--host")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--remote-port", type=int, default=18766)
    parser.add_argument("--bulk-remote-port", type=int, default=18767)
    parser.add_argument("--local-port", type=int, default=19000)
    parser.add_argument("--vps-api-port", type=int, default=10000)
    parser.add_argument("--public-key")
    parser.add_argument("--public-key-file")
    parser.add_argument("--env-file", default="/home/ubuntu/bot/.env")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not WORKER_RE.fullmatch(args.worker_id):
        parser.error("worker_id inválido")
    if args.command != "status" and os.geteuid() != 0 and not args.dry_run:
        parser.error("execute pelo wrapper com sudo -n; não use sudo -v")
    if args.command == "prepare" and not args.host:
        parser.error("prepare exige --host com IP público/DNS da VPS")
    if args.command == "enroll" and bool(args.public_key) == bool(args.public_key_file):
        parser.error("informe somente --public-key ou --public-key-file")
    try:
        with installation_lock() if args.command != "status" and not args.dry_run else nullcontext():
            result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
