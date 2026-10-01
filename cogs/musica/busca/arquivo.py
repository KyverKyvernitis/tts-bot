"""Índice leve do arquivo de músicas; os bytes vivem apenas no Discord."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .memoria import _db_path, _track_payload

log = logging.getLogger(__name__)
_learned_lock = threading.Lock()
_new_learned: dict[str, tuple[dict, float]] = {}
_SCHEMA_VERSION = 11
_schema_lock = threading.Lock()
_RETRY_MAX_SECONDS = 6 * 3600


class _ArchiveConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def _stable_source(url: str) -> str:
    """Só páginas públicas de mídia; nunca um stream assinado ou um link local."""
    try:
        parsed = urlsplit(str(url or "").strip())
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None:
            return ""
        if host in {"youtube.com", "m.youtube.com", "music.youtube.com"} and parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
            return f"https://www.youtube.com/watch?v={video_id}" if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else ""
        if host == "youtu.be":
            video_id = parsed.path.strip("/")
            return f"https://www.youtube.com/watch?v={video_id}" if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else ""
        if host.endswith(".bandcamp.com") and re.fullmatch(r"/track/[^/?#]+/?", parsed.path):
            source = f"https://{host}{parsed.path.rstrip('/')}"
            return source if len(source) <= 500 else ""
        if host == "soundcloud.com" and re.fullmatch(r"/[^/?#]+/[^/?#]+/?", parsed.path):
            source = f"https://{host}{parsed.path.rstrip('/')}"
            return source if len(source) <= 500 else ""
    except ValueError:
        pass
    return ""


def _artist_key(payload: dict) -> str:
    raw = str(payload.get("display_uploader") or payload.get("uploader") or "").strip()
    artist = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().lower()
    artist = " ".join(re.findall(r"[a-z0-9]+", artist))
    return artist[:120] if artist not in {"", "spotify", "youtube", "apple music", "deezer", "unknown"} else ""


def _bandcamp_slug(payload: dict) -> str:
    title = str(payload.get("display_title") or payload.get("title") or "").strip()
    artist = str(payload.get("display_uploader") or payload.get("uploader") or "").strip()
    if artist:
        title = re.sub(rf"^\s*{re.escape(artist)}\s*[-–—:]\s*", "", title, flags=re.IGNORECASE)
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", title).strip("-")[:120]


def _audit(db: sqlite3.Connection, key: str, event: str, *, reason: str = "", source: str = "", revision: str = "") -> None:
    db.execute("INSERT INTO arquivo_eventos(chave, ocorrido_em, evento, motivo, fonte, revisao) VALUES (?, ?, ?, ?, ?, ?)",
               (key, time.time(), event[:30], reason[:48], _stable_source(source), revision[:60]))


def _url_key(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or not host or host.endswith("discord.com") or host.endswith("discordapp.com"):
            return ""
        path = parsed.path.rstrip("/")
        if host in {"youtube.com", "m.youtube.com", "music.youtube.com"} and path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
            if video_id:
                url = "youtube:" + video_id
        elif host == "youtu.be":
            url = "youtube:" + path.lstrip("/")
        else:
            url = host + path
    except ValueError:
        return ""
    return hashlib.sha256(url.lower().encode("utf-8")).hexdigest()[:32]


def _url_aliases(url: str) -> tuple[str, ...]:
    """Aliases canônicos novos sem renomear chaves publicadas no fórum."""
    keys = [_url_key(url)]
    identity = _public_identity(url)
    if identity and identity[0] == "open.spotify.com":
        keys.append(_url_key(f"https://open.spotify.com{identity[1]}"))
    elif identity and identity[0] == "youtube" and identity[1]:
        keys.append(_url_key(f"https://www.youtube.com/watch?v={identity[1]}"))
    return tuple(dict.fromkeys(key for key in keys if key))


def _remember_urls(db: sqlite3.Connection, key: str, urls) -> None:
    for url in urls:
        for alias in _url_aliases(url):
            db.execute("INSERT OR IGNORE INTO arquivo_aliases VALUES (?, ?)", (alias, key))


def media_key(track) -> str:
    if getattr(track, "is_live", False) or (getattr(track, "attachment_ref", None) and not getattr(track, "archive_ref", None)):
        return ""
    original = str(getattr(track, "original_url", "") or "")
    if any(provider in original.lower() for provider in ("open.spotify.com/track/", "deezer.com/track/", "music.apple.com/")):
        return _url_key(original)
    return _url_key(getattr(track, "webpage_url", "")) or _url_key(original)


def _initialize_db(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS arquivo_config (id INTEGER PRIMARY KEY CHECK (id=1), guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, channel_type TEXT NOT NULL DEFAULT 'text')")
    db.execute("""CREATE TABLE IF NOT EXISTS arquivo_musicas (
        chave TEXT PRIMARY KEY, track_json TEXT NOT NULL, tocadas INTEGER NOT NULL DEFAULT 0,
        reference_json TEXT NOT NULL DEFAULT '', emoji TEXT NOT NULL DEFAULT '',
        estado TEXT NOT NULL DEFAULT 'waiting', tentativa_em REAL NOT NULL DEFAULT 0,
        falhas INTEGER NOT NULL DEFAULT 0, apresentacao INTEGER NOT NULL DEFAULT 1
    )""")
    if db.execute("PRAGMA user_version").fetchone()[0] < 3:
        config_columns = {row[1] for row in db.execute("PRAGMA table_info(arquivo_config)")}
        if "channel_type" not in config_columns:
            db.execute("ALTER TABLE arquivo_config ADD COLUMN channel_type TEXT NOT NULL DEFAULT 'text'")
        columns = {row[1] for row in db.execute("PRAGMA table_info(arquivo_musicas)")}
        if "apresentacao" not in columns:
            db.execute("ALTER TABLE arquivo_musicas ADD COLUMN apresentacao INTEGER NOT NULL DEFAULT 1")
        db.execute("PRAGMA user_version=3")
    if db.execute("PRAGMA user_version").fetchone()[0] < 4:
        db.execute("ALTER TABLE arquivo_musicas ADD COLUMN aprendida_em REAL NOT NULL DEFAULT 0")
        db.execute("PRAGMA user_version=4")
    if db.execute("PRAGMA user_version").fetchone()[0] < 5:
        # Uma única vez na atualização: posts v4–v6 em backoff cosmético
        # podem ser corrigidos já pelo agente novo, sem esperar mais uma hora.
        db.execute("UPDATE arquivo_musicas SET tentativa_em=0 WHERE reference_json!='' AND apresentacao BETWEEN 4 AND 6")
        db.execute("PRAGMA user_version=5")
    if db.execute("PRAGMA user_version").fetchone()[0] < 6:
        # Com a busca corrigida no agente 0.3.79, as faixas antigas que
        # falharam não precisam esperar o backoff anterior para tentar de novo.
        db.execute("UPDATE arquivo_musicas SET tentativa_em=0 WHERE estado='failed' AND reference_json=''")
        db.execute("PRAGMA user_version=6")
    if db.execute("PRAGMA user_version").fetchone()[0] < 7:
        if "fonte_arquivo" not in {row[1] for row in db.execute("PRAGMA table_info(arquivo_musicas)")}:
            db.execute("ALTER TABLE arquivo_musicas ADD COLUMN fonte_arquivo TEXT NOT NULL DEFAULT ''")
        db.execute("PRAGMA user_version=7")
    if db.execute("PRAGMA user_version").fetchone()[0] < 8:
        columns = {row[1] for row in db.execute("PRAGMA table_info(arquivo_musicas)")}
        for name, kind in (("fonte_estavel", "TEXT NOT NULL DEFAULT ''"),
                           ("motivo_falha", "TEXT NOT NULL DEFAULT ''"),
                           ("ultima_tentativa_em", "REAL NOT NULL DEFAULT 0"),
                           ("revisao_agente", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                db.execute(f"ALTER TABLE arquivo_musicas ADD COLUMN {name} {kind}")
        db.execute("PRAGMA user_version=8")
    db.execute("""CREATE TABLE IF NOT EXISTS arquivo_eventos (
        id INTEGER PRIMARY KEY, chave TEXT NOT NULL, ocorrido_em REAL NOT NULL,
        evento TEXT NOT NULL, motivo TEXT NOT NULL DEFAULT '',
        fonte TEXT NOT NULL DEFAULT '', revisao TEXT NOT NULL DEFAULT ''
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS arquivo_artistas (
        artista TEXT PRIMARY KEY, dominio TEXT NOT NULL, confirmado_em REAL NOT NULL
    )""")
    db.execute("CREATE TABLE IF NOT EXISTS arquivo_reproducoes (marcador TEXT PRIMARY KEY, registrado_em REAL NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS arquivo_aliases (alias TEXT PRIMARY KEY, chave TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS arquivo_limpezas (
        chave TEXT NOT NULL, message_id INTEGER NOT NULL, previous_json TEXT NOT NULL,
        tentativa_em REAL NOT NULL DEFAULT 0, PRIMARY KEY(chave, message_id)
    )""")
    if db.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
        columns = {row[1] for row in db.execute("PRAGMA table_info(arquivo_musicas)")}
        if "enfileirada_em" not in columns:
            db.execute("ALTER TABLE arquivo_musicas ADD COLUMN enfileirada_em REAL NOT NULL DEFAULT 0")
        if "retry_reference_json" not in columns:
            db.execute("ALTER TABLE arquivo_musicas ADD COLUMN retry_reference_json TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE arquivo_musicas SET enfileirada_em=aprendida_em WHERE enfileirada_em=0 AND aprendida_em>0")
        # Os limites antigos eram decisões locais, não indisponibilidade real.
        db.execute("""UPDATE arquivo_musicas SET estado='waiting', tentativa_em=0, falhas=0,
                   motivo_falha='' WHERE reference_json='' AND
                   (estado='paused' OR (estado='ineligible' AND motivo_falha='duration_limit')
                    OR (estado='too_large' AND motivo_falha='size_limit'))""")
        cursor = db.execute("SELECT chave, track_json FROM arquivo_musicas")
        while rows := cursor.fetchmany(256):
            for key, raw in rows:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                _remember_urls(db, key, (payload.get("webpage_url"), payload.get("original_url")))
        db.execute("CREATE INDEX IF NOT EXISTS arquivo_pendentes ON arquivo_musicas(tentativa_em, enfileirada_em, chave)")
        db.execute("""CREATE INDEX IF NOT EXISTS arquivo_fila_nova ON arquivo_musicas(enfileirada_em, chave)
                   WHERE reference_json='' AND estado NOT IN ('too_large', 'ineligible', 'unavailable', 'paused')
                   AND (aprendida_em>0 OR tocadas>=1)""")
        db.execute("""CREATE INDEX IF NOT EXISTS arquivo_fila_atualizacoes ON arquivo_musicas(enfileirada_em, chave)
                   WHERE reference_json!='' AND (aprendida_em>0 OR tocadas>=1)""")
        db.execute("CREATE INDEX IF NOT EXISTS arquivo_reproducoes_data ON arquivo_reproducoes(registrado_em)")
        db.execute("CREATE INDEX IF NOT EXISTS arquivo_limpezas_pendentes ON arquivo_limpezas(tentativa_em, chave)")
        db.execute("CREATE INDEX IF NOT EXISTS arquivo_eventos_chave_id ON arquivo_eventos(chave, id DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS arquivo_falhas ON arquivo_musicas(estado, ultima_tentativa_em DESC, chave)")
        db.execute("CREATE INDEX IF NOT EXISTS arquivo_escolhas_data ON arquivo_musicas(aprendida_em, chave)")
        db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")


