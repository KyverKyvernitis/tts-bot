from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
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
_load_retry_after = 0.0
_fts_disponivel: bool | None = None
_schema_ready: dict[str, tuple[int, int]] = {}
_memoria: OrderedDict[str, "EscolhaBusca"] = OrderedDict()
_fuzzy_texto: dict[str, str] = {}
_fuzzy_tokens_ordenados: dict[str, str] = {}
_fuzzy_assinatura: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {}
_lexical_tokens: dict[str, frozenset[str]] = {}
_lexical_index: dict[str, set[str]] = {}

_STOPWORDS_ALIAS = {
    "a", "an", "the", "o", "os", "as", "um", "uma", "of", "de", "da", "do", "das", "dos",
}
_SUFFIX_TITULO_RE = re.compile(r"\s*(?:\([^()]{1,96}\)|\[[^\[\]]{1,96}\])\s*$")


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


def _candidate_limit() -> int:
    try:
        return max(16, min(2048, int(getattr(config, "MUSIC_SEARCH_CHOICE_MEMORY_CANDIDATE_LIMIT", 256))))
    except (TypeError, ValueError):
        return 256


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


def _tokens_lexicais(texto: str) -> frozenset[str]:
    return frozenset(
        token
        for token in texto.split()
        if token and (token.isdigit() or (len(token) >= 2 and token not in _STOPWORDS_ALIAS))
    )


def _indexar_escolha(escolha: "EscolhaBusca") -> None:
    texto, ordenado, assinatura = _fuzzy_descriptor(escolha.consulta)
    _fuzzy_texto[escolha.chave] = texto
    _fuzzy_tokens_ordenados[escolha.chave] = ordenado
    _fuzzy_assinatura[escolha.chave] = assinatura
    tokens = _tokens_lexicais(texto)
    _lexical_tokens[escolha.chave] = tokens
    for token in tokens:
        _lexical_index.setdefault(token, set()).add(escolha.chave)


def _desindexar_chave(chave: str) -> None:
    _fuzzy_texto.pop(chave, None)
    _fuzzy_tokens_ordenados.pop(chave, None)
    _fuzzy_assinatura.pop(chave, None)
    tokens = _lexical_tokens.pop(chave, frozenset())
    for token in tokens:
        chaves = _lexical_index.get(token)
        if chaves is None:
            continue
        chaves.discard(chave)
        if not chaves:
            _lexical_index.pop(token, None)


def _limpar_indices() -> None:
    _fuzzy_texto.clear()
    _fuzzy_tokens_ordenados.clear()
    _fuzzy_assinatura.clear()
    _lexical_tokens.clear()
    _lexical_index.clear()


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




def _buscar_lexical_agressiva(query: str) -> "EscolhaBusca | None":
    """Reaproveita memória por sobreposição de tokens, sem consultar rede.

    A qualidade é deliberadamente mais permissiva que a chave semântica exata:
    aceita subconjunto/superconjunto textual. Números e intenção (live/remix/
    lyrics/prefixos) ainda precisam coincidir exatamente para evitar os erros
    mais caros.
    """
    texto, _ordenado, assinatura = _fuzzy_descriptor(query)
    tokens_query = _tokens_lexicais(texto)
    if not tokens_query:
        return None

    # Um único token puramente numérico é uma identidade forte demais para
    # aproximação; esse caso continua reservado ao direct-hit exato/aliases.
    if len(tokens_query) == 1 and next(iter(tokens_query)).isdigit():
        return None

    candidatos = _candidatos_persistidos(query, aproximada=False)
    melhor: EscolhaBusca | None = None
    melhor_cobertura = -1
    melhor_excesso = 10**9
    for escolha in candidatos:
        candidato, _ordenado, assinatura_candidato = _fuzzy_descriptor(escolha.consulta)
        if assinatura_candidato != assinatura:
            continue
        tokens_candidato = _tokens_lexicais(candidato)
        if not tokens_candidato:
            continue
        # Proteção adicional para anos, números de faixa e títulos numéricos.
        numeros_query = {item for item in tokens_query if item.isdigit()}
        numeros_candidato = {item for item in tokens_candidato if item.isdigit()}
        if numeros_query != numeros_candidato:
            continue
        intersecao = tokens_query & tokens_candidato
        if not intersecao:
            continue
        # Agressivo de propósito: um dos lados precisa estar contido no outro,
        # ou todos menos um token da consulta precisam coincidir.
        contido = tokens_query <= tokens_candidato or tokens_candidato <= tokens_query
        quase_contido = len(tokens_query) >= 3 and len(intersecao) >= len(tokens_query) - 1
        if not contido and not quase_contido:
            continue
        cobertura = len(intersecao)
        excesso = len(tokens_query ^ tokens_candidato)
        if (
            melhor is None
            or escolha.prioridade > melhor.prioridade
            or (escolha.prioridade == melhor.prioridade and cobertura > melhor_cobertura)
            or (
                escolha.prioridade == melhor.prioridade
                and cobertura == melhor_cobertura
                and excesso < melhor_excesso
            )
            or (
                escolha.prioridade == melhor.prioridade
                and cobertura == melhor_cobertura
                and excesso == melhor_excesso
                and escolha.registrado_em > melhor.registrado_em
            )
        ):
            melhor = escolha
            melhor_cobertura = cobertura
            melhor_excesso = excesso

    if melhor is not None:
        logger.info(
            "[music/search-memory] lexical hit | query=%r learned=%r overlap=%s origem=%s",
            query,
            melhor.consulta,
            melhor_cobertura,
            melhor.origem,
        )
    return melhor


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
    for escolha in _candidatos_persistidos(query, aproximada=True):
        candidato, candidato_ordenado, assinatura_candidato = _fuzzy_descriptor(escolha.consulta)
        if assinatura_candidato != assinatura or not candidato:
            continue
        if abs(len(candidato) - len(texto)) > limite and abs(len(candidato_ordenado) - len(ordenado)) > limite:
            continue
        distancia = _distancia_edicao_limitada(texto, candidato, limite)
        if distancia is None and ordenado:
            distancia = _distancia_edicao_limitada(ordenado, candidato_ordenado, limite)
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


