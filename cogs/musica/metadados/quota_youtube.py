from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class SnapshotQuotaYouTube:
    dia_quota: str
    chamadas: int
    bloqueadas: int
    limite: int
    restantes: int


_DIA_QUOTA = ""
_CHAMADAS = 0
_BLOQUEADAS = 0
_CARREGADO = False


def _dia_atual() -> str:
    try:
        zona = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        zona = timezone.utc
    return datetime.now(zona).date().isoformat()


def _arquivo_estado() -> Path:
    # O hash do checkout separa runtime real e worktrees do updater. Assim testes
    # em staging não consomem a margem diária da instância viva.
    repo = Path(__file__).resolve().parents[3]
    sufixo = hashlib.sha1(str(repo).encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return Path("/var/tmp") / f"tts-bot-youtube-search-quota-{sufixo}.json"


def _salvar_estado() -> None:
    payload = {
        "day": _DIA_QUOTA,
        "calls": int(_CHAMADAS),
        "blocked": int(_BLOQUEADAS),
    }
    destino = _arquivo_estado()
    temporario = destino.with_suffix(destino.suffix + ".tmp")
    try:
        temporario.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporario.replace(destino)
    except OSError:
        try:
            temporario.unlink(missing_ok=True)
        except OSError:
            pass


def _carregar_estado() -> None:
    global _DIA_QUOTA, _CHAMADAS, _BLOQUEADAS, _CARREGADO
    if _CARREGADO:
        return
    _CARREGADO = True
    try:
        payload = json.loads(_arquivo_estado().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    _DIA_QUOTA = str(payload.get("day") or "")
    try:
        _CHAMADAS = max(0, int(payload.get("calls") or 0))
        _BLOQUEADAS = max(0, int(payload.get("blocked") or 0))
    except (TypeError, ValueError):
        _CHAMADAS = 0
        _BLOQUEADAS = 0


def _renovar_dia() -> None:
    global _DIA_QUOTA, _CHAMADAS, _BLOQUEADAS
    _carregar_estado()
    hoje = _dia_atual()
    if _DIA_QUOTA == hoje:
        return
    _DIA_QUOTA = hoje
    _CHAMADAS = 0
    _BLOQUEADAS = 0
    _salvar_estado()


def limpar_quota_youtube(*, apagar_persistido: bool = True) -> None:
    global _DIA_QUOTA, _CHAMADAS, _BLOQUEADAS, _CARREGADO
    _DIA_QUOTA = ""
    _CHAMADAS = 0
    _BLOQUEADAS = 0
    _CARREGADO = False
    if apagar_persistido:
        try:
            _arquivo_estado().unlink(missing_ok=True)
        except OSError:
            pass


def busca_youtube_disponivel(*, limite_diario: int, habilitado: bool = True) -> bool:
    if not habilitado:
        return True
    _renovar_dia()
    limite = max(0, int(limite_diario or 0))
    return limite > 0 and _CHAMADAS < limite


def consumir_busca_youtube(*, limite_diario: int, habilitado: bool = True) -> bool:
    """Reserva uma chamada search.list no guard local de quota.

    O contador persiste fora do repositório e usa o dia da quota do YouTube.
    Ele é um soft guard: a quota oficial do projeto continua sendo autoritativa.
    """
    global _CHAMADAS, _BLOQUEADAS
    if not habilitado:
        return True
    _renovar_dia()
    limite = max(0, int(limite_diario or 0))
    if limite <= 0 or _CHAMADAS >= limite:
        _BLOQUEADAS += 1
        _salvar_estado()
        return False
    _CHAMADAS += 1
    _salvar_estado()
    return True


def snapshot_quota_youtube(*, limite_diario: int) -> SnapshotQuotaYouTube:
    _renovar_dia()
    limite = max(0, int(limite_diario or 0))
    return SnapshotQuotaYouTube(
        dia_quota=_DIA_QUOTA,
        chamadas=_CHAMADAS,
        bloqueadas=_BLOQUEADAS,
        limite=limite,
        restantes=max(0, limite - _CHAMADAS),
    )
