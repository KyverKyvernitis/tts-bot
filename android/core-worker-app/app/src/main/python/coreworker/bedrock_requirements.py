import os
from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, clean_text


def _mem_total_mb():
    # Em Android normalmente /proc/meminfo é legível e não requer permissão especial.
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(int(parts[1]) / 1024)
    except Exception:
        return -1
    return -1


def _free_bytes(path):
    try:
        st = os.statvfs(str(path))
        return int(st.f_bavail * st.f_frsize)
    except Exception:
        return -1


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        core_linux_dir = Path(str(ctx.get("coreLinuxDir") or ""))
        mem_mb = _mem_total_mb()
        free = _free_bytes(core_linux_dir if core_linux_dir.exists() else Path("."))
        recommended_mb = int(ctx.get("officialRamGb") or 4) * 1024
        enough_ram = mem_mb >= recommended_mb if mem_mb > 0 else False
        enough_storage = free >= 2 * 1024 * 1024 * 1024 if free >= 0 else False
        base_ready = core_linux_dir.exists()
        ready = bool(base_ready and enough_ram and enough_storage)
        summary = "Bedrock: requisitos avaliados"
        if not enough_ram and mem_mb > 0:
            summary = f"Bedrock: RAM abaixo do recomendado ({mem_mb}MB)"
        elif not enough_storage and free >= 0:
            summary = "Bedrock: armazenamento livre pode ser pouco"
        elif ready:
            summary = "Bedrock: aparelho parece apto para protótipo experimental"
        return ok_response(
            "bedrock_requirements",
            summary,
            ok=base_ready,
            ready=ready,
            state="requirements-ready" if ready else "requirements-attention",
            memTotalMb=mem_mb,
            recommendedRamMb=recommended_mb,
            freeBytes=free,
            enoughRam=enough_ram,
            enoughStorage=enough_storage,
            officialLinux=clean_text(ctx.get("officialLinux") or "Ubuntu Linux suportado oficialmente", 160),
            coreLinuxDir=safe_path(core_linux_dir),
            termuxInstalled=bool(ctx.get("termuxInstalled")),
            note="Bedrock Dedicated Server oficial não é Android nativo; etapa futura precisa rootfs Linux/Box64 ou fallback Termux/proot.",
        )
    except Exception as exc:
        return error_response("bedrock_requirements", exc)
