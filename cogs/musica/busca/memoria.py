from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Sequence

from cogs.musica import configuracao as config

from ..nucleo.modelos import MusicTrack
from .chaves import chave_semantica_busca
from .intencao import analisar_consulta
from .normalizacao import texto_basico, tokens_texto

logger = logging.getLogger(__name__)

_ORIGEM_SELECAO = "selecao"
_ORIGEM_LINK = "link"
_PRIORIDADE_SELECAO = 10
_PRIORIDADE_LINK = 100
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PRODUCTION_ROOT = Path("/home/ubuntu/bot")
_LOCK = threading.RLock()
_loaded = False
_memoria: OrderedDict[str, "EscolhaBusca"] = OrderedDict()
_fuzzy_texto: dict[str, str] = {}
_fuzzy_tokens_ordenados: dict[str, str] = {}
_fuzzy_assinatura: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {}

_STOPWORDS_ALIAS = {
    "a", "an", "the", "o", "os", "as", "um", "uma", "of", "de", "da", "do", "das", "dos",
}


@dataclass(frozen=True, slots=True)
class EscolhaBusca:
    """Escolha global confirmada, reutilizável sem busca ou ranking."""

    chave: str
    consulta: str
    track: MusicTrack
    registrado_em: float
    origem: str = _ORIGEM_SELECAO
    prioridade: int = _PRIORIDADE_SELECAO



