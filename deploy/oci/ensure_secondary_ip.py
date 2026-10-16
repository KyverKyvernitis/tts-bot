#!/usr/bin/python3
# Managed by Osaka Edge secondary IP installer v1.
"""Keep one OCI secondary IPv4 address present without managing routes."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

MARKER = "Managed by Osaka Edge secondary IP installer v1."
CONFIG_MARKER = "osaka-edge-secondary-ip/v1"
INSTALLED_HELPER = Path("/usr/local/libexec/osaka-edge-secondary-ip.py")
INSTALLED_CONFIG = Path("/etc/osaka-edge-secondary-ip.json")
INSTALLED_UNIT = Path("/etc/systemd/system/osaka-edge-secondary-ip.service")
UNIT_NAME = INSTALLED_UNIT.name


@dataclass(frozen=True)
class Config:
    primary_ip: str = "10.0.0.174"
    secondary_ip: str = "10.0.0.237"
    interface: str = "ens3"
    wait_seconds: int = 30

    def validate(self):
        private_ranges = [ipaddress.ip_network(value) for value in
                          ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
        for key in ("primary_ip", "secondary_ip"):
            try:
                address = ipaddress.IPv4Address(getattr(self, key))
            except ipaddress.AddressValueError:
                raise ValueError(key + " deve ser um IPv4 privado válido") from None
            if not any(address in network for network in private_ranges):
                raise ValueError(key + " deve ser um IPv4 privado RFC 1918")
            if str(address) != getattr(self, key):
                raise ValueError(key + " deve estar no formato IPv4 canônico")
        if self.primary_ip == self.secondary_ip:
            raise ValueError("IP principal e secundário precisam ser diferentes")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", self.interface):
            raise ValueError("nome de interface inválido")
        if type(self.wait_seconds) is not int or not 0 <= self.wait_seconds <= 30:
            raise ValueError("wait_seconds deve estar entre 0 e 30")
        return self


class PrimaryNotReady(RuntimeError):
    pass


def run(args, *, timeout=5, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        # Commands contain only tool names, paths and network configuration.
        raise RuntimeError(f"{Path(args[0]).name} falhou (status {result.returncode})")
    return result


def tool(name):
    value = shutil.which(name, path="/usr/sbin:/usr/bin:/sbin:/bin")
    if not value:
        raise RuntimeError("comando ausente: " + name)
    return value


def address_state(rows, config):
    owners = lambda address: {row["ifname"] for row in rows
                              for item in row.get("addr_info", [])
                              if item.get("local") == address}
    secondary = owners(config.secondary_ip)
    if secondary and secondary != {config.interface}:
        raise RuntimeError("IP secundário presente em outra interface; nada será alterado")
    primary = owners(config.primary_ip)
    if not primary:
        raise PrimaryNotReady("IP principal ainda não está presente; nada será alterado")
    if primary != {config.interface}:
        raise RuntimeError("interface do IP principal difere da configuração ou é ambígua")
    prefixes = [item.get("prefixlen") for row in rows if row["ifname"] == config.interface
                for item in row.get("addr_info", []) if item.get("local") == config.secondary_ip]
    return {"secondary_present": bool(secondary), "existing_prefixes": prefixes}


def query_addresses(ip, runner=run):
    rows = json.loads(runner([ip, "-j", "-4", "address", "show"], timeout=2).stdout)
    if not isinstance(rows, list):
        raise RuntimeError("resposta inválida de ip address")
    return rows


def default_routes(ip, runner=run):
    # Preserve both pre-existing defaults; do not deduplicate or select a winner.
    return sorted(line.strip() for line in runner([ip, "-4", "route", "show", "default"], timeout=2).stdout.splitlines()
                  if line.strip())


def ensure_address(config, *, ip=None, runner=run, wait=True):
    config.validate()
    ip = ip or tool("ip")
    deadline = time.monotonic() + (config.wait_seconds if wait else 0)
    while True:
        try:
            state = address_state(query_addresses(ip, runner), config)
            break
        except PrimaryNotReady:
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    before = default_routes(ip, runner)
    added = False
    if not state["secondary_present"]:
        result = runner([ip, "address", "add", config.secondary_ip + "/32", "dev", config.interface,
                         "noprefixroute"], check=False, timeout=2)
        # A native manager may have added it concurrently. Re-read; never replace.
        after_state = address_state(query_addresses(ip, runner), config)
        if not after_state["secondary_present"]:
            raise RuntimeError(f"IP secundário não foi confirmado após ip address add (status {result.returncode})")
        added = result.returncode == 0
    after = default_routes(ip, runner)
    return {"ok": True, "config": asdict(config), "secondary_already_present": state["secondary_present"],
            "address_added_now": added, "existing_prefixes": state["existing_prefixes"],
            "default_routes_before": before, "default_routes_after": after,
            "default_routes_unchanged": before == after,
            "note": "Somente presença do endereço é gerenciada; nenhuma rota foi escrita."}


def native_references(address, *, roots=None):
    """Report filenames only; never output network configuration or credentials."""
    roots = roots if roots is not None else [
        Path("/etc/netplan"), Path("/etc/systemd/network"), Path("/etc/NetworkManager/system-connections"),
        Path("/etc/network/interfaces"), Path("/etc/network/interfaces.d"),
        Path("/etc/cloud/cloud.cfg.d"), Path("/var/lib/cloud/instance/network-config"),
        Path("/etc/rc.local"), Path("/etc/systemd/system"), Path("/etc/cron.d"),
    ]
    pattern = re.compile(r"(?<![0-9.])" + re.escape(address) + r"(?![0-9.])")
    result, unreadable = [], []
    own_paths = {INSTALLED_HELPER, INSTALLED_CONFIG, INSTALLED_UNIT}
    for root in roots:
        try:
            root.lstat()
            if root.is_dir():
                candidates = (Path(folder) / name for folder, _directories, names in
                              os.walk(root, onerror=lambda exc: unreadable.append(str(exc.filename)))
                              for name in names)
            else:
                candidates = [root]
            for path in candidates:
                if path in own_paths or path.is_symlink() or not path.is_file():
                    continue
                try:
                    if path.stat().st_size > 1024 * 1024:
                        continue
                    with path.open(encoding="utf-8", errors="replace") as stream:
                        for line in stream:
                            if line.lstrip().startswith(("#", ";")):
                                continue
                            if pattern.search(line):
                                result.append(str(path))
                                break
                except OSError:
                    unreadable.append(str(path))
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append(str(root))
    return {"possible_native_references": sorted(set(result)), "unreadable_paths": sorted(set(unreadable))}


def inspect(config, *, ip=None, runner=run):
    config.validate()
    ip = ip or tool("ip")
    state = address_state(query_addresses(ip, runner), config)
    result = {"ok": True, "config": asdict(config), **state, "default_routes": default_routes(ip, runner),
              **native_references(config.secondary_ip)}
    result["note"] = "Referências em arquivos são indícios; não provam que o gerenciador está ativo. Conteúdo não é exibido."
    return result


def validate_parent_chain(path):
    for parent in reversed(path.parents):
        if not parent.exists() and not parent.is_symlink():
            continue
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("diretório de instalação inseguro: " + str(parent))


def validate_owned_target(path, *, json_config=False):
    validate_parent_chain(path)
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("destino existente não é arquivo root seguro: " + str(path))
    if json_config:
        try:
            owned = json.loads(path.read_text()).get("managed_by") == CONFIG_MARKER
        except (ValueError, AttributeError):
            owned = False
    else:
        with path.open(encoding="utf-8") as stream:
            owned = MARKER in stream.read(512)
    if not owned:
        raise RuntimeError("destino existente não pertence a este instalador: " + str(path))


def atomic_install(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    validate_parent_chain(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, 0, 0)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_unit_registration(systemctl):
    result = run([systemctl, "show", UNIT_NAME, "--property=FragmentPath", "--property=DropInPaths",
                  "--property=LoadState"], check=False)
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and properties.get("LoadState") != "not-found":
        raise RuntimeError("não foi possível conferir a definição existente desta unit")
    fragment = properties.get("FragmentPath", "")
    if fragment and fragment != str(INSTALLED_UNIT):
        raise RuntimeError("já existe outra definição desta unit: " + fragment)
    if properties.get("DropInPaths"):
        raise RuntimeError("a unit possui drop-ins externos; instalação recusada")
    for base in (Path("/etc/systemd/system"), Path("/run/systemd/system")):
        folder = base / (UNIT_NAME + ".d")
        if folder.is_dir() and any(folder.iterdir()):
            raise RuntimeError("a unit possui diretório de drop-ins externo: " + str(folder))


def install(config, *, allow_native_references=False, dry_run=False):
    if os.geteuid() != 0:
        raise RuntimeError("instalação exige root; use install-secondary-ip.sh (sudo -n)")
    config.validate()
    report = inspect(config)
    if report["unreadable_paths"]:
        raise RuntimeError("não foi possível conferir todas as referências de configuração nativa")
    if report["possible_native_references"] and not allow_native_references:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise RuntimeError("há referências externas ao IP; revise a persistência existente antes de criar outro gerenciador")
    for target in (INSTALLED_HELPER, INSTALLED_UNIT):
        validate_owned_target(target)
    validate_owned_target(INSTALLED_CONFIG, json_config=True)
    source = Path(__file__).read_text(encoding="utf-8")
    compile(source, str(INSTALLED_HELPER), "exec")
    unit_source = Path(__file__).with_name(UNIT_NAME)
    unit = unit_source.read_text(encoding="utf-8")
    if MARKER not in unit[:512]:
        raise RuntimeError("unit de origem sem marcador deste instalador")
    systemctl = tool("systemctl")
    run([systemctl, "show", "--property=Version", "--value"])
    validate_unit_registration(systemctl)
    run([tool("systemd-analyze"), "verify", str(unit_source)], timeout=15)
    report.update(dry_run=dry_run, unit=UNIT_NAME, persistence_configured=False)
    if dry_run:
        return report
    atomic_install(INSTALLED_HELPER, source, 0o755)
    atomic_install(INSTALLED_CONFIG, json.dumps({"managed_by": CONFIG_MARKER, **asdict(config)}, indent=2) + "\n", 0o644)
    atomic_install(INSTALLED_UNIT, unit, 0o644)
    run([systemctl, "daemon-reload"], timeout=15)
    run([systemctl, "enable", "--now", UNIT_NAME], timeout=50)
    # start is a no-op for an already-active oneshot: still verify the address.
    state = ensure_address(config)
    report.update(state, persistence_configured=True,
                  reboot_restoration_tested=False,
                  note="Somente esta unit foi habilitada; bot e rede não foram reiniciados. Restauração após reboot não foi testada.")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "ensure", "install"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--primary-ip")
    parser.add_argument("--secondary-ip")
    parser.add_argument("--interface")
    parser.add_argument("--wait-seconds", type=int)
    parser.add_argument("--allow-native-references", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action != "install" and (args.dry_run or args.allow_native_references):
            raise ValueError("--dry-run e --allow-native-references são opções de install; use inspect para somente leitura")
        values = asdict(Config())
        if args.config:
            data = json.loads(args.config.read_text())
            if data.pop("managed_by", None) != CONFIG_MARKER:
                raise ValueError("configuração sem marcador deste instalador")
            values.update(data)
        for key in values:
            if getattr(args, key, None) is not None:
                values[key] = getattr(args, key)
        config = Config(**values).validate()
        if args.action == "inspect":
            report = inspect(config)
        elif args.action == "ensure":
            report = ensure_address(config)
        else:
            report = install(config, allow_native_references=args.allow_native_references, dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
