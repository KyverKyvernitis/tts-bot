import json
import os
import time
from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, dir_size, clean_text

MIN_RAM_GB = 4
MIN_STORAGE_BYTES = 2 * 1024 * 1024 * 1024
RECOMMENDED_STORAGE_BYTES = 4 * 1024 * 1024 * 1024
STAGES = [
    "validate_device",
    "choose_strategy",
    "prepare_environment",
    "download_manifest",
    "rootfs_plan",
    "box64_plan",
    "bedrock_download_plan",
    "eula_local_confirmation",
    "final_preflight",
]


def _write_json(path, payload):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path):
    try:
        p = Path(path)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        pass
    return {}


def _free_bytes(path):
    try:
        import shutil
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        return int(shutil.disk_usage(str(p)).free)
    except Exception:
        return 0


def _ram_bytes():
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except Exception:
        pass
    return 0


def _eula_accepted(bedrock_dir):
    try:
        text = (Path(bedrock_dir) / "eula.txt").read_text(encoding="utf-8", errors="replace").lower().replace(" ", "")
        return "eula=true" in text
    except Exception:
        return False


def _stage_payload(stage, state, summary, *, blocking=False, done=False, next_action=""):
    return {
        "stage": stage,
        "state": state,
        "summary": summary,
        "blocking": bool(blocking),
        "done": bool(done),
        "nextAction": next_action,
    }


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        focus = clean_text(ctx.get("focus") or "status", 80)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or ""))
        if not str(core_linux_dir):
            core_linux_dir = Path("core-linux")
        bedrock_dir = Path(str(ctx.get("bedrockDir") or (core_linux_dir / "bedrock")))
        provision_dir = core_linux_dir / "provision"
        wizard_dir = provision_dir / "bedrock-wizard"
        manifests_dir = provision_dir / "manifests"
        downloads_dir = core_linux_dir / "downloads"
        logs_dir = core_linux_dir / "logs"
        for p in (core_linux_dir, bedrock_dir, provision_dir, wizard_dir, manifests_dir, downloads_dir, logs_dir):
            p.mkdir(parents=True, exist_ok=True)

        ram = _ram_bytes()
        free = _free_bytes(core_linux_dir)
        ram_gb = round(ram / (1024 ** 3), 2) if ram else 0
        free_gb = round(free / (1024 ** 3), 2) if free else 0
        storage_ok = free == 0 or free >= MIN_STORAGE_BYTES
        storage_recommended = free == 0 or free >= RECOMMENDED_STORAGE_BYTES
        ram_ok = ram == 0 or ram >= MIN_RAM_GB * 1024 ** 3
        termux_installed = bool(ctx.get("termuxInstalled"))
        foreground_active = bool(ctx.get("foregroundRuntimeActive"))
        eula_ok = _eula_accepted(bedrock_dir)
        server_installed = (bedrock_dir / "bedrock_server").exists()
        rootfs_ready = (core_linux_dir / "rootfs" / ".core-worker-rootfs-ready").exists()
        box64_ready = (core_linux_dir / "bin" / "box64").exists() or (core_linux_dir / "box64" / "box64").exists()

        selected_strategy = "core-linux-internal-experimental"
        previous_choice = _read_json(wizard_dir / "strategy-choice.json")
        if isinstance(previous_choice, dict) and previous_choice.get("selected"):
            selected_strategy = clean_text(previous_choice.get("selected"), 80)
        if focus in {"choose_strategy", "strategy_termux"}:
            selected_strategy = "termux-proot-managed" if termux_installed else "core-linux-internal-experimental"
        elif focus in {"strategy_core_linux", "prepare_environment", "download_manifest", "rootfs_plan", "box64_plan", "bedrock_download_plan", "final_preflight"}:
            selected_strategy = "core-linux-internal-experimental"

        blockers = []
        if not storage_ok:
            blockers.append("armazenamento insuficiente")
        if not ram_ok:
            blockers.append("RAM abaixo dos 4 GB oficiais/recomendados")
        if selected_strategy == "termux-proot-managed" and not termux_installed:
            blockers.append("Termux não instalado para fallback gerenciado")

        stages = {
            "validate_device": _stage_payload(
                "validate_device",
                "done" if storage_ok and ram_ok else "blocked",
                f"RAM {ram_gb or '?'} GB · livre {free_gb or '?'} GB",
                blocking=not (storage_ok and ram_ok),
                done=storage_ok and ram_ok,
                next_action="liberar espaço/RAM" if not storage_ok or not ram_ok else "escolher estratégia",
            ),
            "choose_strategy": _stage_payload(
                "choose_strategy",
                "done",
                "Core Linux interno experimental" if selected_strategy.startswith("core") else "Termux/proot fallback gerenciado",
                done=True,
                next_action="preparar ambiente",
            ),
            "prepare_environment": _stage_payload(
                "prepare_environment",
                "ready" if not blockers else "blocked",
                "diretórios e planos locais prontos" if not blockers else "; ".join(blockers),
                blocking=bool(blockers),
                done=not blockers,
                next_action="gerar manifesto" if not blockers else "corrigir bloqueios",
            ),
            "download_manifest": _stage_payload(
                "download_manifest",
                "ready",
                "manifesto preparado · nada baixado automaticamente",
                done=True,
                next_action="baixar rootfs/Box64/Bedrock apenas com confirmação local",
            ),
            "rootfs_plan": _stage_payload(
                "rootfs_plan",
                "done" if rootfs_ready else "pending",
                "rootfs pronto" if rootfs_ready else "rootfs pendente",
                done=rootfs_ready,
                next_action="baixar/extrair rootfs com confirmação" if not rootfs_ready else "preparar Box64",
            ),
            "box64_plan": _stage_payload(
                "box64_plan",
                "done" if box64_ready else "pending",
                "Box64 pronto" if box64_ready else "Box64 pendente",
                done=box64_ready,
                next_action="preparar Box64" if not box64_ready else "preparar Bedrock",
            ),
            "bedrock_download_plan": _stage_payload(
                "bedrock_download_plan",
                "done" if server_installed else "pending",
                "bedrock_server encontrado" if server_installed else "servidor Bedrock não instalado",
                done=server_installed,
                next_action="baixar servidor oficial com confirmação" if not server_installed else "confirmar EULA local",
            ),
            "eula_local_confirmation": _stage_payload(
                "eula_local_confirmation",
                "done" if eula_ok else "pending",
                "EULA aceita localmente" if eula_ok else "EULA pendente no app",
                done=eula_ok,
                next_action="confirmar EULA no app" if not eula_ok else "preflight final",
            ),
            "final_preflight": _stage_payload(
                "final_preflight",
                "ready" if (storage_ok and eula_ok and server_installed and foreground_active) else "blocked",
                "pronto para runner" if (storage_ok and eula_ok and server_installed and foreground_active) else "aguardando ambiente, servidor, EULA e runtime persistente",
                blocking=not (storage_ok and eula_ok and server_installed and foreground_active),
                done=bool(storage_ok and eula_ok and server_installed and foreground_active),
                next_action="ativar runtime e concluir instalação" if not foreground_active else "aguardar runner Bedrock",
            ),
        }

        strategy_choice = {
            "selected": selected_strategy,
            "fallback": "termux-proot-managed" if termux_installed else "termux-proot-optional",
            "termuxInstalled": termux_installed,
            "coreLinuxInternal": True,
            "explicitConfirmationRequired": True,
            "autoDownload": False,
            "autoInstall": False,
            "acceptEulaAutomatically": False,
            "updatedAt": int(time.time() * 1000),
        }
        manifest = {
            "kind": "bedrock-assisted-download-manifest",
            "version": 2,
            "downloads": {
                "ubuntuRootfs": {
                    "required": True,
                    "status": "pending-explicit-confirmation" if not rootfs_ready else "ready",
                    "target": safe_path(core_linux_dir / "rootfs"),
                    "sha256": "required-before-extract",
                },
                "box64": {
                    "required": True,
                    "status": "pending-explicit-confirmation" if not box64_ready else "ready",
                    "target": safe_path(core_linux_dir / "bin"),
                    "experimental": True,
                    "sha256": "required-before-run",
                },
                "bedrockDedicatedServer": {
                    "required": True,
                    "status": "pending-explicit-confirmation" if not server_installed else "ready",
                    "source": "official Minecraft Bedrock Dedicated Server Linux download page",
                    "target": safe_path(bedrock_dir),
                    "sha256": "calculate-after-download",
                },
            },
            "limits": {
                "minRamGbOfficial": MIN_RAM_GB,
                "recommendedStorageBytes": RECOMMENDED_STORAGE_BYTES,
                "requireForegroundRuntime": True,
                "requireLocalEulaConfirmation": True,
            },
        }
        state = {
            "ok": True,
            "focus": focus,
            "selectedStrategy": selected_strategy,
            "summary": "Instalador Bedrock assistido pronto · aguardando confirmação" if not blockers else "Instalador Bedrock bloqueado · " + "; ".join(blockers),
            "state": "wizard-ready" if not blockers else "wizard-blocked",
            "nextAction": stages.get(focus, stages["prepare_environment"]).get("nextAction", "seguir etapas"),
            "blockers": blockers,
            "stages": stages,
            "device": {
                "ramBytes": ram,
                "ramGb": ram_gb,
                "storageFreeBytes": free,
                "storageFreeGb": free_gb,
                "storageOk": storage_ok,
                "storageRecommended": storage_recommended,
                "officialLinux": clean_text(ctx.get("officialLinux") or "Ubuntu 22.04 LTS+", 120),
                "officialRamGb": int(ctx.get("officialRamGb") or MIN_RAM_GB),
            },
            "strategy": strategy_choice,
            "manifest": manifest,
            "bedrock": {
                "serverInstalled": server_installed,
                "eulaAccepted": eula_ok,
                "bedrockDir": safe_path(bedrock_dir),
                "port": 19132,
            },
            "safety": "assistido: não baixa, não instala, não aceita EULA e não inicia servidor sem confirmação local explícita",
            "updatedAt": int(time.time() * 1000),
        }
        _write_json(wizard_dir / "strategy-choice.json", strategy_choice)
        _write_json(wizard_dir / "installer-state.json", state)
        _write_json(manifests_dir / "bedrock-download-manifest.json", manifest)
        _write_json(wizard_dir / "stages.json", {"order": STAGES, "stages": stages})

        if focus == "validate_device":
            summary = stages["validate_device"]["summary"]
            wizard_state = stages["validate_device"]["state"]
        elif focus in {"choose_strategy", "strategy_core_linux", "strategy_termux"}:
            summary = "Estratégia escolhida: " + stages["choose_strategy"]["summary"]
            wizard_state = "strategy-ready"
        elif focus == "download_manifest":
            summary = stages["download_manifest"]["summary"]
            wizard_state = "manifest-ready"
        elif focus == "final_preflight":
            summary = stages["final_preflight"]["summary"]
            wizard_state = stages["final_preflight"]["state"]
        else:
            summary = state["summary"]
            wizard_state = state["state"]

        return ok_response(
            "bedrock_installer_wizard",
            summary,
            ok=not bool(blockers) or focus in {"download_manifest", "choose_strategy", "strategy_core_linux", "strategy_termux"},
            ready=not bool(blockers),
            state=wizard_state,
            focus=focus,
            nextAction=state["nextAction"],
            blockers=blockers,
            stages=stages,
            device=state["device"],
            strategy=strategy_choice,
            manifest=manifest,
            bedrock=state["bedrock"],
            installerState=safe_path(wizard_dir / "installer-state.json"),
            size=dir_size(core_linux_dir, max_files=620),
            safety=state["safety"],
        )
    except Exception as exc:
        return error_response("bedrock_installer_wizard", exc)