def _max_entries() -> int:
    try:
        return max(1, min(100_000, int(getattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_MAX_ENTRIES", 10_000) or 10_000)))
    except Exception:
        return 10_000


def _approx_enabled() -> bool:
    return bool(getattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_APPROX_ENABLED", True))


def _approx_max_edits() -> int:
    try:
        return max(1, min(3, int(getattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_APPROX_MAX_EDITS", 2) or 2)))
    except Exception:
        return 2


def _approx_min_chars() -> int:
    try:
        return max(4, min(24, int(getattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_APPROX_MIN_CHARS", 4) or 4)))
    except Exception:
        return 4


def _fuzzy_descriptor(query: str) -> tuple[str, str, tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]]:
    consulta = analisar_consulta(query)
    if consulta.artista and consulta.titulo:
        partes = (consulta.artista, consulta.titulo, *consulta.colaboradores)
        texto = texto_basico(" ".join(str(parte) for parte in partes if parte))
    else:
        texto = texto_basico(consulta.texto or consulta.raw)
    tokens = tuple(token for token in texto.split() if token)
    ordenado = " ".join(sorted(tokens))
    numeros = tuple(token for token in tokens if token.isdigit())
    assinatura = (
        texto_basico(consulta.prefixo),
        tuple(sorted(str(item) for item in consulta.atributos)),
        tuple(sorted(str(item) for item in consulta.apresentacao)),
        numeros,
    )
    return texto, ordenado, assinatura


def _indexar_escolha(escolha: "EscolhaBusca") -> None:
    texto, ordenado, assinatura = _fuzzy_descriptor(escolha.consulta)
    _fuzzy_texto[escolha.chave] = texto
    _fuzzy_tokens_ordenados[escolha.chave] = ordenado
    _fuzzy_assinatura[escolha.chave] = assinatura


def _desindexar_chave(chave: str) -> None:
    _fuzzy_texto.pop(chave, None)
    _fuzzy_tokens_ordenados.pop(chave, None)
    _fuzzy_assinatura.pop(chave, None)


def _limpar_indices() -> None:
    _fuzzy_texto.clear()
    _fuzzy_tokens_ordenados.clear()
    _fuzzy_assinatura.clear()


def _distancia_edicao_limitada(a: str, b: str, limite: int) -> int | None:
    if a == b:
        return 0
    if not a or not b:
        distancia = max(len(a), len(b))
        return distancia if distancia <= limite else None
    if abs(len(a) - len(b)) > limite:
        return None
    if len(a) > len(b):
        a, b = b, a
    anterior = list(range(len(a) + 1))
    for j, char_b in enumerate(b, 1):
        atual = [j]
        minimo_linha = j
        inicio = max(1, j - limite)
        fim = min(len(a), j + limite)
        if inicio > 1:
            atual.extend([limite + 1] * (inicio - 1))
        for i in range(inicio, fim + 1):
            custo = 0 if a[i - 1] == char_b else 1
            delecao = anterior[i] + 1
            insercao = atual[i - 1] + 1
            substituicao = anterior[i - 1] + custo
            valor = min(delecao, insercao, substituicao)
            atual.append(valor)
            if valor < minimo_linha:
                minimo_linha = valor
        if fim < len(a):
            atual.extend([limite + 1] * (len(a) - fim))
        if minimo_linha > limite:
            return None
        anterior = atual
    distancia = anterior[len(a)]
    return distancia if distancia <= limite else None


def _limite_para_texto(texto: str) -> int:
    limite = _approx_max_edits()
    tamanho = len(texto.replace(" ", ""))
    if tamanho <= 6:
        return min(limite, 1)
    return limite


def _buscar_aproximada(query: str) -> "EscolhaBusca | None":
    if not _approx_enabled():
        return None
    texto, ordenado, assinatura = _fuzzy_descriptor(query)
    compactado = texto.replace(" ", "")
    if len(compactado) < _approx_min_chars() or compactado.isdigit():
        return None
    limite = _limite_para_texto(texto)
    melhor: EscolhaBusca | None = None
    melhor_distancia = limite + 1
    for chave, escolha in reversed(_memoria.items()):
        if _fuzzy_assinatura.get(chave) != assinatura:
            continue
        candidato = _fuzzy_texto.get(chave, "")
        if not candidato:
            continue
        if abs(len(candidato) - len(texto)) > limite and abs(len(_fuzzy_tokens_ordenados.get(chave, "")) - len(ordenado)) > limite:
            continue
        distancia = _distancia_edicao_limitada(texto, candidato, limite)
        if distancia is None and ordenado:
            distancia = _distancia_edicao_limitada(ordenado, _fuzzy_tokens_ordenados.get(chave, ""), limite)
        if distancia is None:
            continue
        if melhor is None or distancia < melhor_distancia or (
            distancia == melhor_distancia and escolha.prioridade > melhor.prioridade
        ):
            melhor = escolha
            melhor_distancia = distancia
            if distancia == 0 and escolha.prioridade >= _PRIORIDADE_LINK:
                break
    if melhor is not None:
        logger.info(
            "[music/search-memory] approximate hit | query=%r learned=%r edits=%s origem=%s",
            query,
            melhor.consulta,
            melhor_distancia,
            melhor.origem,
        )
    return melhor


def _db_path() -> Path:
    configured = str(getattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_PATH", "") or "").strip()
    # O runtime real pode usar caminho configurável fora do Git. Worktrees do
    # updater/testes ficam sempre isolados para nunca apagar/alterar a memória
    # persistente da VPS durante staging/preflight.
    if _REPO_ROOT == _PRODUCTION_ROOT:
        return Path(configured or "/home/ubuntu/bot-data/musica/escolhas_busca.sqlite3")
    digest = hashlib.sha1(str(_REPO_ROOT).encode("utf-8")).hexdigest()[:12]
    return Path("/tmp") / f"osaka-musica-escolhas-{digest}.sqlite3"


def _track_payload(track: MusicTrack) -> dict[str, object]:
    return {
        "title": str(track.title or ""),
        "webpage_url": str(track.webpage_url or ""),
        "duration": track.duration,
        "uploader": str(track.uploader or ""),
        "thumbnail": str(track.thumbnail or ""),
        "source": str(track.source or ""),
        "original_url": str(track.original_url or ""),
        "extractor": str(track.extractor or ""),
        "is_live": bool(track.is_live),
        "fallback_reason": str(getattr(track, "fallback_reason", "") or ""),
        "display_title": str(getattr(track, "display_title", "") or ""),
        "display_uploader": str(getattr(track, "display_uploader", "") or ""),
        "display_thumbnail": str(getattr(track, "display_thumbnail", "") or ""),
        "display_source": str(getattr(track, "display_source", "") or ""),
    }


def _track_from_payload(payload: dict[str, object]) -> MusicTrack:
    track = MusicTrack(
        title=str(payload.get("title") or "Música"),
        webpage_url=str(payload.get("webpage_url") or ""),
        requester_id=0,
        requester_name="",
        stream_url="",
        duration=payload.get("duration"),
        uploader=str(payload.get("uploader") or ""),
        thumbnail=str(payload.get("thumbnail") or ""),
        source=str(payload.get("source") or ""),
        original_url=str(payload.get("original_url") or ""),
        extractor=str(payload.get("extractor") or ""),
        is_live=bool(payload.get("is_live")),
    )
    track.fallback_reason = str(payload.get("fallback_reason") or "")
    track.display_title = str(payload.get("display_title") or "")
    track.display_uploader = str(payload.get("display_uploader") or "")
    track.display_thumbnail = str(payload.get("display_thumbnail") or "")
    track.display_source = str(payload.get("display_source") or "")
    return track


def _copiar_track_limpo(
    track: MusicTrack,
    *,
    requester_id: int = 0,
    requester_name: str = "",
) -> MusicTrack:
    """Copia apenas metadata estável; stream temporário nunca é memorizado."""
    payload = _track_payload(track)
    clone = _track_from_payload(payload)
    clone.requester_id = int(requester_id or 0)
    clone.requester_name = str(requester_name or "")
    return clone


def _abrir_db() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=1.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS escolhas (
            chave TEXT PRIMARY KEY,
            consulta TEXT NOT NULL,
            origem TEXT NOT NULL,
            prioridade INTEGER NOT NULL,
            track_json TEXT NOT NULL,
            registrado_em REAL NOT NULL
        )
        """
    )
    return conn


def _ensure_loaded() -> None:
    global _loaded
    with _LOCK:
        if _loaded:
            return
        _memoria.clear()
        _limpar_indices()
        try:
            with _abrir_db() as conn:
                limite = _max_entries()
                rows = conn.execute(
                    "SELECT chave, consulta, origem, prioridade, track_json, registrado_em "
                    "FROM escolhas ORDER BY registrado_em DESC LIMIT ?",
                    (limite,),
                ).fetchall()
                for chave, consulta, origem, prioridade, track_json, registrado_em in reversed(rows):
                    try:
                        payload = json.loads(track_json)
                        track = _track_from_payload(payload if isinstance(payload, dict) else {})
                        escolha = EscolhaBusca(
                            chave=str(chave),
                            consulta=str(consulta),
                            track=track,
                            registrado_em=float(registrado_em),
                            origem=str(origem or _ORIGEM_SELECAO),
                            prioridade=int(prioridade or _PRIORIDADE_SELECAO),
                        )
                        _memoria[str(chave)] = escolha
                        _indexar_escolha(escolha)
                    except Exception:
                        logger.debug("[music/search-memory] entrada persistida inválida ignorada", exc_info=True)
                excesso = conn.execute("SELECT COUNT(*) FROM escolhas").fetchone()[0] - limite
                if excesso > 0:
                    conn.execute(
                        "DELETE FROM escolhas WHERE chave IN ("
                        "SELECT chave FROM escolhas ORDER BY registrado_em ASC LIMIT ?)",
                        (excesso,),
                    )
        except Exception:
            # Memória em RAM continua funcional mesmo que o disco esteja indisponível.
            logger.warning("[music/search-memory] falha ao carregar memória persistente", exc_info=True)
        _loaded = True


def _persistir_varias(escolhas: Sequence[EscolhaBusca]) -> None:
    if not escolhas:
        return
    try:
        limite = _max_entries()
        with _abrir_db() as conn:
            for escolha in escolhas:
                payload = json.dumps(_track_payload(escolha.track), ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    "INSERT INTO escolhas(chave, consulta, origem, prioridade, track_json, registrado_em) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(chave) DO UPDATE SET "
                    "consulta=excluded.consulta, origem=excluded.origem, prioridade=excluded.prioridade, "
                    "track_json=excluded.track_json, registrado_em=excluded.registrado_em",
                    (
                        escolha.chave,
                        escolha.consulta,
                        escolha.origem,
                        escolha.prioridade,
                        payload,
                        escolha.registrado_em,
                    ),
                )
            excesso = conn.execute("SELECT COUNT(*) FROM escolhas").fetchone()[0] - limite
            if excesso > 0:
                conn.execute(
                    "DELETE FROM escolhas WHERE chave IN ("
                    "SELECT chave FROM escolhas ORDER BY registrado_em ASC LIMIT ?)",
                    (excesso,),
                )
    except Exception:
        logger.warning("[music/search-memory] falha ao persistir escolha; mantendo RAM", exc_info=True)


def _persistir(escolha: EscolhaBusca) -> None:
    _persistir_varias((escolha,))


def _registrar(
    query: str,
    track: MusicTrack,
    *,
    origem: str,
    prioridade: int,
    now: float | None = None,
    persistir: bool = True,
) -> EscolhaBusca | None:
    clean_query = str(query or "").strip()
    if not clean_query:
        return None
    chave = chave_semantica_busca(clean_query)
    if not chave:
        return None
    _ensure_loaded()
    stamp = float(time.time() if now is None else now)
    with _LOCK:
        anterior = _memoria.get(chave)
        if anterior is not None and anterior.prioridade > int(prioridade):
            # Link direto é autoridade maior e não é substituído por uma escolha
            # posterior feita no seletor de resultados.
            _memoria.move_to_end(chave)
            return None
        escolha = EscolhaBusca(
            chave=chave,
            consulta=clean_query,
            track=_copiar_track_limpo(track),
            registrado_em=stamp,
            origem=str(origem),
            prioridade=int(prioridade),
        )
        _memoria[chave] = escolha
        _memoria.move_to_end(chave)
        _indexar_escolha(escolha)
        while len(_memoria) > _max_entries():
            removida_chave, _ = _memoria.popitem(last=False)
            _desindexar_chave(removida_chave)
            try:
                with _abrir_db() as conn:
                    conn.execute("DELETE FROM escolhas WHERE chave = ?", (removida_chave,))
            except Exception:
                logger.debug("[music/search-memory] falha ao remover entrada evictada", exc_info=True)
        if persistir:
            _persistir(escolha)
        return escolha


def limpar_memoria_busca() -> None:
    """Limpa RAM e a persistência do checkout atual.

    Em worktrees/testes o banco fica em /tmp e nunca toca o banco da VPS real.
    """
    global _loaded
    with _LOCK:
        _memoria.clear()
        _limpar_indices()
        _loaded = True
        try:
            path = _db_path()
            for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                candidate.unlink(missing_ok=True)
        except Exception:
            logger.debug("[music/search-memory] falha ao limpar persistência", exc_info=True)


def recarregar_memoria_busca() -> None:
    """Descarta somente RAM e recarrega o banco, simulando restart do bot."""
    global _loaded
    with _LOCK:
        _memoria.clear()
        _limpar_indices()
        _loaded = False
    _ensure_loaded()


def registrar_selecao_busca(
    query: str,
    track: MusicTrack,
    *,
    guild_id: int = 0,
    requester_id: int = 0,
    now: float | None = None,
    posicao: int = 0,
    total: int = 0,
) -> bool:
    """Guarda escolha do seletor; links diretos têm prioridade sobre ela."""
    return _registrar(
        query,
        track,
        origem=_ORIGEM_SELECAO,
        prioridade=_PRIORIDADE_SELECAO,
        now=now,
    ) is not None


def _aliases_link(track: MusicTrack) -> tuple[str, ...]:
    titulo_bruto = str(getattr(track, "display_title", "") or track.title or "").strip()
    if not titulo_bruto:
        return ()
    consulta = analisar_consulta(titulo_bruto)
    titulo_musica = str(consulta.titulo or consulta.texto or titulo_bruto).strip()
    artista = str(consulta.artista or getattr(track, "display_uploader", "") or track.uploader or "").strip()

    candidatos: list[str] = [titulo_bruto]
    if titulo_musica:
        candidatos.append(titulo_musica)
        tokens = tokens_texto(titulo_musica, remover_ruido=True)
        if tokens and tokens[0] not in _STOPWORDS_ALIAS:
            candidatos.append(tokens[0])
        if artista:
            candidatos.append(f"{artista} - {titulo_musica}")

    vistos: set[str] = set()
    aliases: list[str] = []
    for candidato in candidatos:
        texto = str(candidato or "").strip()
        if not texto:
            continue
        chave = chave_semantica_busca(texto)
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        aliases.append(texto)
    return tuple(aliases)


def registrar_link_busca(track: MusicTrack, *, now: float | None = None) -> tuple[str, ...]:
    """Aprende aliases de uma faixa iniciada por link direto.

    O título real resolvido pelo Music Agent gera aliases para o título completo,
    para o título da música e para a primeira palavra útil. Essas entradas têm
    prioridade sobre escolhas aprendidas pelo seletor de três resultados.
    """
    original = str(track.original_url or "").strip().lower()
    if not original.startswith(("http://", "https://", "www.")):
        return ()
    aliases = _aliases_link(track)
    gravados: list[str] = []
    escolhas: list[EscolhaBusca] = []
    for alias in aliases:
        escolha = _registrar(
            alias,
            track,
            origem=_ORIGEM_LINK,
            prioridade=_PRIORIDADE_LINK,
            now=now,
            persistir=False,
        )
        if escolha is not None:
            gravados.append(alias)
            escolhas.append(escolha)
    _persistir_varias(escolhas)
    return tuple(gravados)


def obter_escolha_busca(
    query: str,
    *,
    requester_id: int = 0,
    requester_name: str = "",
) -> MusicTrack | None:
    """Retorna a escolha global sem executar busca, ranking ou desempate."""
    clean_query = str(query or "").strip()
    if not clean_query:
        return None
    _ensure_loaded()
    chave = chave_semantica_busca(clean_query)
    with _LOCK:
        escolha = _memoria.get(chave)
        if escolha is None:
            escolha = _buscar_aproximada(clean_query)
            if escolha is None:
                return None
        _memoria.move_to_end(escolha.chave)
        return _copiar_track_limpo(
            escolha.track,
            requester_id=requester_id,
            requester_name=requester_name,
        )


def esquecer_escolha_busca(query: str) -> bool:
    clean_query = str(query or "").strip()
    if not clean_query:
        return False
    _ensure_loaded()
    chave = chave_semantica_busca(clean_query)
    with _LOCK:
        removida = _memoria.pop(chave, None)
        if removida is None:
            return False
        _desindexar_chave(chave)
        try:
            with _abrir_db() as conn:
                conn.execute("DELETE FROM escolhas WHERE chave = ?", (chave,))
        except Exception:
            logger.warning("[music/search-memory] falha ao remover escolha persistida", exc_info=True)
        return True