def _db() -> sqlite3.Connection:
    # Configuração e IDs sobrevivem à limpeza da memória de escolhas.
    memory_path: Path = _db_path()
    path = memory_path.with_name(memory_path.stem + "-arquivo.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5, factory=_ArchiveConnection)
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=NORMAL")
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
            with _schema_lock:
                if db.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
                    db.execute("PRAGMA journal_mode=WAL")
                    _initialize_db(db)
                    db.commit()
        return db
    except Exception:
        db.close()
        raise


def channel() -> tuple[int, int]:
    with _db() as db:
        row = db.execute("SELECT guild_id, channel_id FROM arquivo_config WHERE id=1").fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def channel_type() -> str:
    with _db() as db:
        row = db.execute("SELECT channel_type FROM arquivo_config WHERE id=1").fetchone()
    return str(row[0]) if row else "text"


def set_source_override(key: str, url: str) -> None:
    """Vincula uma página oficial ao arquivo sem alterar a chave da faixa aprendida."""
    key = str(key or "").strip().lower()
    url = _stable_source(url)
    if (not re.fullmatch(r"[a-f0-9]{32}", key)
            or not (urlsplit(url).hostname or "").endswith(".bandcamp.com")):
        raise ValueError("use a página pública de uma faixa do Bandcamp")
    with _db() as db:
        row = db.execute("SELECT reference_json FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()
        if row is None or row[0]:
            raise ValueError("use a chave de uma música aprendida ainda não arquivada")
        db.execute("""UPDATE arquivo_musicas SET fonte_arquivo=?, tentativa_em=0,
                   falhas=0, motivo_falha='', estado='waiting' WHERE chave=?""", (url, key))
        _audit(db, key, "fonte_registrada", source=url)


def requeue(key: str) -> None:
    key = str(key or "").strip().lower()
    with _db() as db:
        row = db.execute("SELECT reference_json FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()
        if not re.fullmatch(r"[a-f0-9]{32}", key) or row is None or row[0]:
            raise ValueError("use a chave de uma música aprendida ainda não arquivada")
        db.execute("""UPDATE arquivo_musicas SET estado='waiting', falhas=0,
                   tentativa_em=0, motivo_falha='' WHERE chave=?""", (key,))
        _audit(db, key, "reavaliacao_manual")


def _bandcamp_candidate_db(db: sqlite3.Connection, track: dict) -> str:
    artist, slug = _artist_key(track), _bandcamp_slug(track)
    if not artist or not 2 <= len(slug) <= 120:
        return ""
    row = db.execute("SELECT dominio FROM arquivo_artistas WHERE artista=?", (artist,)).fetchone()
    return f"https://{row[0]}/track/{slug}" if row else ""


def bandcamp_candidate(track: dict) -> str:
    """Uma tentativa barata no domínio já confirmado deste artista."""
    with _db() as db:
        return _bandcamp_candidate_db(db, track)


def queue_discovered_source(key: str, url: str) -> bool:
    """Só aceita uma faixa do domínio aprendido depois de validar metadados."""
    url = _stable_source(url)
    with _db() as db:
        row = db.execute("SELECT track_json, reference_json, fonte_arquivo FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()
        if not row or row[1] or row[2]:
            return False
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return False
        candidate = _bandcamp_candidate_db(db, payload)
        if not url or url != candidate:
            return False
        db.execute("""UPDATE arquivo_musicas SET fonte_arquivo=?, estado='waiting',
                   falhas=0, motivo_falha='', tentativa_em=0 WHERE chave=?""", (url, key))
        _audit(db, key, "fonte_descoberta", source=url)
    return True


def note_discovery_failure(key: str, reason: str) -> None:
    if reason not in {"source_mismatch", "source_resolve_error"}:
        return
    with _db() as db:
        _audit(db, key, "fonte_rejeitada", reason=reason)


def set_channel(guild_id: int, channel_id: int, *, kind: str = "text") -> None:
    if kind not in {"text", "forum"}:
        raise ValueError("tipo de canal inválido")
    with _db() as db:
        previous = db.execute("SELECT guild_id, channel_id, channel_type FROM arquivo_config WHERE id=1").fetchone()
        db.execute("""INSERT INTO arquivo_config(id, guild_id, channel_id, channel_type) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET guild_id=excluded.guild_id,
            channel_id=excluded.channel_id, channel_type=excluded.channel_type""", (guild_id, channel_id, kind))
        if guild_id > 0 and channel_id > 0 and previous and tuple(previous) != (guild_id, channel_id, kind):
            # No fórum, a referência antiga continua tocável durante a migração.
            for key, encoded in db.execute("SELECT chave, reference_json FROM arquivo_musicas WHERE reference_json!=''"):
                try:
                    ref = json.loads(encoded)
                    same_guild = int(ref["guild_id"]) == guild_id
                    valid_here = same_guild and ((kind == "forum" and int(ref.get("forum_id") or 0) == channel_id)
                                                 or (kind == "text" and int(ref["channel_id"]) == channel_id))
                except (ValueError, KeyError, TypeError):
                    same_guild = False
                    valid_here = False
                if not valid_here:
                    if kind == "forum" and same_guild:
                        db.execute("UPDATE arquivo_musicas SET apresentacao=MIN(apresentacao, 2), tentativa_em=0 WHERE chave=?", (key,))
                    else:
                        db.execute("""UPDATE arquivo_musicas SET reference_json='', emoji='', estado='waiting',
                            tentativa_em=0 WHERE chave=?""", (key,))
                        db.execute("DELETE FROM arquivo_limpezas WHERE chave=?", (key,))


def record_play(track, marker: str) -> bool:
    key = media_key(track)
    try:
        duration = float(track.duration)
    except (TypeError, ValueError):
        return False
    if not key or not math.isfinite(duration) or duration <= 0 or len(marker) > 128:
        return False
    with _db() as db:
        if not db.execute("INSERT OR IGNORE INTO arquivo_reproducoes VALUES (?, ?)", (marker, time.time())).rowcount:
            return False
        previous = db.execute("SELECT fonte_estavel, estado, reference_json, track_json FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()
        payload = _track_payload(track)
        if previous:
            try:
                old_payload = json.loads(previous[3])
                if _metadata_score(old_payload) > _metadata_score(payload):
                    payload = old_payload
            except (TypeError, ValueError):
                pass
        db.execute("""INSERT INTO arquivo_musicas(chave, track_json, tocadas, enfileirada_em) VALUES (?, ?, 1, ?)
            ON CONFLICT(chave) DO UPDATE SET tocadas=MIN(2, tocadas+1), track_json=excluded.track_json""",
            (key, json.dumps(payload, ensure_ascii=False), time.time()))
        source = _stable_source(getattr(track, "webpage_url", ""))
        if source and (previous is None or previous[0] != source):
            if previous and previous[1] in {"unavailable", "paused"} and not previous[2]:
                db.execute("""UPDATE arquivo_musicas SET fonte_estavel=?, estado='waiting',
                           falhas=0, motivo_falha='', tentativa_em=0 WHERE chave=?""", (source, key))
            else:
                db.execute("UPDATE arquivo_musicas SET fonte_estavel=? WHERE chave=?", (source, key))
            _audit(db, key, "fonte_observada", source=source)
        _remember_urls(db, key, (getattr(track, "webpage_url", ""), getattr(track, "original_url", "")))
        db.execute("DELETE FROM arquivo_reproducoes WHERE registrado_em < ?", (time.time() - 90 * 86400,))
    return True


def note_learned(tracks) -> None:
    """Compatibilidade: a origem durável dos jobs agora é o outbox da memória."""
    # Não manter outra cópia de playlists em RAM. A memória escreve o outbox
    # na mesma transação das escolhas, antes desta notificação.


def flush_learned(limit: int = 128) -> int:
    """Entrega e confirma uma página; reinício/falha pode repetir sem perder jobs."""
    from .memoria import obter_pendencias_arquivamento, confirmar_pendencia_arquivamento
    items = obter_pendencias_arquivamento(limit=limit)
    if not items:
        return 0
    registered = register_learned_batch([(_track_payload(track), stamp) for _key, track, stamp in items])
    # Somente depois do commit do catálogo. Ack condicionado ao timestamp
    # observado não apaga uma escolha nova que chegou durante o registro.
    for key, _track, stamp in items:
        confirmar_pendencia_arquivamento(key, registrado_em=stamp)
    return registered


def learned_page(after: str = "", limit: int = 128) -> tuple[str, list[tuple[dict, float]]]:
    """Página pequena da memória persistente, incluindo playlists não tocadas."""
    from .memoria import _abrir_db
    with _abrir_db() as db:
        rows = db.execute("SELECT chave, track_json, registrado_em FROM escolhas WHERE chave>? "
                          "ORDER BY chave LIMIT ?", (after, max(1, min(256, limit)))).fetchall()
    parsed = []
    for _alias, encoded, stamp in rows:
        try:
            payload = json.loads(encoded)
            if isinstance(payload, dict):
                parsed.append((payload, float(stamp)))
        except (TypeError, ValueError):
            continue
    return (rows[-1][0] if rows else after), parsed


def learned_updates(after: tuple[float, str] = (0.0, ""), limit: int = 128) -> tuple[tuple[float, str], list[tuple[dict, float]]]:
    """Retoma por data e alias; captura também alterações em escolhas antigas."""
    from .memoria import _abrir_db
    with _abrir_db() as db:
        rows = db.execute("SELECT chave, track_json, registrado_em FROM escolhas "
                          "WHERE registrado_em>? OR (registrado_em=? AND chave>?) "
                          "ORDER BY registrado_em, chave LIMIT ?",
                          (after[0], after[0], after[1], max(1, min(256, limit)))).fetchall()
    parsed = []
    for _alias, encoded, stamp in rows:
        try:
            payload = json.loads(encoded)
            if isinstance(payload, dict):
                parsed.append((payload, float(stamp)))
        except (TypeError, ValueError):
            continue
    return ((float(rows[-1][2]), str(rows[-1][0])) if rows else after), parsed


def _metadata_score(payload: dict) -> tuple[int, int, int, int]:
    url = str(payload.get("webpage_url") or "")
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    playable = url.startswith("https://") and not host.endswith(("spotify.com", "music.apple.com", "deezer.com"))
    return (int(playable), int(bool(payload.get("duration"))),
            int(bool(payload.get("display_thumbnail") or payload.get("thumbnail"))),
            sum(bool(payload.get(field)) for field in ("original_url", "display_title", "display_source", "uploader")))


def register_learned_batch(items: list[tuple[dict, float]]) -> int:
    """Uma linha por música, nunca uma linha por alias; preserva IDs já publicados."""
    from .memoria import _track_from_payload
    candidates: dict[str, tuple[dict, float]] = {}
    for payload, stamp in items:
        track = _track_from_payload(payload)
        key = media_key(track)
        if not key or not str(track.webpage_url or "").startswith("https://"):
            continue
        try:
            duration = float(track.duration or 0)
        except (TypeError, ValueError):
            duration = 0
        if track.is_live or not math.isfinite(duration) or duration < 0:
            continue
        previous = candidates.get(key)
        if previous is None or (_metadata_score(payload), stamp) > (_metadata_score(previous[0]), previous[1]):
            candidates[key] = (payload, stamp)
    if not candidates:
        return 0
    with _db() as db:
        for key, (payload, stamp) in candidates.items():
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            row = db.execute("SELECT track_json, aprendida_em, fonte_estavel, estado, reference_json FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()
            selected = row is None
            if row is None:
                db.execute("INSERT INTO arquivo_musicas(chave, track_json, aprendida_em, enfileirada_em) VALUES (?, ?, ?, ?)",
                           (key, encoded, stamp, time.time()))
            else:
                try:
                    old = json.loads(row[0])
                except (ValueError, TypeError):
                    old = {}
                if _metadata_score(payload) > _metadata_score(old):
                    selected = True
                    db.execute("UPDATE arquivo_musicas SET track_json=?, aprendida_em=MAX(aprendida_em, ?) WHERE chave=?",
                               (encoded, stamp, key))
                else:
                    db.execute("UPDATE arquivo_musicas SET aprendida_em=MAX(aprendida_em, ?) WHERE chave=?",
                               (stamp, key))
            _remember_urls(db, key, (payload.get("webpage_url"), payload.get("original_url")))
            source = _stable_source(payload.get("webpage_url"))
            if source and (row is None or not row[2] or selected) and (row is None or row[2] != source):
                if row and row[3] in {"unavailable", "paused"} and not row[4]:
                    db.execute("""UPDATE arquivo_musicas SET fonte_estavel=?, estado='waiting',
                               falhas=0, motivo_falha='', tentativa_em=0 WHERE chave=?""", (source, key))
                else:
                    db.execute("UPDATE arquivo_musicas SET fonte_estavel=? WHERE chave=?", (source, key))
                _audit(db, key, "fonte_aprendida", source=source)
    return len(candidates)


def pending(*, prefer_new: bool = True) -> dict | None:
    if channel() == (0, 0):
        return None
    version = 8 if channel_type() == "forum" else 2
    with _db() as db:
        row = None
        for new in (prefer_new, not prefer_new):
            clause = ("reference_json='' AND estado NOT IN ('too_large', 'ineligible', 'unavailable', 'paused')"
                      if new else "reference_json!='' AND apresentacao<?")
            args = (time.time(),) if new else (version, time.time())
            row = db.execute("""SELECT chave, track_json, reference_json, estado,
                             fonte_arquivo, fonte_estavel, retry_reference_json
                             FROM arquivo_musicas WHERE (aprendida_em>0 OR tocadas>=1) AND """
                             + clause + " AND tentativa_em<=? ORDER BY enfileirada_em, chave LIMIT 1", args).fetchone()
            if row:
                break
    if not row:
        return None
    try:
        return {"key": row[0], "track": json.loads(row[1]),
                "reference": json.loads(row[6] or row[2]) if row[6] or row[2] else {}, "retry": row[3] in {"working", "failed"},
                "source_override": row[4], "source_known": row[5]}
    except (TypeError, ValueError):
        log.warning("[music/archive] metadados inválidos: %s", row[0])
        return None


def mark_attempt(key: str, *, source: str = "", revision: str = "") -> None:
    with _db() as db:
        db.execute("UPDATE arquivo_musicas SET estado='working', ultima_tentativa_em=? WHERE chave=?",
                   (time.time(), key))
        _audit(db, key, "tentativa", source=source, revision=revision)


def _reference_valid(ref: dict, guild_id: int, channel_id: int, kind: str) -> bool:
    try:
        ids = {field: int(ref.get(field) or 0) for field in ("guild_id", "channel_id", "message_id", "attachment_id")}
        if any(value <= 0 for value in ids.values()) or ids["guild_id"] != guild_id:
            return False
        if kind == "forum":
            if int(ref.get("forum_id") or 0) != channel_id or ids["channel_id"] == channel_id:
                return False
        elif ids["channel_id"] != channel_id:
            return False
        segments = ref.get("segments", [])
        if not isinstance(segments, list):
            return False
        for segment in segments:
            if not isinstance(segment, dict):
                return False
            reference = segment.get("reference") or segment
            if (not isinstance(reference, dict)
                    or any(int(reference.get(field) or 0) <= 0 for field in ("message_id", "attachment_id"))
                    or int(reference.get("guild_id") or 0) != guild_id
                    or int(reference.get("channel_id") or 0) != ids["channel_id"]
                    or (kind == "forum" and int(reference.get("forum_id") or 0) != channel_id)):
                return False
        return True
    except (ValueError, TypeError, OverflowError):
        return False


def mark_result(key: str, result: dict) -> None:
    status = str(result.get("status") or "failed")
    reason = str(result.get("reason") or "").strip().lower()
    reason = reason if re.fullmatch(r"[a-z0-9_]{1,48}", reason) else "erro_desconhecido"
    revision = str(result.get("agent_revision") or "")[:60]
    ref = result.get("reference") if isinstance(result.get("reference"), dict) else {}
    configured_guild, configured_channel = channel()
    kind = channel_type()
    if status == "done" and _reference_valid(ref, configured_guild, configured_channel, kind):
        with _db() as db:
            version = max(1, min(8, int(result.get("presentation") or 1)))
            refresh_at = time.time() + 3600 if kind == "forum" and version < 8 else 0
            old_row = db.execute("SELECT reference_json, track_json, fonte_arquivo FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()
            old = json.loads(old_row[0]) if old_row and old_row[0] else {}
            same_channel = bool(old and int(old.get("channel_id") or 0) == int(ref["channel_id"]))
            same_location = same_channel and (kind == "forum" or int(old.get("message_id") or 0) == int(ref["message_id"]))
            # Atualizar manifesto/segmentos na mesma thread não torna a thread
            # anterior descartável: apagá-la removeria o arquivo recém-validado.
            cleanup = old if old and not same_location and int(old.get("guild_id") or 0) == configured_guild else {}
            source = _stable_source(result.get("source_url"))
            db.execute("""UPDATE arquivo_musicas SET reference_json=?, emoji=?, estado='done',
                apresentacao=?, tentativa_em=?, falhas=0, motivo_falha='', retry_reference_json='',
                fonte_estavel=CASE WHEN ?!='' THEN ? ELSE fonte_estavel END,
                revisao_agente=? WHERE chave=?""",
                (json.dumps(ref), str(result.get("emoji") or "")[:100], version, refresh_at,
                 source, source, revision, key))
            if source:
                _remember_urls(db, key, (source,))
            _audit(db, key, "concluido", source=source, revision=revision)
            if source and old_row and _stable_source(old_row[2]) == source:
                host = (urlsplit(source).hostname or "").lower()
                if host.endswith(".bandcamp.com"):
                    try:
                        artist = _artist_key(json.loads(old_row[1]))
                    except (TypeError, ValueError):
                        artist = ""
                    if artist:
                        db.execute("""INSERT OR IGNORE INTO arquivo_artistas(artista, dominio, confirmado_em)
                                   VALUES (?, ?, ?)""", (artist, host, time.time()))
            if cleanup:
                db.execute("INSERT OR IGNORE INTO arquivo_limpezas(chave, message_id, previous_json) VALUES (?, ?, ?)",
                           (key, int(cleanup["message_id"]), json.dumps(cleanup)))
        return
    with _db() as db:
        row = db.execute("SELECT falhas, fonte_arquivo, fonte_estavel FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()
        if row is None:
            return
        if ref and _reference_valid(ref, configured_guild, configured_channel, kind):
            db.execute("UPDATE arquivo_musicas SET retry_reference_json=? WHERE chave=?",
                       (json.dumps(ref, ensure_ascii=False), key))
        failures = int(row[0]) + 1
        if status == "unavailable" and reason in {"no_match", "metadata_mismatch", "source_mismatch", "source_unavailable"}:
            state, retry_at = "unavailable", 0
        elif status in {"too_large", "ineligible"}:
            state, retry_at = status, 0
        else:
            # Falha de rede/upload nunca elimina o trabalho. O teto limita
            # pressão sobre os provedores; não limita o número de tentativas.
            state = "failed"
            retry_at = time.time() + min(_RETRY_MAX_SECONDS, 30 * 2 ** min(10, failures - 1))
        if status == "done":
            reason = "invalid_reference"
        if not result.get("reason") and state == "unavailable":
            reason = "erro_desconhecido"
        db.execute("""UPDATE arquivo_musicas SET falhas=?, estado=?, motivo_falha=?,
                   tentativa_em=?, enfileirada_em=?, revisao_agente=? WHERE chave=?""",
                   (failures, state, reason, retry_at, retry_at, revision, key))
        _audit(db, key, state, reason=reason, source=row[1] or row[2], revision=revision)
        log.info("[music/archive] resultado | chave=%s estado=%s motivo=%s tentativas=%s", key, state, reason, failures)


def reopen_on_revision(revision: str) -> int:
    """Nova versão do resolvedor pode encontrar faixas antes sem fonte."""
    revision = str(revision or "")[:60]
    if not re.fullmatch(r"[\w.:-]{3,60}", revision):
        return 0
    with _db() as db:
        rows = db.execute("""SELECT chave FROM arquivo_musicas WHERE reference_json=''
            AND (estado='paused' OR (estado='unavailable' AND motivo_falha='no_match'))
            AND revisao_agente!='' AND revisao_agente!=?""", (revision,)).fetchall()
        for (key,) in rows:
            db.execute("""UPDATE arquivo_musicas SET estado='waiting', falhas=0,
                       tentativa_em=0, motivo_falha='', revisao_agente=? WHERE chave=?""", (revision, key))
            _audit(db, key, "nova_revisao", revision=revision)
    return len(rows)


def archived(track) -> tuple[dict, str, str]:
    key = media_key(track)
    if not key:
        return {}, "", ""
    with _db() as db:
        row = db.execute("""SELECT chave, reference_json, emoji, track_json, fonte_estavel FROM arquivo_musicas
            WHERE chave=COALESCE((SELECT chave FROM arquivo_aliases WHERE alias=?), ?)""", (key, key)).fetchone()
        configured = db.execute("SELECT guild_id, channel_id, channel_type FROM arquivo_config WHERE id=1").fetchone()
    if not row or not row[0] or configured is None:
        return {}, "", ""
    try:
        ref = json.loads(row[1])
        if int(ref["guild_id"]) != configured[0]:
            return {}, "", ""
        if configured[2] != "forum" and int(ref["channel_id"]) != configured[1]:
            return {}, "", ""
        payload = json.loads(row[3])
        known = {_public_identity(payload.get("webpage_url")),
                 _public_identity(payload.get("original_url")), _public_identity(row[4])}
        requested = {_public_identity(getattr(track, "webpage_url", "")),
                     _public_identity(getattr(track, "original_url", ""))}
        if not (known - {None}) & (requested - {None}):
            return {}, "", ""
        return ref, row[2], row[0]
    except (ValueError, TypeError, KeyError):
        return {}, "", ""


def _public_identity(url: str) -> tuple | None:
    """Confirma a URL exata antes do atalho, inclusive IDs sensíveis a caixa.

    As chaves legadas ignoravam algumas queries. Esta checagem impede que
    Apple Music (?i=faixa) ou uma fonte genérica herdem o áudio de outra faixa.
    """
    try:
        parsed = urlsplit(str(url or "").strip())
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password or parsed.port is not None:
            return None
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)
        if host in {"youtube.com", "m.youtube.com", "music.youtube.com"} and path == "/watch":
            return "youtube", query.get("v", [""])[0]
        if host == "youtu.be":
            return "youtube", path.lstrip("/")
        if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
            short = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})", path)
            if short:
                return "youtube", short.group(1)
        if host == "open.spotify.com":
            path = re.sub(r"^/intl-[^/]+/", "/", path)
            return host, path
        if host in {"soundcloud.com", "deezer.com"} or host.endswith(".bandcamp.com"):
            return host, path
        if host == "music.apple.com":
            return host, path, query.get("i", [""])[0]
        meaningful = tuple(sorted((name, tuple(values)) for name, values in query.items()
                                  if not name.lower().startswith("utm_") and name.lower() not in {"fbclid", "gclid"}))
        return host, path, meaningful
    except (ValueError, TypeError):
        return None


def archived_track_for_url(url: str, *, requester_id: int = 0, requester_name: str = "", guild_id: int = 0):
    """Resolve um link conhecido pelo índice do fórum, sem consultar provedores."""
    from .memoria import _track_from_payload
    aliases = _url_aliases(url)
    if not aliases:
        return None
    with _db() as db:
        row = None
        for alias in aliases:
            row = db.execute("""SELECT chave, track_json, reference_json, emoji, fonte_estavel FROM arquivo_musicas
                WHERE chave=COALESCE((SELECT chave FROM arquivo_aliases WHERE alias=?), ?)
                AND reference_json!=''""", (alias, alias)).fetchone()
            if row is not None:
                break
        configured = db.execute("SELECT guild_id, channel_id, channel_type FROM arquivo_config WHERE id=1").fetchone()
    if row is None or configured is None or (guild_id and int(configured[0]) != int(guild_id)):
        return None
    try:
        payload, ref = json.loads(row[1]), json.loads(row[2])
        if not isinstance(payload, dict) or not isinstance(ref, dict) or int(ref.get("guild_id") or 0) != int(configured[0]):
            return None
        requested = _public_identity(url)
        if requested is None or requested not in {
            _public_identity(payload.get("webpage_url")),
            _public_identity(payload.get("original_url")),
            _public_identity(row[4]),
        }:
            return None
        # Referências do fórum anterior no mesmo guild continuam tocáveis
        # durante uma migração; o runtime valida o servidor novamente.
        if configured[2] != "forum" and int(ref.get("channel_id") or 0) != int(configured[1]):
            return None
        track = _track_from_payload(payload)
        track.requester_id = int(requester_id or 0)
        track.requester_name = str(requester_name or "")
        track.archive_ref = ref
        track.source_emoji = str(row[3] or "")
        return track
    except (TypeError, ValueError, KeyError, OverflowError):
        return None


def counts() -> dict[str, int]:
    version = 8 if channel_type() == "forum" else 2
    with _db() as db:
        rows = db.execute("SELECT estado, COUNT(*) FROM arquivo_musicas GROUP BY estado").fetchall()
        learned = db.execute("SELECT COUNT(*) FROM arquivo_musicas WHERE aprendida_em>0 OR tocadas>0").fetchone()[0]
        unposted = db.execute("SELECT COUNT(*) FROM arquivo_musicas WHERE (aprendida_em>0 OR tocadas>0) "
                              "AND reference_json='' AND estado NOT IN ('too_large', 'ineligible', 'unavailable', 'paused')").fetchone()[0]
        one_play = db.execute("SELECT COUNT(*) FROM arquivo_musicas WHERE tocadas=1").fetchone()[0]
        refresh = db.execute("SELECT COUNT(*) FROM arquivo_musicas WHERE reference_json!='' AND apresentacao<?",
                             (version,)).fetchone()[0]
        cleanup = db.execute("SELECT COUNT(*) FROM arquivo_limpezas").fetchone()[0]
    from .memoria import _abrir_db
    with _abrir_db() as memory_db:
        choices = memory_db.execute("SELECT COUNT(*) FROM escolhas").fetchone()[0]
    return {**dict(rows), "learned": int(learned), "choices": int(choices), "unposted": int(unposted), "one_play": int(one_play),
            "refresh": int(refresh), "cleanup": int(cleanup)}


def failures(limit: int = 8, offset: int = 0, *, after: tuple[float, str] | None = None) -> list[dict]:
    """Resumo do dono; cursor dispensa OFFSET crescente em acervos grandes."""
    clause = ""
    args: list = []
    if after is not None:
        clause = " AND (ultima_tentativa_em<? OR (ultima_tentativa_em=? AND chave>?))"
        args.extend((after[0], after[0], after[1]))
    args.extend((max(1, min(20, limit)), max(0, int(offset))))
    with _db() as db:
        rows = db.execute("""SELECT chave, track_json, estado, motivo_falha, falhas,
            tentativa_em, ultima_tentativa_em, fonte_arquivo, fonte_estavel
            FROM arquivo_musicas WHERE reference_json='' AND estado IN ('failed', 'paused', 'unavailable', 'too_large', 'ineligible')"""
            + clause + " ORDER BY ultima_tentativa_em DESC, chave LIMIT ? OFFSET ?", args).fetchall()
    result = []
    for key, raw, state, reason, attempts, retry_at, last_at, override, known in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {}
        result.append({"key": key, "title": str(payload.get("display_title") or payload.get("title") or "Música")[:140],
                       "state": state, "reason": reason or "sem_diagnostico", "attempts": attempts,
                       "retry_at": retry_at if state == "failed" else 0, "last_at": last_at,
                       "source": _stable_source(override or known)})
    return result


def failure_cursor(entry: dict) -> str:
    return f"{float(entry['last_at']).hex()}:{entry['key']}"


def parse_failure_cursor(cursor: str) -> tuple[float, str]:
    stamp, key = str(cursor).rsplit(":", 1)
    number = float.fromhex(stamp)
    if not math.isfinite(number) or number < 0 or not re.fullmatch(r"[a-f0-9]{32}", key):
        raise ValueError("cursor de falhas inválido")
    return number, key


def history(key: str, limit: int = 8, offset: int = 0, *, before_id: int = 0) -> list[dict]:
    key = str(key or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", key):
        return []
    with _db() as db:
        rows = db.execute("""SELECT ocorrido_em, evento, motivo, fonte, revisao, id
            FROM arquivo_eventos WHERE chave=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT ? OFFSET ?""",
            (key, max(0, int(before_id)), max(0, int(before_id)), max(1, min(20, limit)), max(0, int(offset)))).fetchall()
    return [{"at": at, "event": event, "reason": reason, "source": source, "revision": revision, "id": event_id}
            for at, event, reason, source, revision, event_id in rows]


def cleanup_pending() -> dict | None:
    with _db() as db:
        row = db.execute("""SELECT l.chave, m.reference_json, l.previous_json, m.track_json FROM arquivo_limpezas l
            JOIN arquivo_musicas m ON m.chave=l.chave
            WHERE l.tentativa_em<=? ORDER BY l.tentativa_em, l.chave LIMIT 1""",
            (time.time(),)).fetchone()
    if not row:
        return None
    try:
        return {"key": row[0], "reference": json.loads(row[1]), "previous": json.loads(row[2]),
                "track": json.loads(row[3])}
    except (TypeError, ValueError):
        return None


def mark_cleanup(key: str, previous: dict, *, done: bool) -> None:
    with _db() as db:
        if done:
            db.execute("DELETE FROM arquivo_limpezas WHERE chave=? AND message_id=? AND previous_json=?",
                       (key, int(previous["message_id"]), json.dumps(previous)))
        else:
            db.execute("UPDATE arquivo_limpezas SET tentativa_em=? WHERE chave=? AND message_id=? AND previous_json=?",
                       (time.time() + 300, key, int(previous["message_id"]), json.dumps(previous)))