class _MemoryConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def _abrir_db() -> sqlite3.Connection:
    with _LOCK:
        return _abrir_db_locked()


def _abrir_db_locked() -> sqlite3.Connection:
    global _fts_disponivel
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=2.0, factory=_MemoryConnection)
    conn.execute("PRAGMA synchronous=NORMAL")
    stat = path.stat()
    identity = (stat.st_dev, stat.st_ino)
    if _schema_ready.get(str(path)) == identity:
        return conn
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS escolhas (
            chave TEXT PRIMARY KEY,
            consulta TEXT NOT NULL,
            origem TEXT NOT NULL,
            prioridade INTEGER NOT NULL,
            track_json TEXT NOT NULL,
            registrado_em REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS escolhas_registradas ON escolhas(registrado_em, chave);
        CREATE TABLE IF NOT EXISTS escolhas_indice (
            chave TEXT PRIMARY KEY,
            texto TEXT NOT NULL,
            ordenado TEXT NOT NULL,
            assinatura TEXT NOT NULL,
            tamanho INTEGER NOT NULL,
            media_key TEXT NOT NULL,
            original_key TEXT NOT NULL,
            webpage_key TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS escolhas_indice_tamanho ON escolhas_indice(assinatura, tamanho);
        CREATE INDEX IF NOT EXISTS escolhas_indice_midia ON escolhas_indice(media_key);
        CREATE INDEX IF NOT EXISTS escolhas_indice_original ON escolhas_indice(original_key);
        CREATE INDEX IF NOT EXISTS escolhas_indice_webpage ON escolhas_indice(webpage_key);
        CREATE TABLE IF NOT EXISTS escolhas_tokens (
            token TEXT NOT NULL,
            chave TEXT NOT NULL,
            PRIMARY KEY(token, chave)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS escolhas_tokens_chave ON escolhas_tokens(chave);
        CREATE TABLE IF NOT EXISTS escolhas_trigramas (
            trigrama TEXT NOT NULL,
            chave TEXT NOT NULL,
            PRIMARY KEY(trigrama, chave)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS escolhas_trigramas_chave ON escolhas_trigramas(chave);
        CREATE TABLE IF NOT EXISTS escolhas_meta (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS escolhas_arquivo_outbox (
            chave TEXT PRIMARY KEY,
            track_json TEXT NOT NULL,
            registrado_em REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS escolhas_outbox_ordem ON escolhas_arquivo_outbox(registrado_em, chave);
        CREATE TABLE IF NOT EXISTS escolhas_link_pendentes (
            batch_id TEXT PRIMARY KEY,
            tracks_json TEXT NOT NULL,
            registrado_em REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS escolhas_links_ordem ON escolhas_link_pendentes(registrado_em, batch_id);
        CREATE TRIGGER IF NOT EXISTS escolhas_removida AFTER DELETE ON escolhas BEGIN
            DELETE FROM escolhas_indice WHERE chave=OLD.chave;
            DELETE FROM escolhas_tokens WHERE chave=OLD.chave;
            DELETE FROM escolhas_trigramas WHERE chave=OLD.chave;
        END;
        """
    )
    if _fts_disponivel is not False:
        try:
            had_fts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='escolhas_fts'"
            ).fetchone() is not None
            conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS escolhas_fts USING fts5(texto, tokenize='unicode61');
                CREATE TRIGGER IF NOT EXISTS escolhas_fts_ins AFTER INSERT ON escolhas_indice BEGIN
                    INSERT INTO escolhas_fts(rowid, texto) VALUES (NEW.rowid, NEW.texto);
                END;
                CREATE TRIGGER IF NOT EXISTS escolhas_fts_upd AFTER UPDATE ON escolhas_indice BEGIN
                    DELETE FROM escolhas_fts WHERE rowid=OLD.rowid;
                    INSERT INTO escolhas_fts(rowid, texto) VALUES (NEW.rowid, NEW.texto);
                END;
                CREATE TRIGGER IF NOT EXISTS escolhas_fts_del AFTER DELETE ON escolhas_indice BEGIN
                    DELETE FROM escolhas_fts WHERE rowid=OLD.rowid;
                END;
                """
            )
            if not had_fts:
                # Uma atualização de SQLite pode disponibilizar FTS5 após o
                # catálogo já ter sido indexado pela alternativa de trigramas.
                conn.execute("INSERT INTO escolhas_fts(rowid, texto) SELECT rowid, texto FROM escolhas_indice")
                conn.commit()
            _fts_disponivel = True
        except sqlite3.OperationalError:
            # Termux/VPS com SQLite sem FTS5 continuam usando índices de tokens
            # e trigramas, com o mesmo orçamento de candidatos.
            _fts_disponivel = False
            # Ao abrir um banco vindo de SQLite com FTS5, os gatilhos antigos
            # também precisam sair do caminho das escritas sem esse módulo.
            conn.executescript("DROP TRIGGER IF EXISTS escolhas_fts_ins; "
                               "DROP TRIGGER IF EXISTS escolhas_fts_upd; "
                               "DROP TRIGGER IF EXISTS escolhas_fts_del;")
    _schema_ready[str(path)] = identity
    return conn


def _assinatura_json(assinatura: tuple) -> str:
    return json.dumps(assinatura, ensure_ascii=False, separators=(",", ":"))


def _trigramas(texto: str, ordenado: str) -> set[str]:
    # Tokens independentes permitem erro no meio da palavra sem varrer catálogo.
    return {token[idx:idx + 3] for token in set((texto + " " + ordenado).split())
            for idx in range(max(0, len(token) - 2))}


def _indexar_db(conn: sqlite3.Connection, escolha: EscolhaBusca) -> None:
    from .arquivo import _url_key, media_key

    texto, ordenado, assinatura = _fuzzy_descriptor(escolha.consulta)
    conn.execute(
        "INSERT INTO escolhas_indice(chave, texto, ordenado, assinatura, tamanho, media_key, original_key, webpage_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chave) DO UPDATE SET "
        "texto=excluded.texto, ordenado=excluded.ordenado, assinatura=excluded.assinatura, "
        "tamanho=excluded.tamanho, media_key=excluded.media_key, "
        "original_key=excluded.original_key, webpage_key=excluded.webpage_key",
        (escolha.chave, texto, ordenado, _assinatura_json(assinatura), len(texto), media_key(escolha.track),
         _url_key(escolha.track.original_url), _url_key(escolha.track.webpage_url)),
    )
    conn.execute("DELETE FROM escolhas_tokens WHERE chave=?", (escolha.chave,))
    conn.executemany("INSERT INTO escolhas_tokens(token, chave) VALUES (?, ?)",
                     ((token, escolha.chave) for token in _tokens_lexicais(texto)))
    conn.execute("DELETE FROM escolhas_trigramas WHERE chave=?", (escolha.chave,))
    conn.executemany("INSERT INTO escolhas_trigramas(trigrama, chave) VALUES (?, ?)",
                     ((gram, escolha.chave) for gram in _trigramas(texto, ordenado)))


def _enfileirar_arquivo(conn: sqlite3.Connection, track: MusicTrack, *, stamp: float) -> None:
    from .arquivo import media_key

    key = media_key(track)
    if key:
        conn.execute(
            "INSERT INTO escolhas_arquivo_outbox(chave, track_json, registrado_em) VALUES (?, ?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET track_json=excluded.track_json, "
            "registrado_em=MAX(escolhas_arquivo_outbox.registrado_em+0.000001, excluded.registrado_em)",
            (key, json.dumps(_track_payload(track), ensure_ascii=False, separators=(",", ":")), stamp),
        )


def _escolha_row(row: tuple) -> EscolhaBusca | None:
    try:
        chave, consulta, origem, prioridade, track_json, registrado_em = row
        payload = json.loads(track_json)
        if not isinstance(payload, dict):
            return None
        return EscolhaBusca(str(chave), str(consulta), _track_from_payload(payload), float(registrado_em),
                            str(origem or _ORIGEM_SELECAO), int(prioridade or _PRIORIDADE_SELECAO))
    except (TypeError, ValueError):
        logger.debug("[music/search-memory] entrada persistida inválida ignorada", exc_info=True)
        return None


def _cache_escolha(escolha: EscolhaBusca) -> None:
    if _memoria.get(escolha.chave) is not escolha:
        _desindexar_chave(escolha.chave)
        _memoria[escolha.chave] = escolha
        _indexar_escolha(escolha)
    _memoria.move_to_end(escolha.chave)
    while len(_memoria) > _max_entries():
        chave, _ = _memoria.popitem(last=False)
        _desindexar_chave(chave)


def _ensure_loaded() -> None:
    global _loaded, _load_retry_after
    with _LOCK:
        if _loaded or time.monotonic() < _load_retry_after:
            return
        try:
            with _abrir_db() as conn:
                # Migração em páginas: indexa metadata antiga e recupera eventos
                # de arquivamento sem carregar o acervo inteiro em RAM.
                migrated = conn.execute(
                    "SELECT 1 FROM escolhas_meta WHERE chave='indice_outbox_v1'"
                ).fetchone()
                if migrated is None:
                    cursor = conn.execute(
                        "SELECT e.chave, e.consulta, e.origem, e.prioridade, e.track_json, e.registrado_em "
                        "FROM escolhas e LEFT JOIN escolhas_indice i ON i.chave=e.chave WHERE i.chave IS NULL"
                    )
                    while rows := cursor.fetchmany(256):
                        for row in rows:
                            escolha = _escolha_row(row)
                            if escolha is not None:
                                _indexar_db(conn, escolha)
                                _enfileirar_arquivo(conn, escolha.track, stamp=time.time())
                    conn.execute("INSERT INTO escolhas_meta(chave, valor) VALUES ('indice_outbox_v1', 'done')")
                rows = conn.execute(
                    "SELECT chave, consulta, origem, prioridade, track_json, registrado_em "
                    "FROM escolhas ORDER BY registrado_em DESC LIMIT ?", (_max_entries(),),
                ).fetchall()
                for row in reversed(rows):
                    escolha = _escolha_row(row)
                    if escolha is not None:
                        _cache_escolha(escolha)
            _loaded = True
            _load_retry_after = 0.0
        except Exception:
            # Falha temporária não marca migração pronta nem apaga a RAM.
            _load_retry_after = time.monotonic() + 5.0
            logger.warning("[music/search-memory] falha ao carregar memória persistente", exc_info=True)


def _buscar_exata_persistida(chave: str) -> EscolhaBusca | None:
    try:
        with _abrir_db() as conn:
            row = conn.execute(
                "SELECT chave, consulta, origem, prioridade, track_json, registrado_em FROM escolhas WHERE chave=?",
                (chave,),
            ).fetchone()
        return _escolha_row(row) if row is not None else None
    except Exception:
        logger.debug("[music/search-memory] falha no lookup persistente", exc_info=True)
        return None


def _candidatos_persistidos(query: str, *, aproximada: bool) -> list[EscolhaBusca]:
    texto, ordenado, assinatura = _fuzzy_descriptor(query)
    tokens = sorted(_tokens_lexicais(texto))
    limite = _candidate_limit()
    candidatos: dict[str, EscolhaBusca] = {}
    # RAM é apenas um cache limitado e também mantém o modo degradado sem disco.
    for idx, escolha in enumerate(reversed(_memoria.values())):
        if idx >= limite:
            break
        sig = _fuzzy_assinatura.get(escolha.chave)
        if sig == assinatura and (aproximada or bool(set(tokens) & _lexical_tokens.get(escolha.chave, frozenset()))):
            candidatos[escolha.chave] = escolha
    try:
        with _abrir_db() as conn:
            ids: list[str] = []
            if tokens:
                placeholders = ",".join("?" for _ in tokens)
                rows = conn.execute(
                    "SELECT t.chave FROM escolhas_tokens t JOIN escolhas_indice i ON i.chave=t.chave "
                    "JOIN escolhas e ON e.chave=t.chave "
                    f"WHERE t.token IN ({placeholders}) AND i.assinatura=? "
                    "GROUP BY t.chave ORDER BY COUNT(*) DESC, e.prioridade DESC, e.registrado_em DESC LIMIT ?",
                    (*tokens, _assinatura_json(assinatura), limite),
                ).fetchall()
                ids.extend(str(row[0]) for row in rows)
            if aproximada:
                if _fts_disponivel and tokens:
                    match = " OR ".join('"' + token.replace('"', '""') + '"*' for token in tokens)
                    rows = conn.execute(
                        "SELECT i.chave FROM escolhas_fts f JOIN escolhas_indice i ON i.rowid=f.rowid "
                        "WHERE escolhas_fts MATCH ? AND i.assinatura=? ORDER BY f.rank LIMIT ?",
                        (match, _assinatura_json(assinatura), limite),
                    ).fetchall()
                    ids.extend(str(row[0]) for row in rows)
                grams = sorted(_trigramas(texto, ordenado))
                if grams:
                    placeholders = ",".join("?" for _ in grams)
                    rows = conn.execute(
                        "SELECT g.chave FROM escolhas_trigramas g JOIN escolhas_indice i ON i.chave=g.chave "
                        "JOIN escolhas e ON e.chave=g.chave "
                        f"WHERE g.trigrama IN ({placeholders}) AND i.assinatura=? AND i.tamanho BETWEEN ? AND ? "
                        "GROUP BY g.chave ORDER BY COUNT(*) DESC, e.prioridade DESC, e.registrado_em DESC LIMIT ?",
                        (*grams, _assinatura_json(assinatura), len(texto) - _approx_max_edits(),
                         len(texto) + _approx_max_edits(), limite),
                    ).fetchall()
                    ids.extend(str(row[0]) for row in rows)
            # Cada busca retorna um conjunto pequeno; não há SELECT do catálogo.
            unique = list(dict.fromkeys(ids))[:limite]
            disk_candidates: dict[str, EscolhaBusca] = {}
            if unique:
                placeholders = ",".join("?" for _ in unique)
                for row in conn.execute(
                    "SELECT chave, consulta, origem, prioridade, track_json, registrado_em FROM escolhas "
                    f"WHERE chave IN ({placeholders}) ORDER BY registrado_em DESC", unique,
                ):
                    escolha = _escolha_row(row)
                    if escolha is not None:
                        disk_candidates[escolha.chave] = escolha
            # Candidatos de disco já foram filtrados por índices. RAM completa
            # apenas as vagas para preservar o modo degradado sem exceder teto.
            for key, escolha in candidatos.items():
                if len(disk_candidates) >= limite:
                    break
                disk_candidates.setdefault(key, escolha)
            candidatos = disk_candidates
    except Exception:
        logger.debug("[music/search-memory] falha ao consultar candidatos persistentes", exc_info=True)
    return sorted(candidatos.values(), key=lambda item: item.registrado_em, reverse=True)[:limite]


def _persistir_varias(escolhas: Sequence[EscolhaBusca], *, raise_errors: bool = False) -> None:
    if not escolhas:
        return
    try:
        with _LOCK, _abrir_db() as conn:
            stamp = time.time()
            for escolha in escolhas:
                payload = json.dumps(_track_payload(escolha.track), ensure_ascii=False, separators=(",", ":"))
                cursor = conn.execute(
                    "INSERT INTO escolhas(chave, consulta, origem, prioridade, track_json, registrado_em) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chave) DO UPDATE SET "
                    "consulta=excluded.consulta, origem=excluded.origem, prioridade=excluded.prioridade, "
                    "track_json=excluded.track_json, registrado_em=excluded.registrado_em "
                    "WHERE excluded.prioridade>escolhas.prioridade OR "
                    "(excluded.prioridade=escolhas.prioridade AND excluded.registrado_em>=escolhas.registrado_em)",
                    (escolha.chave, escolha.consulta, escolha.origem, escolha.prioridade, payload, escolha.registrado_em),
                )
                if not cursor.rowcount:
                    # Outro processo pode ter registrado um link soberano entre
                    # o lookup e esta transação; seu índice permanece intacto.
                    row = conn.execute(
                        "SELECT chave, consulta, origem, prioridade, track_json, registrado_em FROM escolhas WHERE chave=?",
                        (escolha.chave,),
                    ).fetchone()
                    atual = _escolha_row(row) if row is not None else None
                    if atual is not None:
                        _cache_escolha(atual)
                    continue
                _indexar_db(conn, escolha)
                # Escolha e entrega ao arquivador pertencem à mesma transação.
                _enfileirar_arquivo(conn, escolha.track, stamp=stamp)
    except Exception:
        if raise_errors:
            raise
        logger.warning("[music/search-memory] falha ao persistir escolha; mantendo RAM", exc_info=True)


def obter_pendencias_arquivamento(limit: int = 128) -> list[tuple[str, MusicTrack, float]]:
    """Lê uma página durável; confirmação só depois de persistir no arquivador."""
    _ensure_loaded()
    with _LOCK, _abrir_db() as conn:
        rows = conn.execute(
            "SELECT chave, track_json, registrado_em FROM escolhas_arquivo_outbox "
            "ORDER BY registrado_em, chave LIMIT ?", (max(1, min(2048, int(limit))),),
        ).fetchall()
    result = []
    for key, encoded, stamp in rows:
        try:
            payload = json.loads(encoded)
            if isinstance(payload, dict):
                result.append((str(key), _track_from_payload(payload), float(stamp)))
        except (TypeError, ValueError):
            logger.warning("[music/search-memory] evento de arquivamento inválido: %s", key)
    return result


def confirmar_pendencia_arquivamento(key: str, registrado_em: float | None = None) -> None:
    with _LOCK, _abrir_db() as conn:
        if registrado_em is None:
            conn.execute("DELETE FROM escolhas_arquivo_outbox WHERE chave=?", (str(key),))
        else:
            conn.execute("DELETE FROM escolhas_arquivo_outbox WHERE chave=? AND registrado_em<=?",
                         (str(key), float(registrado_em)))


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
    preservar_mais_novo: bool = False,
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
        anterior = _memoria.get(chave) or _buscar_exata_persistida(chave)
        if anterior is not None and (anterior.prioridade > int(prioridade) or (
            preservar_mais_novo and anterior.prioridade == int(prioridade) and anterior.registrado_em > stamp
        )):
            # Link direto é autoridade maior e não é substituído por uma escolha
            # posterior feita no seletor de resultados.
            _cache_escolha(anterior)
            return None
        escolha = EscolhaBusca(
            chave=chave,
            consulta=clean_query,
            track=_copiar_track_limpo(track),
            registrado_em=stamp,
            origem=str(origem),
            prioridade=int(prioridade),
        )
        _cache_escolha(escolha)
        if persistir:
            _persistir(escolha)
        return escolha


def limpar_memoria_busca() -> None:
    """Limpa RAM e a persistência do checkout atual.

    Em worktrees/testes o banco fica em /tmp e nunca toca o banco da VPS real.
    """
    global _loaded, _load_retry_after
    with _LOCK:
        _memoria.clear()
        _limpar_indices()
        _loaded = True
        _load_retry_after = 0.0
        try:
            path = _db_path()
            _schema_ready.pop(str(path), None)
            for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                candidate.unlink(missing_ok=True)
        except Exception:
            logger.debug("[music/search-memory] falha ao limpar persistência", exc_info=True)


def recarregar_memoria_busca() -> None:
    """Descarta somente RAM e recarrega o banco, simulando restart do bot."""
    global _loaded, _load_retry_after
    with _LOCK:
        _memoria.clear()
        _limpar_indices()
        _loaded = False
        _load_retry_after = 0.0
    _ensure_loaded()


def faixa_aprendida(track: MusicTrack) -> bool:
    """Consulta a memória real por identidade da mídia, sem busca aproximada."""
    from .arquivo import _url_key, media_key

    target = media_key(track)
    origins = {_url_key(getattr(track, "original_url", "")), _url_key(getattr(track, "webpage_url", ""))}
    origins.discard("")
    if not target and not origins:
        return False
    _ensure_loaded()
    with _LOCK:
        if any(media_key(item.track) == target or bool(
            origins & {_url_key(item.track.original_url), _url_key(item.track.webpage_url)}
        ) for item in _memoria.values()):
            return True
        try:
            with _abrir_db() as conn:
                clauses = []
                params: list[str] = []
                if target:
                    clauses.append("media_key=?")
                    params.append(target)
                for origin in origins:
                    clauses.extend(("original_key=?", "webpage_key=?"))
                    params.extend((origin, origin))
                return conn.execute("SELECT 1 FROM escolhas_indice WHERE " + " OR ".join(clauses) + " LIMIT 1",
                                    params).fetchone() is not None
        except Exception:
            logger.debug("[music/search-memory] falha ao consultar identidade persistente", exc_info=True)
            return False



def _remover_sufixos_titulo(value: str) -> str:
    texto = str(value or "").strip()
    # Resultados de YouTube frequentemente terminam em ``(Official Video)`` ou
    # ``[Game/Album]``. Para a memoria de escolha, a versao curta e mais util
    # como alias e evita uma nova consulta externa na proxima forma equivalente.
    for _ in range(3):
        reduzido = _SUFFIX_TITULO_RE.sub("", texto).strip(" -–—|·")
        if reduzido == texto or not reduzido:
            break
        texto = reduzido
    return texto


def _componentes_alias_track(track: MusicTrack) -> tuple[str, str, str]:
    titulo_bruto = str(getattr(track, "display_title", "") or track.title or "").strip()
    if not titulo_bruto:
        return "", "", ""
    consulta_original = analisar_consulta(titulo_bruto)
    # Nao transforme uma escolha explicitamente live/remix/instrumental/lyrics
    # em alias neutro. A degradacao de qualidade desta fase e intencional, mas
    # ainda preservamos versoes que mudam a gravacao ou a forma pedida.
    preservar_sufixo = bool(consulta_original.atributos or "lyrics" in consulta_original.apresentacao)
    titulo_limpo = titulo_bruto if preservar_sufixo else (_remover_sufixos_titulo(titulo_bruto) or titulo_bruto)
    uploader = str(getattr(track, "display_uploader", "") or track.uploader or "").strip()
    consulta = analisar_consulta(titulo_limpo)
    titulo_musica = str(consulta.titulo or consulta.texto or titulo_limpo).strip()
    artista = str(consulta.artista or uploader).strip()

    # Quando o resultado vem como ``Titulo - Artista``, o uploader real e um
    # sinal barato melhor que tentar sofisticar o parser. Corrigimos apenas as
    # duas formas obvias para gerar alias; isso nao afeta a ordem da pesquisa.
    base = texto_basico(titulo_limpo)
    up = texto_basico(uploader)
    if up and base:
        if base.startswith(up + " "):
            resto = base[len(up):].strip()
            if resto:
                artista, titulo_musica = uploader, resto
        elif base.endswith(" " + up):
            resto = base[:-len(up)].strip()
            if resto:
                artista, titulo_musica = uploader, resto
    return titulo_bruto, titulo_musica, artista


def _deduplicar_aliases(candidatos: Sequence[str]) -> tuple[str, ...]:
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


def _aliases_selecao(query: str, track: MusicTrack) -> tuple[str, ...]:
    _titulo_bruto, titulo_musica, artista = _componentes_alias_track(track)
    candidatos: list[str] = [str(query or "").strip()]
    if titulo_musica:
        candidatos.append(titulo_musica)
        if artista:
            candidatos.append(f"{artista} - {titulo_musica}")
    # Uma escolha humana ensina no maximo tres formas. Isso aumenta muito a
    # cobertura da memoria sem transformar resultados de pesquisa em cache.
    return _deduplicar_aliases(candidatos)[:3]


def _registrar_aliases(
    aliases: Sequence[str],
    track: MusicTrack,
    *,
    origem: str,
    prioridade: int,
    now: float | None,
) -> tuple[EscolhaBusca | None, ...]:
    escolhas: list[EscolhaBusca | None] = []
    persistir: list[EscolhaBusca] = []
    for alias in aliases:
        escolha = _registrar(
            alias,
            track,
            origem=origem,
            prioridade=prioridade,
            now=now,
            persistir=False,
        )
        escolhas.append(escolha)
        if escolha is not None:
            persistir.append(escolha)
    _persistir_varias(persistir)
    return tuple(escolhas)


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
    """Aprende consulta+título da escolha; link direto continua soberano."""
    aliases = _aliases_selecao(query, track)
    if not aliases:
        return False
    resultados = _registrar_aliases(
        aliases,
        track,
        origem=_ORIGEM_SELECAO,
        prioridade=_PRIORIDADE_SELECAO,
        now=now,
    )
    if any(resultados):
        from .arquivo import note_learned
        note_learned((track,))
    # Mantem o contrato historico: o retorno informa se a consulta original
    # foi registrada, mesmo que aliases secundarios tenham sido bloqueados por
    # uma escolha de link com prioridade maior.
    return bool(resultados and resultados[0] is not None)


def _aliases_link(track: MusicTrack) -> tuple[str, ...]:
    titulo_bruto, titulo_musica, artista = _componentes_alias_track(track)
    if not titulo_bruto:
        return ()

    candidatos: list[str] = [titulo_bruto]
    if titulo_musica:
        candidatos.append(titulo_musica)
        tokens = tokens_texto(titulo_musica, remover_ruido=True)
        if tokens and tokens[0] not in _STOPWORDS_ALIAS:
            candidatos.append(tokens[0])
        if artista:
            candidatos.append(f"{artista} - {titulo_musica}")
    return _deduplicar_aliases(candidatos)


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
    resultados = _registrar_aliases(
        aliases,
        track,
        origem=_ORIGEM_LINK,
        prioridade=_PRIORIDADE_LINK,
        now=now,
    )
    if any(resultados):
        from .arquivo import note_learned
        note_learned((track,))
    return tuple(alias for alias, escolha in zip(aliases, resultados) if escolha is not None)



def registrar_lote_link_busca(
    tracks: Sequence[MusicTrack],
    *,
    now: float | None = None,
    raise_errors: bool = False,
    preservar_mais_novo: bool = False,
) -> int:
    """Aprende em lote os mesmos aliases agressivos usados por link direto.

    Playlists de metadata já conhecem título/artista antes de tocar. Registrar a
    janela inteira faz ``_play <nome>`` virar direct-hit sem consultar novamente
    a busca externa. A persistência é feita numa única transação para não
    acrescentar uma sequência de I/O síncrono ao comando da playlist.
    """
    persistir: list[EscolhaBusca] = []
    registrados = 0
    for track in tracks:
        original = str(getattr(track, "original_url", "") or getattr(track, "webpage_url", "") or "").strip().lower()
        if not original.startswith(("http://", "https://", "www.")):
            continue
        aliases = _aliases_link(track)
        for alias in aliases:
            escolha = _registrar(
                alias,
                track,
                origem=_ORIGEM_LINK,
                prioridade=_PRIORIDADE_LINK,
                now=now,
                persistir=False,
                preservar_mais_novo=preservar_mais_novo,
            )
            if escolha is not None:
                persistir.append(escolha)
                registrados += 1
    if persistir:
        _persistir_varias(persistir, raise_errors=raise_errors)
        from .arquivo import note_learned
        note_learned((escolha.track for escolha in persistir))
    return registrados


def enfileirar_lote_link_busca(tracks: Sequence[MusicTrack]) -> tuple[str, ...]:
    """Commit curto antes do play; aliases e índices ficam para depois do som.

    Os dados públicos da janela ficam duráveis mesmo se o bot reiniciar entre
    enqueue e aprendizagem. Não persiste URLs assinadas nem áudio.
    """
    payloads = [_track_payload(track) for track in tracks
                if str(track.original_url or track.webpage_url or "").lower().startswith(("http://", "https://", "www."))]
    ids = []
    if not payloads:
        return ()
    with _LOCK, _abrir_db() as conn:
        stamp = time.time()
        for start in range(0, len(payloads), 32):
            batch_id = uuid.uuid4().hex
            conn.execute("INSERT INTO escolhas_link_pendentes(batch_id, tracks_json, registrado_em) VALUES (?, ?, ?)",
                         (batch_id, json.dumps(payloads[start:start + 32], ensure_ascii=False, separators=(",", ":")), stamp))
            ids.append(batch_id)
    return tuple(ids)


def processar_lotes_link_pendentes(*, limit: int = 2) -> int:
    """Replay limitado; ACK só após commit de escolhas, índices e outbox."""
    with _LOCK, _abrir_db() as conn:
        rows = conn.execute("SELECT batch_id, tracks_json, registrado_em FROM escolhas_link_pendentes "
                            "ORDER BY registrado_em, batch_id LIMIT ?", (max(1, min(16, int(limit))),)).fetchall()
    processed = 0
    for batch_id, encoded, stamp in rows:
        payloads = json.loads(encoded)
        tracks = [_track_from_payload(payload) for payload in payloads if isinstance(payload, dict)]
        registrar_lote_link_busca(tracks, now=float(stamp), raise_errors=True, preservar_mais_novo=True)
        with _LOCK, _abrir_db() as conn:
            conn.execute("DELETE FROM escolhas_link_pendentes WHERE batch_id=?", (batch_id,))
        processed += 1
    return processed

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
        escolha = _memoria.get(chave) or _buscar_exata_persistida(chave)
        if escolha is None:
            escolha = _buscar_lexical_agressiva(clean_query)
        if escolha is None:
            escolha = _buscar_aproximada(clean_query)
            if escolha is None:
                return None
        _cache_escolha(escolha)
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
        removida = _memoria.pop(chave, None) or _buscar_exata_persistida(chave)
        if removida is None:
            return False
        _desindexar_chave(chave)
        try:
            with _abrir_db() as conn:
                conn.execute("DELETE FROM escolhas WHERE chave = ?", (chave,))
        except Exception:
            logger.warning("[music/search-memory] falha ao remover escolha persistida", exc_info=True)
        return True
