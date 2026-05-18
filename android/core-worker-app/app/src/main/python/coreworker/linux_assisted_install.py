import json
from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, dir_size, clean_text


MIN_STORAGE_BYTES = 2 * 1024 * 1024 * 1024


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
        return int(shutil.disk_usage(str(path)).free)
    except Exception:
        return 0


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        focus = clean_text(ctx.get("focus") or "strategy", 80)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or ""))
        bedrock_dir = Path(str(ctx.get("bedrockDir") or (core_linux_dir / "bedrock")))
        provision_dir = core_linux_dir / "provision"
        downloads_dir = core_linux_dir / "downloads"
        manifests_dir = provision_dir / "manifests"
        logs_dir = core_linux_dir / "logs"
        for p in (core_linux_dir, bedrock_dir, provision_dir, downloads_dir, manifests_dir, logs_dir):
            p.mkdir(parents=True, exist_ok=True)

        termux_installed = bool(ctx.get("termuxInstalled"))
        termux_api_installed = bool(ctx.get("termuxApiInstalled"))
        foreground_active = bool(ctx.get("foregroundRuntimeActive"))
        free = _free_bytes(core_linux_dir)
        storage_ok = free == 0 or free >= MIN_STORAGE_BYTES

        strategy = {
            "ok": True,
            "kind": "linux-assisted-install-strategy",
            "version": 1,
            "selected": "core-linux-internal-experimental",
            "fallback": "termux-proot-managed" if termux_installed else "termux-proot-optional",
            "termuxInstalled": termux_installed,
            "termuxApiInstalled": termux_api_installed,
            "foregroundRuntimeActive": foreground_active,
            "requiresExplicitUserAction": True,
            "autoDownload": False,
            "autoInstall": False,
            "acceptEulaAutomatically": False,
            "arbitraryShell": False,
            "checks": {
                "storageFreeBytes": free,
                "storageRecommendedBytes": MIN_STORAGE_BYTES,
                "storageOk": storage_ok,
                "officialLinux": clean_text(ctx.get("officialLinux") or "Ubuntu 22.04 LTS+", 160),
                "officialRamGb": int(ctx.get("officialRamGb") or 4),
            },
            "steps": [
                "confirmar modo de instalação no APK",
                "baixar rootfs apenas por ação explícita",
                "verificar checksum antes de extrair",
                "preparar proot/Box64 em etapa separada",
                "baixar Bedrock oficial apenas por ação explícita",
                "exigir confirmação explícita da EULA antes de start",
                "rodar servidor somente com Foreground Service visível",
            ],
        }
        manifest = {
            "ok": True,
            "kind": "core-linux-download-manifest-plan",
            "version": 1,
            "rootfs": {
                "name": "ubuntu-22.04-arm64-rootfs",
                "status": "not-downloaded",
                "url": "pending-explicit-confirmation",
                "sha256": "pending-official-manifest",
                "destructive": False,
            },
            "box64": {
                "name": "box64-arm64",
                "status": "not-downloaded",
                "url": "pending-explicit-confirmation",
                "sha256": "pending-release-manifest",
                "experimental": True,
            },
            "bedrock": {
                "name": "bedrock-dedicated-server-linux-x86_64",
                "status": "not-downloaded",
                "url": "official-download-page-required",
                "sha256": "pending-after-download",
                "eulaAccepted": False,
            },
            "limits": {
                "maxSingleDownloadBytes": 512 * 1024 * 1024,
                "requireWifiRecommended": True,
                "requireForegroundRuntime": True,
            },
        }
        bedrock_assisted = {
            "ok": True,
            "kind": "bedrock-assisted-install-plan",
            "version": 1,
            "bedrockDir": safe_path(bedrock_dir),
            "serverInstalled": (bedrock_dir / "bedrock_server").exists(),
            "serverProperties": (bedrock_dir / "server.properties").exists(),
            "eulaAccepted": False,
            "startMode": "foreground-service-future",
            "port": 19132,
            "steps": [
                "validar requisitos do aparelho",
                "preparar Linux runtime",
                "preparar Box64 se necessário",
                "baixar Bedrock oficial manualmente/assistido",
                "mostrar EULA antes de aceitar",
                "start/stop/logs em Foreground Service",
            ],
        }

        _write_json(provision_dir / "linux-install-strategy.json", strategy)
        _write_json(manifests_dir / "download-manifest-plan.json", manifest)
        _write_json(provision_dir / "bedrock-assisted-install-plan.json", bedrock_assisted)

        if focus == "manifest":
            summary = "Manifesto de downloads preparado · nada baixado"
            details = manifest
            state = "manifest-plan-ready"
        elif focus == "bedrock_assisted":
            summary = "Plano assistido Bedrock pronto · EULA pendente"
            details = bedrock_assisted
            state = "bedrock-assisted-plan-ready"
        else:
            summary = "Estratégia Linux pronta · Core Linux interno com fallback Termux"
            details = strategy
            state = "strategy-ready"

        return ok_response(
            "linux_assisted_install",
            summary,
            ok=True,
            ready=True,
            state=state,
            focus=focus,
            coreLinuxDir=safe_path(core_linux_dir),
            provisionDir=safe_path(provision_dir),
            strategy=strategy,
            manifest=manifest,
            bedrockAssisted=bedrock_assisted,
            selected=details,
            existingPlan=_read_json(provision_dir / "bedrock-install-plan.json"),
            size=dir_size(core_linux_dir, max_files=420),
            safety="somente planos/manifestos locais: não baixa, não instala, não aceita EULA, não inicia servidor e não executa shell livre",
        )
    except Exception as exc:
        return error_response("linux_assisted_install", exc)
