"""Índice leve do arquivo de músicas; os bytes vivem apenas no Discord."""
from __future__ import annotations

import hashlib
import json
import logging
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
_MAX_TRANSIENT_FAILURES = 5


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
    cursor = db.execute("INSERT INTO arquivo_eventos(chave, ocorrido_em, evento, motivo, fonte, revisao) VALUES (?, ?, ?, ?, ?, ?)",
                        (key, time.time(), event[:30], reason[:48], _stable_source(source), revision[:60]))
    if cursor.lastrowid % 128 == 0:
        db.execute("DELETE FROM arquivo_eventos WHERE id <= (SELECT COALESCE(MAX(id), 0)-2000 FROM arquivo_eventos)")


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


def media_key(track) -> str:
    if getattr(track, "is_live", False) or (getattr(track, "attachment_ref", None) and not getattr(track, "archive_ref", None)):
        return ""
    original = str(getattr(track, "original_url", "") or "")
    if any(provider in original.lower() for provider in ("open.spotify.com/track/", "deezer.com/track/", "music.apple.com/")):
        return _url_key(original)
    return _url_key(getattr(track, "webpage_url", "")) or _url_key(original)


def _db() -> sqlite3.Connection:
    # O comando que limpa escolhas não pode apagar o canal configurado nem os
    # IDs dos anexos já enviados. O sufixo preserva o isolamento de worktrees.
    memory_path: Path = _db_path()
    path = memory_path.with_name(memory_path.stem + "-arquivo.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=2)
    db.execute("PRAGMA busy_timeout=2000")
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
    return db


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
    if not key or not 0 < duration <= 600 or len(marker) > 128:
        return False
    with _db() as db:
        if not db.execute("INSERT OR IGNORE INTO arquivo_reproducoes VALUES (?, ?)", (marker, time.time())).rowcount:
            return False
        previous = db.execute("SELECT fonte_estavel, estado, reference_json FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()
        db.execute("""INSERT INTO arquivo_musicas(chave, track_json, tocadas) VALUES (?, ?, 1)
            ON CONFLICT(chave) DO UPDATE SET tocadas=MIN(2, tocadas+1), track_json=excluded.track_json""",
            (key, json.dumps(_track_payload(track), ensure_ascii=False),))
        source = _stable_source(getattr(track, "webpage_url", ""))
        if source and (previous is None or previous[0] != source):
            if previous and previous[1] in {"unavailable", "paused"} and not previous[2]:
                db.execute("""UPDATE arquivo_musicas SET fonte_estavel=?, estado='waiting',
                           falhas=0, motivo_falha='', tentativa_em=0 WHERE chave=?""", (source, key))
            else:
                db.execute("UPDATE arquivo_musicas SET fonte_estavel=? WHERE chave=?", (source, key))
            _audit(db, key, "fonte_observada", source=source)
        for url in (getattr(track, "webpage_url", ""), getattr(track, "original_url", "")):
            alias = _url_key(url)
            if alias:
                db.execute("INSERT OR REPLACE INTO arquivo_aliases VALUES (?, ?)", (alias, key))
        db.execute("DELETE FROM arquivo_reproducoes WHERE registrado_em < ?", (time.time() - 90 * 86400,))
    return True


def note_learned(tracks) -> None:
    """A escolha já foi persistida; avise o coordenador sem I/O no play."""
    with _learned_lock:
        for track in tracks:
            key = media_key(track)
            if key:
                _new_learned[key] = (_track_payload(track), time.time())
        # A varredura da memória persistida recupera qualquer item descartado.
        while len(_new_learned) > 2048:
            _new_learned.pop(next(iter(_new_learned)))


def flush_learned() -> int:
    with _learned_lock:
        items = list(_new_learned.items())
        _new_learned.clear()
    try:
        return register_learned_batch([(payload, stamp) for _key, (payload, stamp) in items])
    except Exception:
        with _learned_lock:
            for key, value in items:
                _new_learned.setdefault(key, value)
        raise


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
        if track.is_live or duration > 600 or duration < 0:
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
                db.execute("INSERT INTO arquivo_musicas(chave, track_json, aprendida_em) VALUES (?, ?, ?)",
                           (key, encoded, stamp))
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
    version = 7 if channel_type() == "forum" else 2
    with _db() as db:
        row = db.execute("""SELECT chave, track_json, reference_json, estado, fonte_arquivo, fonte_estavel FROM arquivo_musicas
            WHERE (aprendida_em>0 OR tocadas>=1)
            AND ((reference_json='' AND estado NOT IN ('too_large', 'ineligible', 'unavailable', 'paused'))
                OR (reference_json!='' AND apresentacao<?)) AND tentativa_em<=?
            ORDER BY CASE WHEN (reference_json='')=? THEN 0 ELSE 1 END,
                     tentativa_em, aprendida_em DESC, chave LIMIT 1""",
                         (version, time.time(), int(prefer_new))).fetchone()
    if not row:
        return None
    try:
        return {"key": row[0], "track": json.loads(row[1]),
                "reference": json.loads(row[2]) if row[2] else {}, "retry": row[3] in {"working", "failed"},
                "source_override": row[4], "source_known": row[5]}
    except (TypeError, ValueError):
        log.warning("[music/archive] metadados inválidos: %s", row[0])
        return None


def mark_attempt(key: str, *, source: str = "", revision: str = "") -> None:
    with _db() as db:
        db.execute("UPDATE arquivo_musicas SET estado='working', ultima_tentativa_em=? WHERE chave=?", (time.time(), key))
        _audit(db, key, "tentativa", source=source, revision=revision)


def mark_result(key: str, result: dict) -> None:
    status = str(result.get("status") or "failed")
    reason = str(result.get("reason") or "").strip().lower()
    reason = reason if re.fullmatch(r"[a-z0-9_]{1,48}", reason) else "erro_desconhecido"
    revision = str(result.get("agent_revision") or "")[:60]
    ref = result.get("reference") if isinstance(result.get("reference"), dict) else {}
    configured_guild, configured_channel = channel()
    kind = channel_type()
    same_target = ((int(ref.get("forum_id") or 0) == configured_channel
                    and int(ref.get("channel_id") or 0) != configured_channel
                    and int(result.get("presentation") or 0) >= 3) if kind == "forum"
                   else int(ref.get("channel_id") or 0) == configured_channel)
    if (status == "done" and all(int(ref.get(field) or 0) > 0 for field in ("guild_id", "channel_id", "message_id", "attachment_id"))
            and int(ref["guild_id"]) == configured_guild and same_target):
        with _db() as db:
            version = max(1, min(7, int(result.get("presentation") or 1)))
            refresh_at = time.time() + 3600 if kind == "forum" and version < 7 else 0
            old_row = db.execute("SELECT reference_json, track_json, fonte_arquivo FROM arquivo_musicas WHERE chave=?", (key,)).fetchone()
            old = json.loads(old_row[0]) if old_row and old_row[0] else {}
            cleanup = old if old and old != ref and int(old.get("guild_id") or 0) == configured_guild else {}
            source = _stable_source(result.get("source_url"))
            db.execute("""UPDATE arquivo_musicas SET reference_json=?, emoji=?, estado='done',
                apresentacao=?, tentativa_em=?, falhas=0, motivo_falha='',
                fonte_estavel=CASE WHEN ?!='' THEN ? ELSE fonte_estavel END,
                revisao_agente=? WHERE chave=?""",
                (json.dumps(ref), str(result.get("emoji") or "")[:100], version, refresh_at,
                 source, source, revision, key))
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
        failures = int(row[0]) + 1
        if status == "unavailable" and reason in {"no_match", "metadata_mismatch", "source_mismatch", "source_unavailable"}:
            state, retry_at = "unavailable", 0
        elif status in {"too_large", "ineligible"}:
            state, retry_at = status, 0
        elif failures >= _MAX_TRANSIENT_FAILURES:
            state, retry_at = "paused", 0
        else:
            state = "failed"
            retry_at = time.time() + min(1800, 30 * 2 ** min(6, failures - 1))
        if status == "done":
            reason = "invalid_reference"
        if not result.get("reason") and state == "unavailable":
            reason = "erro_desconhecido"
        db.execute("""UPDATE arquivo_musicas SET falhas=?, estado=?, motivo_falha=?,
                   tentativa_em=?, revisao_agente=? WHERE chave=?""",
                   (failures, state, reason, retry_at, revision, key))
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
        row = db.execute("""SELECT chave, reference_json, emoji FROM arquivo_musicas
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
        return ref, row[2], row[0]
    except (ValueError, TypeError, KeyError):
        return {}, "", ""


def counts() -> dict[str, int]:
    version = 7 if channel_type() == "forum" else 2
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


def failures(limit: int = 8, offset: int = 0) -> list[dict]:
    """Resumo leve para o dono, sem URLs temporárias nem logs brutos."""
    with _db() as db:
        rows = db.execute("""SELECT chave, track_json, estado, motivo_falha, falhas,
            tentativa_em, ultima_tentativa_em, fonte_arquivo, fonte_estavel
            FROM arquivo_musicas WHERE reference_json='' AND estado IN ('failed', 'paused', 'unavailable', 'too_large', 'ineligible')
            ORDER BY ultima_tentativa_em DESC, chave LIMIT ? OFFSET ?""",
            (max(1, min(20, limit)), max(0, min(1000, offset)))).fetchall()
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


def history(key: str, limit: int = 8, offset: int = 0) -> list[dict]:
    key = str(key or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", key):
        return []
    with _db() as db:
        rows = db.execute("""SELECT ocorrido_em, evento, motivo, fonte, revisao
            FROM arquivo_eventos WHERE chave=? ORDER BY id DESC LIMIT ? OFFSET ?""",
            (key, max(1, min(20, limit)), max(0, min(1000, offset)))).fetchall()
    return [{"at": at, "event": event, "reason": reason, "source": source, "revision": revision}
            for at, event, reason, source, revision in rows]


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
