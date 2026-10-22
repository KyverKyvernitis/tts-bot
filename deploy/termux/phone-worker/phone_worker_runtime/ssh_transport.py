"""Persistent SSH forwarding, owned by the existing Termux watchdog.

No worker imports, global proxy, remote shell or additional supervisor. The
canonical copy and private state remain usable when the agent release rolls back.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SCHEMA = "osaka-phone-ssh-v1"
MARKER = "Managed by Osaka Phone SSH v1."
WORKER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
SECRET_RE = re.compile(r"token|password|secret|authorization|cookie|credential|identity_file|known_hosts", re.I)
MAX_JSON = 512 * 1024


def read_env(path):
    values = {}
    try:
        for line in Path(path).expanduser().read_text().splitlines():
            match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if match:
                # Parse literal dotenv data, never execute shell substitutions.
                parts = shlex.split(match[2], comments=True, posix=True)
                values[match[1]] = " ".join(parts)
    except FileNotFoundError:
        pass
    return values


def environment(extra=None):
    values = dict(os.environ)
    path = Path(values.get("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env").expanduser()
    values.update(read_env(path))
    values.update(extra or {})
    return values


def private_dir(path):
    path = Path(path).expanduser().absolute()
    # Check every existing component before mkdir/chmod; a symlink must not
    # redirect a private key or the transport lock into another directory.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise PermissionError("diretório privado não pode atravessar link simbólico")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
        raise PermissionError("diretório privado pertence a outro usuário")
    os.chmod(path, 0o700)
    return path


def private_file(path, *, must_exist=True):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise PermissionError("arquivo privado não pode ser link simbólico")
    try:
        info = path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise
        return path
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise PermissionError("arquivo privado exige proprietário atual, modo 0600 e um único link")
    return path


def atomic_write(path, data, *, mode=0o600):
    path = Path(path)
    private_dir(path.parent)
    private_file(path, must_exist=False)
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".ssh-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def read_json(path, *, private=True):
    try:
        path = private_file(path) if private else Path(path)
        if path.stat().st_size > MAX_JSON:
            raise ValueError("arquivo JSON excedeu limite")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("JSON deve ser objeto")
        return value
    except FileNotFoundError:
        return {}


def public_key(text):
    parts = str(text).strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519" or "\n" in str(text).strip():
        raise ValueError("chave pública Ed25519 inválida")
    try:
        raw = base64.b64decode(parts[1], validate=True)
        pos = 0
        fields = []
        while pos < len(raw):
            length = struct.unpack("!I", raw[pos:pos + 4])[0]
            pos += 4
            fields.append(raw[pos:pos + length])
            pos += length
        if pos != len(raw) or len(fields) != 2 or fields[0] != b"ssh-ed25519" or len(fields[1]) != 32:
            raise ValueError
    except (ValueError, IndexError, struct.error):
        raise ValueError("conteúdo da chave Ed25519 inválido") from None
    return "ssh-ed25519 " + base64.b64encode(raw).decode("ascii")


def key_fingerprint(key):
    raw = hashlib.sha256(base64.b64decode(public_key(key).split()[1])).digest()
    return "SHA256:" + base64.b64encode(raw).decode().rstrip("=")


def bundle_config(bundle):
    bundle = bundle.get("bundle", bundle)
    if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA:
        raise ValueError("pacote público de configuração SSH incompatível")
    result = {key: bundle[key] for key in ("schema", "worker_id", "ssh_host", "ssh_port", "ssh_user",
              "ssh_host_key", "remote_port", "bulk_remote_port", "local_port", "vps_api_port")}
    if not WORKER_RE.fullmatch(str(result["worker_id"])):
        raise ValueError("worker_id inválido")
    host = str(result["ssh_host"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host):
        raise ValueError("host deve ser IP público ou nome DNS, sem opções SSH")
    if host.endswith((".ts.net", ".tailnet")):
        raise ValueError("use o endereço público da VPS, sem dependência do Tailscale")
    try:
        if ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10"):
            raise ValueError("use o endereço público da VPS")
    except ValueError as exc:
        if "use o endereço" in str(exc):
            raise
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", str(result["ssh_user"])):
        raise ValueError("usuário SSH inválido")
    for key in ("ssh_port", "remote_port", "bulk_remote_port", "local_port", "vps_api_port"):
        value = int(result[key])
        if not (1 if key == "ssh_port" else 1024) <= value <= 65535:
            raise ValueError("porta SSH inválida")
        result[key] = value
    if len({result[k] for k in ("ssh_port", "remote_port", "bulk_remote_port", "local_port", "vps_api_port")}) != 5:
        raise ValueError("as portas devem ser distintas")
    result["ssh_host_key"] = public_key(result["ssh_host_key"])
    return result


def paths(env):
    home = Path.home()
    root = Path(env.get("CORE_WORKER_SSH_PRIVATE_DIR") or home / ".ssh/osaka").expanduser()
    state_root = Path(env.get("PHONE_WORKER_STATE_DIR") or home / ".local/state/core-worker-phone-worker").expanduser()
    return {"private": root, "config": root / "transport.json", "state_root": state_root,
            "state": state_root / "ssh-transport.json", "lock": state_root / "ssh-transport.lock",
            "identity": Path(env.get("CORE_WORKER_SSH_IDENTITY_FILE") or root / "id_ed25519").expanduser(),
            "known_hosts": Path(env.get("CORE_WORKER_SSH_KNOWN_HOSTS_FILE") or root / "known_hosts").expanduser()}


def load_config(env):
    locations = paths(env)
    config = read_json(locations["config"])
    if not config:
        raise ValueError("transporte não preparado; execute prepare --bundle primeiro")
    return bundle_config(config), locations


def sanitize(value, env=None):
    if isinstance(value, dict):
        return {k: ("[OCULTO]" if SECRET_RE.search(str(k)) else sanitize(v, env)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, env) for v in value]
    if not isinstance(value, str):
        return value
    for key, secret in (env or {}).items():
        if re.search(r"token|secret|password|cookie", key, re.I) and len(str(secret)) >= 4:
            value = value.replace(str(secret), "[OCULTO]")
    value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [OCULTO]", value)
    return value[:1000]


def run(args, *, timeout=3, env=None):
    # No shell and no command echo: command lines can include private filenames.
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout, env=env, check=False)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_json(url, token="", *, timeout=2):
    started = time.monotonic()
    headers = {"Authorization": "Bearer " + token} if token else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("health não respondeu HTTP 200")
        raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError("resposta health excedeu limite")
        body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("health deve ser objeto JSON")
    return body, round((time.monotonic() - started) * 1000, 2)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return int(pid) > 1
    except (TypeError, ValueError, ProcessLookupError, PermissionError):
        return False


def owned_pid(pid, control):
    if not pid_alive(pid):
        return False
    try:
        args = [x.decode(errors="replace") for x in (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0") if x]
        return bool(args and Path(args[0]).name in {"ssh", "ssh.exe"} and str(control) in args)
    except OSError:
        return False


def control_path(config, locations, channel):
    digest = hashlib.sha256((config["worker_id"] + "@" + config["ssh_host"]).encode()).hexdigest()[:8]
    path = locations["private"] / ("cm-" + digest + ("-i" if channel == "interactive" else "-b"))
    if len(os.fsencode(path)) > 100:
        raise ValueError("caminho do socket SSH longo demais; configure um diretório privado mais curto")
    if path.is_symlink():
        raise PermissionError("socket de controle SSH não pode ser link simbólico")
    return path


def ssh_base(config, locations, channel):
    binary = shutil.which("ssh")
    if not binary:
        raise RuntimeError("OpenSSH ausente; instale openssh no Termux")
    return [binary, "-F", "/dev/null", "-p", str(config["ssh_port"]), "-S",
            str(control_path(config, locations, channel)), "-o", "BatchMode=yes"]


def remote_target(config):
    return config["ssh_user"] + "@" + config["ssh_host"]


def master_alive(config, locations, channel):
    if not control_path(config, locations, channel).exists():
        return False
    try:
        return run(ssh_base(config, locations, channel) + ["-O", "check", remote_target(config)], timeout=1).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def reverse_spec(config, channel, worker_port):
    port = config["remote_port" if channel == "interactive" else "bulk_remote_port"]
    return f"127.0.0.1:{port}:127.0.0.1:{worker_port}"


def master_command(config, locations, channel, worker_port):
    private_file(locations["identity"])
    private_file(locations["known_hosts"])
    command = ssh_base(config, locations, channel) + ["-M", "-N", "-T", "-i", str(locations["identity"])]
    for option in ("ExitOnForwardFailure=yes", "ServerAliveInterval=5", "ServerAliveCountMax=2",
                   "ConnectTimeout=5", "ConnectionAttempts=1", "Compression=no", "TCPKeepAlive=yes",
                   "StrictHostKeyChecking=yes", "IdentitiesOnly=yes", "PasswordAuthentication=no",
                   "KbdInteractiveAuthentication=no", "UpdateHostKeys=no", "HostKeyAlgorithms=ssh-ed25519",
                   "GlobalKnownHostsFile=/dev/null", "UserKnownHostsFile=" + str(locations["known_hosts"])):
        command.extend(["-o", option])
    command.extend(["-R", reverse_spec(config, channel, worker_port)])
    if channel == "bulk":
        command.extend(["-L", f"127.0.0.1:{config['local_port']}:127.0.0.1:{config['vps_api_port']}"])
    return command + [remote_target(config)]


def move_reverse(config, locations, channel, old_port, new_port):
    base = ssh_base(config, locations, channel)
    # Cancel only this listener. Existing channels and L19000 remain connected.
    run(base + ["-O", "cancel", "-R", reverse_spec(config, channel, old_port), remote_target(config)])
    result = run(base + ["-O", "forward", "-R", reverse_spec(config, channel, new_port), remote_target(config)])
    if result.returncode:
        raise RuntimeError("não foi possível atualizar o encaminhamento do worker")


def local_worker(config, env):
    state_root = paths(env)["state_root"]
    runtime = read_json(state_root / "runtime-status.json", private=False)
    ports = []
    if (runtime.get("worker_id") == config["worker_id"] and pid_alive(runtime.get("pid"))
            and runtime.get("control_plane_alive")):
        ports.append(runtime.get("http_port"))
    ports.extend([env.get("PHONE_WORKER_PORT", 8766), 8766, 8768])
    token = env.get("PHONE_WORKER_TOKEN", "")
    tried = set()
    for value in ports:
        try:
            port = int(value)
            if not 1024 <= port <= 65535 or port in tried:
                continue
            tried.add(port)
            body, _ = http_json(f"http://127.0.0.1:{port}/tts-agent/health", token, timeout=1)
            if body.get("worker_id") != config["worker_id"]:
                continue
            if body.get("runtime_kind", "termux") != "termux":
                continue
            if body.get("pid") is not None and not pid_alive(body["pid"]):
                continue
            return port, body
        except (OSError, ValueError, urllib.error.URLError):
            continue
    fallback = runtime.get("http_port") if runtime.get("worker_id") == config["worker_id"] else None
    try:
        fallback = int(fallback or env.get("PHONE_WORKER_PORT") or 8766)
    except (TypeError, ValueError):
        fallback = 8766
    return fallback if 1024 <= fallback <= 65535 else 8766, None


def public_snapshot(state, env):
    keys = ("mode", "state", "interactive_state", "bulk_state", "last_ok_at", "rtt_ms",
            "reconnect_count", "generation", "last_error", "worker_ready", "vps_ready", "updated_at")
    result = {key: state.get(key) for key in keys if key in state}
    result.setdefault("mode", "ssh")
    result.setdefault("state", "not_configured")
    result.setdefault("rtt_ms", None)
    age = time.time() - float(state.get("updated_at") or 0)
    if age > 30 and state.get("state") in {"connected", "worker_unavailable", "degraded"}:
        result.update(state="stale", vps_ready=False, worker_ready=False)
    return sanitize(result, env)


def connection_snapshot():
    """No subprocess or HTTP: safe to call from a health handler/heartbeat."""
    env = environment()
    if str(env.get("PHONE_WORKER_TRANSPORT", "legacy")).lower() != "ssh":
        return {"mode": "legacy"}
    try:
        return public_snapshot(read_json(paths(env)["state"]), env)
    except Exception as exc:
        return {"mode": "ssh", "state": "not_configured", "last_error": type(exc).__name__ + ": estado SSH indisponível"}


def ensure(env=None):
    env = environment(env)
    if str(env.get("PHONE_WORKER_TRANSPORT", "legacy")).lower() != "ssh":
        return {"mode": "legacy", "state": "not_required", "vps_ready": False}
    config, locations = load_config(env)
    private_dir(locations["private"])
    private_dir(locations["state_root"])
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(locations["lock"], flags, 0o600)
    with os.fdopen(fd, "r+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return public_snapshot(read_json(locations["state"]), env)
        state = read_json(locations["state"])
        now = time.time()
        state.setdefault("generation", 0)
        state.setdefault("reconnect_count", 0)
        records = state.setdefault("channels", {})
        worker_port, body = local_worker(config, env)
        ready = bool(body and body.get("ok") and (body.get("tts_agent") or {}).get("ok"))
        state.update(mode="ssh", worker_ready=ready, vps_ready=False, updated_at=now, last_error="")
        changed = False
        for channel in ("interactive", "bulk"):
            record = records.setdefault(channel, {})
            control = control_path(config, locations, channel)
            try:
                alive = master_alive(config, locations, channel)
                if alive:
                    old_port = int(record.get("worker_port") or worker_port)
                    if old_port != worker_port or record.get("reverse_ready") is False:
                        move_reverse(config, locations, channel, old_port, worker_port)
                        changed = True
                    record.update(worker_port=worker_port, reverse_ready=True, failures=0, next_retry=0, state="connected")
                elif owned_pid(record.get("pid"), control) and now - float(record.get("started_at") or 0) < 10:
                    record["state"] = "connecting"
                else:
                    # Terminate only the exact SSH process recorded for our
                    # private control path. Never pkill, kill a worker or music.
                    if owned_pid(record.get("pid"), control):
                        os.kill(int(record["pid"]), signal.SIGTERM)
                        record["state"] = "reconnecting"
                        record["next_retry"] = now + 1
                    elif record.get("pid") and record.get("failed_pid") != record["pid"]:
                        record["failed_pid"] = record["pid"]
                        record["failures"] = int(record.get("failures") or 0) + 1
                        record["next_retry"] = now + min(5, 2 ** min(3, record["failures"] - 1))
                        record["state"] = "reconnecting"
                    if now >= float(record.get("next_retry") or 0):
                        if control.exists():
                            # A control socket is removed only after the exact
                            # master check failed and its owned process is dead.
                            if pid_alive(record.get("pid")):
                                raise RuntimeError("socket SSH ainda pertence a um processo vivo")
                            control.unlink()
                        log = locations["private"] / (channel + ".log")
                        private_file(log, must_exist=False)
                        if log.exists() and log.stat().st_size > 256000:
                            atomic_write(log, log.read_bytes()[-64000:])
                        with log.open("ab") as errors:
                            os.chmod(log, 0o600)
                            process = subprocess.Popen(master_command(config, locations, channel, worker_port),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                                start_new_session=True, close_fds=True)
                        if record.get("started_at"):
                            state["reconnect_count"] += 1
                        record.update(pid=process.pid, started_at=now, worker_port=worker_port,
                                      reverse_ready=None, state="connecting", next_retry=0)
                        changed = True
                state[channel + "_state"] = record.get("state", "reconnecting")
            except Exception as exc:
                record.update(state="reconnecting", next_retry=now + 5)
                state[channel + "_state"] = "reconnecting"
                state["last_error"] = sanitize(type(exc).__name__ + ": " + str(exc), env)[:180]
        if changed:
            state["generation"] += 1
        if state.get("bulk_state") == "connected":
            try:
                vps, rtt = vps_ping(config, env)
                state.update(vps_ready=True, last_ok_at=time.time(), rtt_ms=rtt)
            except Exception as exc:
                state.update(rtt_ms=None, last_error=sanitize(type(exc).__name__ + ": " + str(exc), env)[:180])
        else:
            state["rtt_ms"] = None
        masters_ok = all(state.get(channel + "_state") == "connected" for channel in ("interactive", "bulk"))
        state["state"] = ("connected" if ready else "worker_unavailable") if masters_ok and state["vps_ready"] else "reconnecting"
        state["updated_at"] = time.time()
        atomic_write(locations["state"], json_bytes(state))
        return public_snapshot(state, env)


def vps_ping(config, env):
    url = (f"http://127.0.0.1:{config['local_port']}/core-worker/agent/ping"
           f"?worker_id={config['worker_id']}")
    body, rtt = http_json(url, env.get("CORE_WORKER_TOKEN", ""))
    if not body.get("ok") or body.get("worker_id") != config["worker_id"] or body.get("service") != "osaka-phone-ssh":
        raise ValueError("VPS não confirmou o ping autenticado do worker pareado")
    return body, rtt


def prepare(bundle_path, env=None):
    env = environment(env)
    bundle = bundle_config(read_json(bundle_path, private=False))
    locations = paths(env)
    private_dir(locations["private"])
    existing = read_json(locations["config"])
    if existing and bundle_config(existing) != bundle:
        raise ValueError("configuração SSH existente difere; nenhuma chave ou identidade será substituída")
    identity = locations["identity"]
    private_dir(identity.parent)
    if not identity.exists():
        binary = shutil.which("ssh-keygen")
        if not binary:
            raise RuntimeError("OpenSSH ausente; instale openssh no Termux")
        result = run([binary, "-q", "-t", "ed25519", "-N", "", "-f", str(identity), "-C", "osaka:" + bundle["worker_id"]], timeout=10)
        if result.returncode:
            raise RuntimeError("não foi possível gerar a chave exclusiva do telefone")
    private_file(identity)
    result = run([shutil.which("ssh-keygen") or "ssh-keygen", "-y", "-f", str(identity)])
    if result.returncode:
        raise RuntimeError("chave privada exclusiva indisponível")
    client = public_key(result.stdout)
    # The public key is safe to copy; the private key is never read or printed.
    pub_path = Path(str(identity) + ".pub")
    if pub_path.is_symlink():
        raise PermissionError("arquivo de chave pública não pode ser link")
    pub_path.write_text(client + " osaka:" + bundle["worker_id"] + "\n")
    os.chmod(pub_path, 0o644)
    host = bundle["ssh_host"] if bundle["ssh_port"] == 22 else f"[{bundle['ssh_host']}]:{bundle['ssh_port']}"
    pinned = "# " + MARKER + "\n" + host + " " + bundle["ssh_host_key"] + "\n"
    if locations["known_hosts"].exists() and locations["known_hosts"].read_text() != pinned:
        raise PermissionError("identidade fixada da VPS mudou; cadastro automático recusado")
    atomic_write(locations["known_hosts"], pinned)
    atomic_write(locations["config"], json_bytes(bundle))
    return {"ok": True, "worker_id": bundle["worker_id"], "client_public_key": client,
            "public_key_file": str(pub_path), "host_fingerprint": key_fingerprint(bundle["ssh_host_key"]),
            "next": "Cadastre esta chave pública na VPS com enroll. Depois execute activate no Termux."}


def updated_env(raw, changes):
    text = raw.decode("utf-8")
    for key in changes:
        text = re.sub(r"(?m)^[ \t]*(?:export[ \t]+)?" + re.escape(key) + r"[ \t]*=[^\r\n]*(?:\r?\n|$)", "", text)
    return (text.rstrip("\r\n") + "\n" + "".join(key + "=" + shlex.quote(str(value)) + "\n" for key, value in changes.items())).encode()


def idle_worker(config, env):
    port, light = local_worker(config, env)
    if light is None:
        runtime = read_json(paths(env)["state_root"] / "runtime-status.json", private=False)
        if pid_alive(runtime.get("pid")):
            raise RuntimeError("worker existente não respondeu; não é possível confirmar janela ociosa")
        return
    body, _ = http_json(f"http://127.0.0.1:{port}/health", env.get("PHONE_WORKER_TOKEN", ""), timeout=4)
    if body.get("worker_id") != config["worker_id"]:
        raise RuntimeError("identidade do worker não confirmada")
    agent = body.get("tts_agent") or {}
    jobs = body.get("core_worker_jobs") or {}
    music = body.get("music_agent") or {}
    guilds = music.get("guilds") or {}
    if isinstance(guilds, dict):
        sessions = list(guilds.values())
    elif isinstance(guilds, list):
        sessions = guilds
    else:
        raise RuntimeError("estado musical não reconhecido; confirme uma janela sem reprodução")
    active_music = any(isinstance(item, dict) and str(item.get("status") or item.get("state") or "").lower()
                       in {"preparing", "starting", "playing", "paused", "queued"} for item in sessions)
    if int(agent.get("active") or 0) or jobs.get("active") or active_music:
        raise RuntimeError("há TTS, música ou job ativo; aguarde uma janela ociosa e execute activate novamente")


def worker_has_environment(env, expected):
    """Confirm the running process loaded the new addresses, without a restart."""
    runtime = read_json(paths(env)["state_root"] / "runtime-status.json", private=False)
    pid = runtime.get("pid")
    if not pid_alive(pid):
        return False
    try:
        parts = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
        current = dict(part.decode(errors="replace").split("=", 1) for part in parts if b"=" in part)
        return all(current.get(key) == str(value) for key, value in expected.items())
    except OSError:
        return False


def replace_watch(worker_dir, env):
    """Handoff only a confirmed watchdog PID; SSH masters stay independent."""
    pid_path = Path(env.get("PHONE_WORKER_WATCH_PID_FILE") or worker_dir / "phone-worker-watch.pid")
    try:
        old_pid = int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        old_pid = 0
    if old_pid > 1 and pid_alive(old_pid):
        command_path = Path("/proc") / str(old_pid) / "cmdline"
        original = command_path.read_bytes()
        if b"watch-phone-worker.sh" not in original:
            raise RuntimeError("PID do watchdog pertence a outro processo; handoff recusado")
        os.kill(old_pid, signal.SIGTERM)
        for _ in range(20):
            if not pid_alive(old_pid):
                break
            time.sleep(0.1)
        if pid_alive(old_pid):
            # Old pre-migration Bash versions catch TERM without exiting. Kill
            # only that exact unchanged command, never its group or children.
            if command_path.read_bytes() != original:
                raise RuntimeError("identidade do watchdog mudou durante handoff")
            os.kill(old_pid, signal.SIGKILL)
            for _ in range(20):
                if not pid_alive(old_pid):
                    break
                time.sleep(0.1)
            if pid_alive(old_pid):
                raise RuntimeError("watchdog antigo não encerrou; canal SSH foi preservado")
    watch = worker_dir / "watch-phone-worker.sh"
    with (worker_dir / "phone-worker-watch.log").open("ab") as log:
        subprocess.Popen([shutil.which("bash") or "bash", str(watch)], stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, env={**os.environ, **environment()}, start_new_session=True, close_fds=True)


def activate(env=None, *, apply_worker=False, wait_seconds=25):
    env = environment(env)
    config, locations = load_config(env)
    if env.get("CORE_WORKER_ID") != config["worker_id"] or not env.get("CORE_WORKER_TOKEN") or not env.get("PHONE_WORKER_TOKEN"):
        raise ValueError("pareamento existente não corresponde ao pacote SSH; IDs e tokens não serão recriados")
    idle_worker(config, env)
    deadline = time.monotonic() + max(1, min(60, wait_seconds))
    current = {}
    while time.monotonic() < deadline:
        current = ensure({**env, "PHONE_WORKER_TRANSPORT": "ssh"})
        if current.get("vps_ready") and current.get("interactive_state") == "connected":
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("túnel ainda não validou acesso à VPS; configuração anterior foi preservada")
    changes = {"PHONE_WORKER_TRANSPORT": "ssh", "CORE_WORKER_VPS_URL": f"http://127.0.0.1:{config['local_port']}",
               "CORE_WORKER_ENDPOINT": f"http://127.0.0.1:{config['remote_port']}",
               "CORE_WORKER_SSH_HOST": config["ssh_host"], "CORE_WORKER_SSH_PORT": config["ssh_port"],
               "CORE_WORKER_SSH_USER": config["ssh_user"], "CORE_WORKER_SSH_INTERACTIVE_PORT": config["remote_port"],
               "CORE_WORKER_SSH_BULK_PORT": config["bulk_remote_port"], "CORE_WORKER_SSH_LOCAL_PORT": config["local_port"],
               "CORE_WORKER_SSH_VPS_HTTP_PORT": config["vps_api_port"]}
    env_path = private_file(Path(env.get("PHONE_WORKER_ENV") or Path.home() / ".phone-worker.env"))
    before = env_path.read_bytes()
    # Remove obsolete absolute overrides rather than keeping an update path
    # pointed at a tailnet after the primary API has moved to SSH.
    existing = read_env(env_path)
    if existing.get("PHONE_WORKER_AGENT_MANIFEST_URL"):
        changes["PHONE_WORKER_AGENT_MANIFEST_URL"] = f"http://127.0.0.1:{config['local_port']}/core-worker/agent/latest?worker_id={config['worker_id']}"
    backup = locations["private"] / "before-ssh.env"
    if not backup.exists():
        atomic_write(backup, before)
    after = updated_env(before, changes)
    atomic_write(env_path, after)
    atomic_write(locations["private"] / "ssh-base.env", after)
    result = {"ok": True, "worker_id": config["worker_id"], "connection": current,
              "changed_keys": list(changes), "worker_restart_required": before != after,
              "worker_applied": False}
    if apply_worker:
        # The bootstrap may need to install the SSH-capable release before an
        # old current release can report the new runtime/connection metadata.
        worker_dir = Path(env.get("PHONE_WORKER_DIR") or Path.home() / "phone-worker").expanduser()
        bootstrap = worker_dir / "phone_worker_bootstrap.py"
        if not bootstrap.is_file():
            raise RuntimeError("bootstrap canônico ausente; configuração SSH preservada, instale os arquivos completos")
        bootstrap_result = run([sys.executable, str(bootstrap), "--check"], timeout=150,
                               env={**os.environ, **environment()})
        if bootstrap_result.returncode:
            raise RuntimeError("bootstrap SSH não concluiu a atualização; execute diagnostic, o túnel e a configuração foram preservados")
        loaded = {key: changes[key] for key in ("PHONE_WORKER_TRANSPORT", "CORE_WORKER_VPS_URL", "CORE_WORKER_ENDPOINT")}
        new_env = environment()
        # A newly installed release has already been restarted by bootstrap.
        # Restart once only when the existing process still has old settings.
        if not worker_has_environment(new_env, loaded):
            start = worker_dir / "start-phone-worker.sh"
            restart = run([shutil.which("bash") or "bash", str(start), "--force-restart"], timeout=35,
                          env={**os.environ, **new_env})
            if restart.returncode or not worker_has_environment(new_env, loaded):
                raise RuntimeError("worker não confirmou a configuração nova; canal SSH foi mantido para diagnóstico")
        replace_watch(worker_dir, new_env)
        result.update(worker_applied=True, worker_restart_required=False)
    return result


def diagnostic(env=None):
    env = environment(env)
    result = {"ok": True, "captured_at": time.time(), "connection": connection_snapshot(),
              "note": "Estado local e Phone→VPS; a VPS confirma separadamente o encaminhamento reverso."}
    try:
        config, locations = load_config(env)
        result["worker_id"] = config["worker_id"]
        result["host_fingerprint"] = key_fingerprint(config["ssh_host_key"])
        result["ports"] = {key: config[key] for key in ("remote_port", "bulk_remote_port", "local_port", "vps_api_port")}
        port, body = local_worker(config, env)
        result["local_worker"] = {"port": port, "identified": bool(body), "ready": bool(body and body.get("ok"))}
        for channel in ("interactive", "bulk"):
            log = locations["private"] / (channel + ".log")
            if log.is_file() and not log.is_symlink():
                result[channel + "_last_errors"] = log.read_bytes()[-2500:].decode(errors="replace")
        body, rtt = vps_ping(config, env)
        result["vps"] = {"reachable": bool(body.get("ok")), "rtt_ms": rtt}
    except Exception as exc:
        result.update(ok=False, error=type(exc).__name__ + ": " + str(exc))
    return sanitize(result, env)


def reconnect(env=None, *, channel="all"):
    env = environment(env)
    config, locations = load_config(env)
    for name in (("interactive", "bulk") if channel == "all" else (channel,)):
        if master_alive(config, locations, name):
            run(ssh_base(config, locations, name) + ["-O", "exit", remote_target(config)])
    return {"ok": True, "state": "reconnect_requested", "note": "O watchdog reconectará os canais; worker e música não foram reiniciados."}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Osaka: conexão SSH do Phone Worker")
    parser.add_argument("command", choices=("prepare", "activate", "ensure", "status", "diagnostic", "reconnect"))
    parser.add_argument("--bundle")
    parser.add_argument("--apply-worker", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=25)
    parser.add_argument("--channel", choices=("interactive", "bulk", "all"), default="all")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    env = environment()
    try:
        if args.command == "prepare":
            if not args.bundle:
                parser.error("prepare exige --bundle com o JSON público gerado na VPS")
            result = prepare(args.bundle, env)
        elif args.command == "activate":
            result = activate(env, apply_worker=args.apply_worker, wait_seconds=args.wait_seconds)
        elif args.command == "ensure":
            result = ensure(env)
        elif args.command == "status":
            result = connection_snapshot()
        elif args.command == "diagnostic":
            result = diagnostic(env)
        else:
            result = reconnect(env, channel=args.channel)
        result = sanitize(result, env)
        # client_public_key and public_key_file are deliberately public setup
        # output, not SSH private material; the serializer does not read a key.
        raw = json_bytes(result)
        if args.output:
            output = Path(args.output).expanduser()
            if output.is_symlink():
                raise PermissionError("arquivo de saída não pode ser link")
            output.write_bytes(raw)
        print(raw.decode(), end="")
        if args.command == "ensure":
            return 0 if result.get("mode") == "legacy" or result.get("vps_ready") else 1
        return 0 if result.get("ok", True) else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": sanitize(str(exc), env)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
