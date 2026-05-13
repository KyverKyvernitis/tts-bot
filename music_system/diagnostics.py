from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import io
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import traceback
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

import config


SENSITIVE_PATTERNS = (
    r"(?i)(password\s*[:=]\s*)[^\s\"']+",
    r"(?i)(token\s*[:=]\s*)[^\s\"']+",
    r"(?i)(secret\s*[:=]\s*)[^\s\"']+",
    r"(?i)(authorization\s*[:=]\s*)[^\s\"']+",
    r"(?i)(clientSecret\s*[:=]\s*)[^\s\"']+",
    r"(?i)(refreshToken\s*[:=]\s*)[^\s\"']+",
    r"(?i)(arl\s*[:=]\s*)[^\s\"']+",
    r"(?i)(DISCORD_TOKEN=).*",
    r"(?i)(MONGODB_URI=).*",
    r"(?i)(MONGO_URI=).*",
    r"(?i)(WEBHOOK[^=]*=).*",
    r"(?i)([A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|COOKIE|CREDENTIAL|WEBHOOK|MONGODB_URI|MONGO_URI|ARL)[A-Z0-9_]*=).*",
)

REPO_ROOT = Path(getattr(config, "BASE_DIR", Path(__file__).resolve().parents[1])).resolve()
DEFAULT_MUSICNODE_DB = REPO_ROOT / "data" / "musicnode" / "musicnode.db"

VALID_SPOTIFY_TEST_URL = "https://open.spotify.com/track/3BxXcWY0ZYkNBhiOvy6vWr?si=I69KMQsjTB2g4La8tLAOCw"
VALID_SPOTIFY_TEST_ID = "3BxXcWY0ZYkNBhiOvy6vWr"
VALID_SOUNDCLOUD_TEST_URL = (
    "https://soundcloud.com/yosoyharmless/untitled-1"
    "?si=f0620a0357c74d5badaa77670b9094a5"
    "&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing"
)
VALID_YOUTUBE_TEST_URL = "https://www.youtube.com/watch?v=qU9mHegkTc4"



@dataclass(slots=True)
class DiagnosticsOptions:
    guild_id: int
    guild_name: str
    requester_id: int
    requester_name: str
    include_journalctl: bool = True
    include_local_logs: bool = True


def redact(text: object) -> str:
    out = str(text if text is not None else "")
    for pattern in SENSITIVE_PATTERNS:
        out = re.sub(pattern, lambda m: f"{m.group(1)}***REDACTED***" if m.groups() else "***REDACTED***", out)
    # Máscara tokens comuns do Discord/webhook e valores longos que parecem secrets.
    out = re.sub(r"https://discord(?:app)?\.com/api/webhooks/[\w\-/]+", "https://discord.com/api/webhooks/***REDACTED***", out, flags=re.I)
    out = re.sub(r"\b[A-Za-z0-9_\-]{24}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{20,}\b", "***DISCORD_TOKEN_REDACTED***", out)
    return out


def _mask_value(key: str, value: Any) -> Any:
    lowered = str(key or "").lower()
    if any(marker in lowered for marker in ("pass", "token", "secret", "cookie", "credential", "webhook", "mongo", "arl", "key")):
        return "***REDACTED***" if value not in (None, "") else ""
    return value


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")




def cleanup_music_diagnostics_temp_artifacts(*, max_age_seconds: float = 12 * 3600) -> str:
    """Remove apenas artefatos temporários de diagnóstico, nunca logs reais.

    O diagnóstico musical deve continuar completo, mas não precisa manter zips/txt
    antigos em pastas temporárias. A função é conservadora e apaga só nomes que
    seguem o padrão do próprio diagnóstico.
    """
    now = time.time()
    roots = [
        Path("/tmp"),
        REPO_ROOT / "data" / "diagnostics" / "tmp",
        REPO_ROOT / "data" / "diagnostics",
    ]
    patterns = (
        "vps-music-diagnostics-*.zip",
        "vps-music-diagnostics-summary-*.txt",
        "vps-music-diagnostics-*.txt",
        "vps-full-diagnostics-*.txt",
    )
    removed = 0
    checked = 0
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            with contextlib.suppress(Exception):
                for path in root.glob(pattern):
                    checked += 1
                    if not path.is_file():
                        continue
                    try:
                        age = now - path.stat().st_mtime
                    except Exception:
                        continue
                    if age >= max_age_seconds:
                        with contextlib.suppress(Exception):
                            path.unlink()
                            removed += 1
    return f"diagnostic-temp-cleanup: checked={checked} removed={removed} max_age_seconds={int(max_age_seconds)}"

def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return "não encontrado"


def _run_cmd(args: list[str], *, timeout: float = 12.0, cwd: Path | None = None) -> str:
    try:
        cp = subprocess.run(
            args,
            cwd=str(cwd or REPO_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        body = cp.stdout or ""
        return f"$ {' '.join(args)}\nexit={cp.returncode}\n{body}"
    except subprocess.TimeoutExpired as exc:
        body = exc.stdout or ""
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return f"$ {' '.join(args)}\nTIMEOUT após {timeout}s\n{body}"
    except Exception as exc:
        return f"$ {' '.join(args)}\nERRO: {type(exc).__name__}: {exc}"


def _read_env_flags() -> dict[str, Any]:
    names = [
        "LAVALINK_ENABLED",
        "LAVALINK_MODE",
        "MUSIC_NODE_PROVIDER",
        "AUDIO_NODE_FAILURE_COOLDOWN_SECONDS",
        "AUDIO_NODE_STARTUP_WAIT_SECONDS",
        "NODELINK_ENABLED",
        "NODELINK_HOST",
        "NODELINK_PORT",
        "NODELINK_PASSWORD",
        "NODELINK_SECURE",
        "NODELINK_NODE_NAME",
        "LAVALINK_HOST",
        "LAVALINK_PORT",
        "WAVELINK_HOST",
        "WAVELINK_PORT",
        "MUSIC_LAVASRC_MIRROR_PREFIXES",
        "MUSIC_LAVALINK_ENABLE_ALL_GUILDS",
        "YTDLP_COOKIES_FILE",
        "MUSIC_YTDLP_COOKIES_FILE",
        "YT_DLP_COOKIES_FILE",
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_ID",
        "LAVASRC_SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_SECRET",
        "LAVASRC_SPOTIFY_CLIENT_SECRET",
        "DEEZER_API_ENABLED",
        "LAVASRC_DEEZER_ARL",
        "LAVASRC_DEEZER_MASTER_DECRYPTION_KEY",
        "TTS_VOICE_AUTO_RESTORE_ENABLED",
        "MUSIC_TTS_PUBLIC_BASE_URL",
        "MUSIC_TTS_INTERNAL_BASE_URL",
        "MUSIC_LAVALINK_TTS_INTERNAL_FIRST",
        "MUSIC_LAVALINK_TTS_URL_PROBE_TIMEOUT_SECONDS",
        "MUSIC_TTS_AUDIO_FORMAT",
        "MUSIC_TTS_AUDIO_FALLBACK_FORMAT",
        "MUSIC_TTS_OPUS_BITRATE",
        "MUSIC_TTS_OPUS_SAMPLE_RATE",
        "MUSIC_TTS_OPUS_CHANNELS",
        "MUSIC_TTS_CONVERT_TIMEOUT_SECONDS",
        "MUSIC_TTS_PREROLL_SILENCE_MS",
        "MUSIC_TTS_POSTROLL_SILENCE_MS",
        "MUSIC_TTS_FADE_IN_MS",
        "MUSIC_TTS_FADE_OUT_MS",
        "MUSIC_TTS_RESUME_SEEK_AHEAD_MS",
        "MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED",
        "MUSIC_TTS_LAVALINK_VOLUME_RAMP_MS",
        "MUSIC_TTS_LAVALINK_RAMP_FLOOR_PERCENT",
    ]
    result: dict[str, Any] = {}
    for name in names:
        value = os.getenv(name)
        if value is None:
            result[name] = "não definido"
            continue
        if any(marker in name.lower() for marker in ("secret", "token", "password", "cookie", "arl")):
            result[name] = f"definido ({len(value)} caracteres)"
        elif name.endswith("_ID") or name in {"SPOTIFY_ID", "LAVASRC_SPOTIFY_CLIENT_ID"}:
            result[name] = f"definido ({len(value)} caracteres)" if value else "vazio"
        else:
            result[name] = value
    return result


def _db_snapshot(db_path: Path = DEFAULT_MUSICNODE_DB) -> tuple[str, dict[str, Any]]:
    data: dict[str, Any] = {"path": str(db_path), "exists": db_path.exists()}
    if not db_path.exists():
        return "DB do musicnode não encontrado.", data
    try:
        con = sqlite3.connect(str(db_path), timeout=8.0)
        con.row_factory = sqlite3.Row
        try:
            tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            data["tables"] = tables
            rows_by_table: dict[str, list[dict[str, Any]]] = {}
            for table in tables:
                if table not in {"lavalink_nodes", "lavalink_guild_settings", "lavalink_meta"}:
                    continue
                rows: list[dict[str, Any]] = []
                for row in con.execute(f"SELECT * FROM {table} LIMIT 30"):
                    item = {key: _mask_value(key, row[key]) for key in row.keys()}
                    rows.append(item)
                rows_by_table[table] = rows
            data["rows"] = rows_by_table
            return json.dumps(data, ensure_ascii=False, indent=2), data
        finally:
            con.close()
    except Exception as exc:
        data["error"] = f"{type(exc).__name__}: {exc}"
        return json.dumps(data, ensure_ascii=False, indent=2), data


def _lavalink_cfg_from_router(router: Any, guild_id: int) -> dict[str, Any]:
    cfg_data: dict[str, Any] = {}
    try:
        backends = getattr(router, "backends", None)
        store = getattr(backends, "lavalink_store", None)
        if backends is not None and hasattr(backends, "_node_config_for_guild"):
            cfg = backends._node_config_for_guild(guild_id)  # diagnóstico interno: mostra o node efetivo.
        elif store is not None:
            cfg = store.load(guild_id=guild_id)
        else:
            cfg = None
        if cfg is not None:
            cfg_data = {
                "provider": str(getattr(cfg, "provider", "lavalink") or "lavalink"),
                "provider_label": str(getattr(cfg, "provider_label", "Lavalink") or "Lavalink"),
                "enabled": bool(getattr(cfg, "enabled", False)),
                "configured": bool(getattr(cfg, "configured", False)),
                "mode": str(getattr(cfg, "mode", "") or ""),
                "node_name": str(getattr(cfg, "node_name", "") or ""),
                "host": str(getattr(cfg, "host", "") or ""),
                "port": int(getattr(cfg, "port", 0) or 0),
                "secure": bool(getattr(cfg, "secure", False)),
                "password": "***REDACTED***" if getattr(cfg, "password", "") else "",
                "password_defined": bool(getattr(cfg, "password", "")),
                "timeout_seconds": float(getattr(cfg, "timeout_seconds", 0) or 0),
                "base_url": str(getattr(cfg, "base_url", "") or ""),
                "raw_password": str(getattr(cfg, "password", "") or ""),
            }
            with contextlib.suppress(Exception):
                cfg_data["summary"] = store.summary(guild_id=guild_id) if store is not None else {}
            with contextlib.suppress(Exception):
                runtime = backends.compact_runtime_summary(guild_id=guild_id) if backends is not None else {}
                cfg_data["runtime"] = runtime
    except Exception as exc:
        cfg_data = {"error": f"{type(exc).__name__}: {exc}"}
    return cfg_data


def _http_json(url: str, *, password: str = "", timeout: float = 18.0) -> dict[str, Any]:
    headers = {}
    if password:
        headers["Authorization"] = password
    req = urllib.request.Request(url, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(500_000).decode("utf-8", "replace")
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            try:
                data = json.loads(body)
            except Exception:
                data = body[:1000]
            return {"ok": True, "status": resp.status, "latency_ms": elapsed, "data": data}
    except urllib.error.HTTPError as exc:
        body = exc.read(80_000).decode("utf-8", "replace")
        return {"ok": False, "status": exc.code, "error": body[:1500]}
    except Exception as exc:
        return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}


def _summarize_loadtracks(data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("ok"):
        return data
    payload = data.get("data")
    if not isinstance(payload, dict):
        return {**data, "summary": "resposta não JSON/dict"}
    load_type = payload.get("loadType")
    raw_tracks = payload.get("data") or []
    tracks: list[Any]
    if isinstance(raw_tracks, dict) and "tracks" in raw_tracks:
        tracks = raw_tracks.get("tracks") or []
    elif isinstance(raw_tracks, dict):
        tracks = [raw_tracks]
    elif isinstance(raw_tracks, list):
        tracks = raw_tracks
    else:
        tracks = []
    brief_tracks = []
    for track in tracks[:5]:
        if not isinstance(track, dict):
            continue
        info = track.get("info") if isinstance(track.get("info"), dict) else {}
        brief_tracks.append({
            "sourceName": info.get("sourceName"),
            "title": info.get("title"),
            "author": info.get("author"),
            "length": info.get("length"),
            "uri": info.get("uri"),
        })
    out = {k: v for k, v in data.items() if k != "data"}
    out["loadType"] = load_type
    out["tracks_found"] = len(tracks)
    out["tracks"] = brief_tracks
    if load_type == "error":
        out["error_data"] = raw_tracks
    return out


def _application_yml_text() -> str:
    path = Path("/opt/lavalink/application.yml")
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _application_lavasrc_analysis() -> dict[str, Any]:
    text = _application_yml_text()
    data: dict[str, Any] = {
        "path": "/opt/lavalink/application.yml",
        "exists": bool(text),
        "providers": [],
        "sources": {},
        "warnings": [],
    }
    if not text:
        data["warnings"].append("application.yml não encontrado ou inacessível.")
        return data

    lines = text.splitlines()
    in_providers = False
    in_sources = False
    for line in lines:
        stripped = line.strip()
        if stripped == "providers:":
            in_providers = True
            in_sources = False
            continue
        if stripped == "sources:":
            in_sources = True
            in_providers = False
            continue
        if in_providers:
            if re.match(r"^\s+-\s+", line):
                data["providers"].append(stripped.lstrip("- ").strip().strip('"'))
                continue
            if stripped and not line.startswith("      "):
                in_providers = False
        if in_sources:
            m = re.match(r"^\s+([A-Za-z0-9_-]+):\s*(true|false)\s*$", line)
            if m:
                data["sources"][m.group(1)] = m.group(2).lower() == "true"
                continue
            if stripped and not line.startswith("      "):
                in_sources = False

    deezer_enabled = bool(data["sources"].get("deezer"))
    has_deezer_master = bool(re.search(r"(?m)^\s*masterDecryptionKey:\s*\S+", text))
    has_deezer_arl = bool(re.search(r"(?m)^\s*arl:\s*\S+", text))
    has_dz_provider = any(str(item).startswith(("dzsearch:", "dzisrc:")) for item in data["providers"])
    data["deezer_master_key_defined"] = has_deezer_master
    data["deezer_arl_defined"] = has_deezer_arl
    if deezer_enabled and not has_deezer_master:
        data["warnings"].append("Deezer está ligado no LavaSrc, mas masterDecryptionKey não foi configurada. Isso derruba o Lavalink com 'Deezer master key must be set'.")
    if has_dz_provider and not deezer_enabled:
        data["warnings"].append("Provider dzsearch/dzisrc está configurado, mas sources.deezer está false; dzsearch tende a falhar/ficar inútil.")
    if has_dz_provider and deezer_enabled and not has_deezer_master:
        data["warnings"].append("Remova dzsearch ou configure Deezer corretamente antes de usar esse provider.")
    return data


def _mirror_prefixes_for_diagnostics() -> tuple[list[str], list[str]]:
    raw = os.getenv("MUSIC_LAVASRC_MIRROR_PREFIXES", "scsearch") or "scsearch"
    prefixes: list[str] = []
    notes: list[str] = []
    analysis = _application_lavasrc_analysis()
    deezer_ok = bool(analysis.get("sources", {}).get("deezer")) and bool(analysis.get("deezer_master_key_defined"))
    for item in re.split(r"[,;\s]+", raw.strip()):
        prefix = item.strip().lower().removesuffix(":")
        if not prefix:
            continue
        if prefix.startswith("dz") and not deezer_ok:
            notes.append(f"{prefix} ignorado no diagnóstico porque Deezer não está totalmente configurado no LavaSrc.")
            continue
        if prefix not in {"scsearch", "spsearch", "dzsearch", "amsearch"}:
            notes.append(f"{prefix} ignorado: prefixo desconhecido para mirror LavaSrc.")
            continue
        if prefix not in prefixes:
            prefixes.append(prefix)
    if not prefixes:
        prefixes = ["scsearch"]
        notes.append("Mirror efetivo do diagnóstico caiu para scsearch.")
    return prefixes, notes



def _spotify_api_fetch_test_track() -> tuple[dict[str, Any] | None, list[str]]:
    """Busca a metadata do track Spotify de teste.

    Retorna também linhas de diagnóstico para que o dry-run consiga explicar
    quando caiu para metadata estática. O dry-run não toca áudio nem entra em call.
    """
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "") or os.getenv("SPOTIFY_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "") or os.getenv("SPOTIFY_SECRET", "")
    lines: list[str] = []
    if not client_id or not client_secret:
        lines.append("Spotify API não consultada no dry-run: credenciais ausentes.")
        return None, lines
    import base64
    try:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=body,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=18) as resp:
            token_payload = json.loads(resp.read().decode("utf-8", "replace"))
        token = token_payload.get("access_token") or ""
        if not token:
            lines.append("Spotify API retornou token vazio no dry-run.")
            return None, lines
        req = urllib.request.Request(
            f"https://api.spotify.com/v1/tracks/{VALID_SPOTIFY_TEST_ID}?market=BR",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=18) as resp:
            track = json.loads(resp.read().decode("utf-8", "replace"))
        artists = [a.get("name", "") for a in track.get("artists", []) if isinstance(a, dict) and a.get("name")]
        data = {
            "ok": True,
            "metadata_source": "Spotify API",
            "id": track.get("id") or VALID_SPOTIFY_TEST_ID,
            "title": track.get("name") or "Castle Vein",
            "artists": ", ".join(artists) or "Heaven Pierce Her",
            "primary_artist": artists[0] if artists else "Heaven Pierce Her",
            "duration_ms": track.get("duration_ms") or 340480,
            "url": VALID_SPOTIFY_TEST_URL,
        }
        return data, lines
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace")
        lines.append(f"Spotify API indisponível no dry-run: HTTP {exc.code} {redact(body[:600])}")
    except Exception as exc:
        lines.append(f"Spotify API indisponível no dry-run: {type(exc).__name__}: {exc}")
    return None, lines


def _spotify_static_test_track_metadata() -> dict[str, Any]:
    return {
        "ok": False,
        "metadata_source": "fallback estático do diagnóstico",
        "id": VALID_SPOTIFY_TEST_ID,
        "title": "Castle Vein",
        "artists": "Heaven Pierce Her",
        "primary_artist": "Heaven Pierce Her",
        "duration_ms": 340480,
        "url": VALID_SPOTIFY_TEST_URL,
    }


def _spotify_dry_run_mirror_test(cfg: dict[str, Any]) -> str:
    """Simula Spotify -> mirror LavaSrc -> decisão de fallback sem tocar áudio.

    Esse teste existe para validar a query gerada pelo bot e a decisão que seria
    tomada pelo fluxo real, mas sem entrar em call, mexer em fila ou trocar música.
    """
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    password = str(cfg.get("raw_password") or "")
    provider_label = str(cfg.get("provider_label") or "Lavalink")
    lines: list[str] = [
        "Este teste NÃO entra em call e NÃO toca áudio; ele só simula a resolução Spotify -> mirror/fallback.",
        f"Link Spotify de teste: {VALID_SPOTIFY_TEST_URL}",
    ]
    if not base_url or not password:
        lines.append(f"{provider_label} não configurado ou senha ausente; dry-run encerrado.")
        return "\n".join(lines)

    track_meta, api_notes = _spotify_api_fetch_test_track()
    if api_notes:
        lines.append("Notas Spotify API:")
        lines.extend(f"- {note}" for note in api_notes)
    if track_meta is None:
        track_meta = _spotify_static_test_track_metadata()
        lines.append("Usando metadata estática conhecida para ainda validar normalização de query e mirror.")

    title = str(track_meta.get("title") or "Castle Vein").strip()
    primary_artist = str(track_meta.get("primary_artist") or "Heaven Pierce Her").strip()
    duration_ms = int(track_meta.get("duration_ms") or 0)
    spotify_url = str(track_meta.get("url") or VALID_SPOTIFY_TEST_URL).strip()

    try:
        from .backends.lavalink import LavalinkBackend, LavalinkConfig
        from .models import MusicTrack

        backend = LavalinkBackend(LavalinkConfig(
            enabled=True,
            mode=str(cfg.get("mode") or "auto"),
            host=str(cfg.get("host") or "127.0.0.1"),
            port=int(cfg.get("port") or 2333),
            password=password,
            secure=bool(cfg.get("secure") or False),
            node_name=str(cfg.get("node_name") or "lavalink"),
            timeout_seconds=float(cfg.get("timeout_seconds") or 45.0),
            provider=str(cfg.get("provider") or "lavalink"),
        ))
        # Simula o formato que o fluxo local do Spotify costuma entregar:
        # título já pode vir como "Artista - Música" e uploader também como artista.
        # A correção esperada é não gerar "Artista Artista - Música".
        simulated_track = MusicTrack(
            title=f"{primary_artist} - {title}",
            webpage_url=spotify_url,
            requester_id=0,
            requester_name="diagnóstico",
            duration=duration_ms / 1000 if duration_ms else None,
            uploader=primary_artist,
            source="spotify",
            original_url=spotify_url,
            extractor="spotify",
        )
        metadata_query = backend._metadata_search_query(simulated_track, fallback_query=spotify_url)
        candidates = backend._mirror_search_candidates(simulated_track, fallback_query=spotify_url)
    except Exception as exc:
        lines.append(f"ERRO ao gerar query pelo backend: {type(exc).__name__}: {exc}")
        return "\n".join(lines)

    prefixes, prefix_notes = _mirror_prefixes_for_diagnostics()
    lines.append("Metadata usada:")
    lines.append(json.dumps(_safe_report_obj({
        "source": track_meta.get("metadata_source"),
        "title": title,
        "artist": primary_artist,
        "duration_ms": duration_ms,
    }), ensure_ascii=False, indent=2))
    lines.append(f"Mirror prefixes efetivos: {', '.join(prefixes)}")
    if prefix_notes:
        lines.append("Notas de prefixo:")
        lines.extend(f"- {note}" for note in prefix_notes)
    lines.append(f"Query metadata normalizada: {metadata_query!r}")
    lines.append("Candidatos gerados:")
    lines.append(json.dumps(candidates[:6], ensure_ascii=False, indent=2))

    decision = "fallback local · Spotify"
    tested: list[dict[str, Any]] = []
    for candidate in candidates[:4]:
        enc = urllib.parse.quote(candidate, safe="")
        result = _http_json(f"{base_url}/v4/loadtracks?identifier={enc}", password=password, timeout=25.0)
        summary = _summarize_loadtracks(result)
        first = (summary.get("tracks") or [None])[0] if isinstance(summary, dict) else None
        item: dict[str, Any] = {
            "candidate": candidate,
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
            "latency_ms": result.get("latency_ms"),
            "loadType": summary.get("loadType") if isinstance(summary, dict) else None,
            "tracks_found": summary.get("tracks_found") if isinstance(summary, dict) else None,
            "first_track": first,
            "strict_match": False,
        }
        if isinstance(first, dict):
            compare_meta = {
                "title": first.get("title") or "",
                "uploader": first.get("author") or "",
                "duration": (float(first.get("length") or 0) / 1000) if first.get("length") else None,
            }
            try:
                item["strict_match"] = bool(backend._mirror_meta_matches_track(simulated_track, compare_meta, candidate=candidate))
            except Exception as exc:
                item["match_error"] = f"{type(exc).__name__}: {exc}"
        tested.append(item)
        if item.get("strict_match"):
            decision = f"{provider_label} · mirror Spotify aprovado"
            break

    lines.append("Resultados dos mirrors testados:")
    lines.append(json.dumps(_safe_report_obj(tested), ensure_ascii=False, indent=2))
    lines.append(f"Decisão simulada: {decision}")
    if any("  " in str(c) for c in candidates):
        lines.append("AVISO: há espaços duplicados nos candidatos gerados.")
    duplicated_pattern = f"{primary_artist} {primary_artist}".lower()
    if duplicated_pattern in "\n".join(str(c).lower() for c in candidates):
        lines.append("AVISO: artista ainda apareceu duplicado na query gerada.")
    return "\n".join(lines)


def _service_restart_markers() -> str:
    cmds = [
        ["systemctl", "show", "tts-bot.service", "-p", "ActiveEnterTimestamp", "-p", "ExecMainStartTimestamp", "-p", "MainPID", "--no-pager"],
        ["systemctl", "show", "lavalink.service", "-p", "ActiveEnterTimestamp", "-p", "ExecMainStartTimestamp", "-p", "MainPID", "--no-pager"],
    ]
    lines = [
        "Use estes horários para diferenciar logs antigas de logs pós-restart.",
        "Logs locais podem conter histórico antigo; journalctl por unidade é mais confiável para eventos recentes.",
    ]
    for cmd in cmds:
        lines.append(_run_cmd(cmd, timeout=8.0, cwd=REPO_ROOT))
    return "\n\n".join(lines)

def _spotify_api_test() -> str:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "") or os.getenv("SPOTIFY_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "") or os.getenv("SPOTIFY_SECRET", "")
    lines = [f"Link Spotify válido usado no diagnóstico: {VALID_SPOTIFY_TEST_URL}"]
    lines.append(f"client_id: {'definido (' + str(len(client_id)) + ' chars)' if client_id else 'não definido'}")
    lines.append(f"client_secret: {'definido (' + str(len(client_secret)) + ' chars)' if client_secret else 'não definido'}")
    if not client_id or not client_secret:
        lines.append("Spotify API não testada: SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET ausentes.")
        return "\n".join(lines)
    import base64
    try:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=body,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=18) as resp:
            token_payload = json.loads(resp.read().decode("utf-8", "replace"))
        token = token_payload.get("access_token") or ""
        lines.append(f"token_ok: {bool(token)}")
        if not token:
            return "\n".join(lines)
        req = urllib.request.Request(
            f"https://api.spotify.com/v1/tracks/{VALID_SPOTIFY_TEST_ID}?market=BR",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=18) as resp:
            track = json.loads(resp.read().decode("utf-8", "replace"))
        artists = ", ".join(a.get("name", "") for a in track.get("artists", []) if isinstance(a, dict))
        summary = {
            "ok": True,
            "id": track.get("id"),
            "title": track.get("name"),
            "artists": artists,
            "duration_ms": track.get("duration_ms"),
            "preview_url_defined": bool(track.get("preview_url")),
        }
        lines.append(json.dumps(summary, ensure_ascii=False, indent=2))
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace")
        lines.append(f"HTTP_ERROR: {exc.code}")
        lines.append(redact(body[:1200]))
    except Exception as exc:
        lines.append(f"ERRO: {type(exc).__name__}: {exc}")
    return "\n".join(lines)


def _lavalink_tests(cfg: dict[str, Any]) -> str:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    password = str(cfg.get("raw_password") or "")
    provider_label = str(cfg.get("provider_label") or "Lavalink")
    if not base_url or not password:
        return f"{provider_label} não configurado ou senha ausente."
    lines: list[str] = []
    lines.append(f"Node efetivo testado: {provider_label} ({base_url})")
    analysis = _application_lavasrc_analysis()
    lines.append("Análise rápida do application.yml/LavaSrc:")
    lines.append(json.dumps(_safe_report_obj(analysis), ensure_ascii=False, indent=2))
    if analysis.get("warnings"):
        lines.append("\nAVISO: há alerta de configuração acima. Se o Lavalink estiver em Connection refused, corrija isso primeiro.")

    info = _http_json(f"{base_url}/v4/info", password=password, timeout=18.0)
    lines.append("\n/v4/info:")
    lines.append(json.dumps(_safe_report_obj(info), ensure_ascii=False, indent=2))

    prefixes, notes = _mirror_prefixes_for_diagnostics()
    if notes:
        lines.append("\nNotas sobre mirrors configurados:")
        lines.extend(f"- {note}" for note in notes)
    mirror_prefix = prefixes[0]

    tests = [
        ("MP3 HTTP direto", "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"),
        ("SoundCloud busca", "scsearch:megalovania"),
        ("SoundCloud link válido", VALID_SOUNDCLOUD_TEST_URL),
        (f"Mirror LavaSrc configurado ({mirror_prefix})", f"{mirror_prefix}:505 arctic monkeys"),
    ]
    for label, identifier in tests:
        enc = urllib.parse.quote(identifier, safe="")
        result = _http_json(f"{base_url}/v4/loadtracks?identifier={enc}", password=password, timeout=30.0)
        lines.append(f"\n{label} -> {identifier}:")
        lines.append(json.dumps(_safe_report_obj(_summarize_loadtracks(result)), ensure_ascii=False, indent=2))
    lines.append(
        "\nSpotify direto via spsearch: omitido. No fluxo atual, Spotify é resolvido pela API do bot "
        "e depois espelhado pelo mirror configurado; se o application.yml estiver com spotify:false, spsearch vazio é esperado."
    )
    lines.append(
        "\nYouTube direto no Lavalink: omitido. No fluxo atual, YouTube fica fora do Lavalink e é validado no teste yt-dlp local com cookies."
    )
    return "\n".join(lines)

def _safe_report_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _safe_report_obj(_mask_value(k, v)) for k, v in obj.items() if k != "raw_password"}
    if isinstance(obj, list):
        return [_safe_report_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def _run_yt_dlp_quick(args: list[str], *, timeout: float = 35.0) -> str:
    """Executa yt-dlp e encerra assim que título+duração aparecerem.

    Alguns builds continuam fazendo trabalho de rede mesmo depois de imprimir as
    duas linhas úteis; para diagnóstico, duas linhas já bastam.
    """
    started = time.monotonic()
    lines: list[str] = [f"$ {' '.join(args)}"]
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(REPO_ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        captured: list[str] = []
        assert proc.stdout is not None
        while True:
            if time.monotonic() - started > timeout:
                break
            line = proc.stdout.readline()
            if line:
                captured.append(line.rstrip("\n"))
                useful = [x for x in captured if x.strip() and not x.startswith("[")]
                if len(useful) >= 2:
                    proc.terminate()
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=3)
                    lines.append("exit=0 (saída útil coletada; processo encerrado pelo diagnóstico)")
                    lines.extend(captured)
                    return "\n".join(lines)
                continue
            rc = proc.poll()
            if rc is not None:
                lines.append(f"exit={rc}")
                lines.extend(captured)
                return "\n".join(lines)
            time.sleep(0.05)
        if proc and proc.poll() is None:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=3)
        lines.append(f"TIMEOUT após {timeout}s")
        if 'captured' in locals():
            lines.extend(captured)
        return "\n".join(lines)
    except Exception as exc:
        if proc and proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.kill()
        return f"$ {' '.join(args)}\nERRO: {type(exc).__name__}: {exc}"


def _yt_dlp_test() -> str:
    cookie_candidates = [
        os.getenv("MUSIC_YTDLP_COOKIES_FILE"),
        os.getenv("YTDLP_COOKIES_FILE"),
        os.getenv("YT_DLP_COOKIES_FILE"),
        str(REPO_ROOT / "cookies.txt"),
    ]
    cookie_path = ""
    for candidate in cookie_candidates:
        if candidate and Path(candidate).exists():
            cookie_path = str(Path(candidate))
            break
    lines = []
    if cookie_path:
        path = Path(cookie_path)
        lines.append(f"cookies.txt: existe em {path} ({path.stat().st_size} bytes)")
    else:
        lines.append("cookies.txt: não encontrado pelos caminhos conhecidos")
    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--get-title",
        "--get-duration",
        "-f",
        "bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best",
    ]
    if cookie_path:
        args.extend(["--cookies", cookie_path])
    args.append(VALID_YOUTUBE_TEST_URL)
    lines.append(_run_yt_dlp_quick(args, timeout=35.0))
    return "\n".join(lines)

def _local_log_tail() -> str:
    note = "Observação: logs locais podem conter eventos pré-restart; confira a seção Marcos de restart/runtime para contextualizar horários.\n\n"
    log_dir = REPO_ROOT / "logs"
    if not log_dir.exists():
        return note + "Pasta logs/ não existe."
    parts: list[str] = []
    for path in sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:4]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tail = "\n".join(text.splitlines()[-180:])
            parts.append(f"===== {path.relative_to(REPO_ROOT)} =====\n{tail}")
        except Exception as exc:
            parts.append(f"===== {path} =====\nERRO: {type(exc).__name__}: {exc}")
    return note + ("\n\n".join(parts) if parts else "Nenhum .log em logs/.")


def _nodelink_enabled_for_diagnostics() -> bool:
    provider = str(os.getenv("MUSIC_NODE_PROVIDER", "lavalink") or "lavalink").strip().lower()
    return provider in {"nodelink", "node", "auto"} and str(os.getenv("NODELINK_ENABLED", "false") or "false").strip().lower() in {"1", "true", "yes", "y", "on", "sim"}


def _systemd_units_for_diagnostics(*, include_nodelink: bool | None = None) -> list[str]:
    units = ["tts-bot.service", "lavalink.service", "callkeeper.service"]
    if include_nodelink is None:
        include_nodelink = _nodelink_enabled_for_diagnostics()
    if include_nodelink:
        units.insert(2, "nodelink.service")
    return units


def _node_process_inventory() -> str:
    """Mostra processos Node.js sem confundir Sinuca Activity com NodeLink."""
    ss_output = _run_cmd(["ss", "-ltnp"], timeout=8.0)
    lines: list[str] = []
    proc_root = Path("/proc")
    for item in sorted(proc_root.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else -1):
        if not item.name.isdigit():
            continue
        pid = item.name
        try:
            cmdline = (item / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
            if not cmdline:
                continue
            exe = os.readlink(item / "exe")
        except Exception:
            continue
        combined = f"{exe} {cmdline}".lower()
        if "node" not in combined and "npm" not in combined:
            continue
        try:
            cwd = os.readlink(item / "cwd")
        except Exception:
            cwd = "?"
        combined_with_cwd = f"{combined} {cwd.lower()}"
        if "nodelink" in combined_with_cwd or "/opt/nodelink" in combined_with_cwd:
            label = "NodeLink"
        elif "sinuca" in combined_with_cwd or "activity/sinuca-server" in combined_with_cwd:
            label = "Sinuca Activity"
        else:
            label = "Node.js (outro)"
        listen = ""
        for ss_line in ss_output.splitlines():
            if f"pid={pid}," in ss_line or f"pid={pid})" in ss_line:
                listen = ss_line.strip()
                break
        suffix = f" | listen={listen}" if listen else ""
        lines.append(f"pid={pid} | tipo={label} | cwd={cwd} | cmd={cmdline}{suffix}")
    if not lines:
        return "Nenhum processo Node.js encontrado."
    return "Processos Node.js detectados:\n" + "\n".join(lines)



def _tts_runtime_snapshot(router: Any, guild_id: int) -> str:
    try:
        state = router.get_state(int(guild_id)) if router is not None and hasattr(router, "get_state") else None
    except Exception as exc:
        return f"Não consegui ler estado TTS/música: {type(exc).__name__}: {exc}"
    if state is None:
        return "Sem estado de música/TTS para esta guild."
    now = time.monotonic()
    data = {
        "current_backend": getattr(state, "current_backend", ""),
        "current_status": getattr(state, "current_status", ""),
        "current_track": getattr(getattr(state, "current", None), "title", ""),
        "tts_voice_touched": bool(getattr(state, "tts_voice_touched", False)),
        "last_tts_activity_age_s": round(max(0.0, now - float(getattr(state, "last_tts_activity_at", 0.0) or 0.0)), 2) if getattr(state, "last_tts_activity_at", 0.0) else None,
        "lavalink_tts_active_for_s": round(max(0.0, float(getattr(state, "lavalink_tts_until", 0.0) or 0.0) - now), 2),
        "lavalink_resume_grace_for_s": round(max(0.0, float(getattr(state, "lavalink_resume_grace_until", 0.0) or 0.0) - now), 2),
        "tts_session_active_for_s": round(max(0.0, float(getattr(state, "tts_session_active_until", 0.0) or 0.0) - now), 2),
        "tts_lavalink_failures": int(getattr(state, "tts_lavalink_failures", 0) or 0),
        "tts_session_last_error": str(getattr(state, "tts_session_last_error", "") or ""),
        "current_lavalink_player_present": getattr(state, "current_lavalink_player", None) is not None,
        "current_lavalink_playable_present": getattr(state, "current_lavalink_playable", None) is not None,
        "current_source_present": getattr(state, "current_source", None) is not None,
        "current_status_age_s": round(max(0.0, now - float(getattr(state, "current_status_changed_at", 0.0) or 0.0)), 2),
        "current_resolve_task_active": bool(getattr(state, "current_resolve_task", None) is not None and not getattr(state, "current_resolve_task", None).done()),
        "tts_public_base_url_configured": bool(str(getattr(config, "MUSIC_TTS_PUBLIC_BASE_URL", "") or "").strip()),
        "tts_public_base_url": redact(str(getattr(config, "MUSIC_TTS_PUBLIC_BASE_URL", "") or "").strip()),
        "tts_internal_base_url_configured": bool(str(getattr(config, "MUSIC_TTS_INTERNAL_BASE_URL", "") or "").strip()),
        "tts_internal_base_url": redact(str(getattr(config, "MUSIC_TTS_INTERNAL_BASE_URL", "") or "").strip()),
        "lavalink_tts_internal_first": bool(getattr(config, "MUSIC_LAVALINK_TTS_INTERNAL_FIRST", True)),
        "lavalink_tts_file_fallback": bool(getattr(config, "MUSIC_LAVALINK_TTS_FILE_FALLBACK", False)),
        "tts_audio_format": str(getattr(config, "MUSIC_TTS_AUDIO_FORMAT", "opus") or "opus"),
        "tts_audio_fallback_format": str(getattr(config, "MUSIC_TTS_AUDIO_FALLBACK_FORMAT", "mp3") or "mp3"),
        "tts_opus_bitrate": str(getattr(config, "MUSIC_TTS_OPUS_BITRATE", "48k") or "48k"),
        "tts_preroll_silence_ms": int(getattr(config, "MUSIC_TTS_PREROLL_SILENCE_MS", 140) or 0),
        "tts_postroll_silence_ms": int(getattr(config, "MUSIC_TTS_POSTROLL_SILENCE_MS", 180) or 0),
        "tts_fade_in_ms": int(getattr(config, "MUSIC_TTS_FADE_IN_MS", 45) or 0),
        "tts_fade_out_ms": int(getattr(config, "MUSIC_TTS_FADE_OUT_MS", 70) or 0),
        "tts_resume_seek_ahead_ms": int(getattr(config, "MUSIC_TTS_RESUME_SEEK_AHEAD_MS", 120) or 0),
        "tts_lavalink_volume_ramp_enabled": bool(getattr(config, "MUSIC_TTS_LAVALINK_VOLUME_RAMP_ENABLED", True)),
        "tts_lavalink_volume_ramp_ms": int(getattr(config, "MUSIC_TTS_LAVALINK_VOLUME_RAMP_MS", 180) or 0),
        "tts_lavalink_ramp_floor_percent": int(getattr(config, "MUSIC_TTS_LAVALINK_RAMP_FLOOR_PERCENT", 5) or 0),
    }
    return json.dumps(_safe_report_obj(data), ensure_ascii=False, indent=2)

def _journalctl_commands(*, full: bool = False) -> list[list[str]]:
    if full:
        spec = [("tts-bot.service", "2 hours ago", "1200"), ("lavalink.service", "2 hours ago", "900"), ("callkeeper.service", "2 hours ago", "500")]
        if _nodelink_enabled_for_diagnostics():
            spec.insert(2, ("nodelink.service", "2 hours ago", "500"))
    else:
        spec = [("tts-bot.service", "8 minutes ago", "160"), ("lavalink.service", "8 minutes ago", "160")]
        if _nodelink_enabled_for_diagnostics():
            spec.append(("nodelink.service", "8 minutes ago", "100"))
    return [["journalctl", "-u", unit, "--since", since, "-n", limit, "--no-pager", "-o", "cat"] for unit, since, limit in spec]


def _journalctl_tail() -> str:
    parts = []
    for cmd in _journalctl_commands(full=False):
        out = _run_cmd(cmd, timeout=6.0, cwd=REPO_ROOT)
        lines = out.splitlines()
        if len(lines) > 260:
            lines = lines[:3] + ["... (cortado) ..."] + lines[-250:]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _application_yml_head() -> str:
    path = Path("/opt/lavalink/application.yml")
    if not path.exists():
        return "/opt/lavalink/application.yml não existe ou não é acessível."
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()[:160]
        return "\n".join(f"{idx + 1}: {line}" for idx, line in enumerate(lines))
    except Exception as exc:
        return f"ERRO ao ler application.yml: {type(exc).__name__}: {exc}"



BASE_ARCHIVE_ROOT_NAME = "tts-bot-main"
BASE_ARCHIVE_MAX_BYTES = 23 * 1024 * 1024
BASE_ARCHIVE_SENSITIVE_NAMES = {
    ".env",
    "cookies.txt",
    "cookie.txt",
    "youtube-cookies.txt",
}
BASE_ARCHIVE_SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)


def _git_cmd(args: list[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _is_sensitive_tracked_file(rel: str) -> bool:
    normalized = rel.replace("\\", "/").lstrip("/")
    name = Path(normalized).name.lower()
    lowered = normalized.lower()
    if name in BASE_ARCHIVE_SENSITIVE_NAMES:
        return True

    # .env real e variantes locais são sensíveis.
    # .env.example/.env.sample/.env.template são exemplos rastreados e devem ir no zip,
    # para a base anexada ficar equivalente à base baixada do GitHub.
    allowed_env_examples = {".env.example", ".env.sample", ".env.template"}
    if name.startswith(".env") and name not in allowed_env_examples:
        return True

    if lowered.endswith(BASE_ARCHIVE_SENSITIVE_SUFFIXES):
        return True
    # Banco/log/cookies não deveriam estar rastreados, mas se estiverem, não anexa no Discord.
    if lowered.endswith((".sqlite", ".sqlite3", ".db", ".log")):
        return True
    if "cookies" in lowered and lowered.endswith(".txt"):
        return True
    return False


def build_git_tracked_base_archive_sync() -> tuple[bytes | None, str, str, str]:
    """Cria um zip com os arquivos rastreados pelo git no estado atual do disco.

    Usa `git ls-files`, então pega apenas arquivos rastreados pelo repositório,
    mas com o conteúdo atual da VPS, inclusive mudanças ainda não commitadas.
    Arquivos sensíveis rastreados por engano são pulados e listados no manifesto
    retornado para o relatório de diagnóstico. O manifesto não entra no zip,
    para a base ficar igual ao zip baixado do GitHub.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"tts-bot-base-git-tracked-{stamp}.zip"

    try:
        root_check = _git_cmd(["rev-parse", "--show-toplevel"], timeout=8.0)
    except Exception as exc:
        return None, filename, f"Não consegui executar git: {type(exc).__name__}: {exc}", ""

    if root_check.returncode != 0:
        return None, filename, "Repo não parece ter .git acessível; não foi possível gerar a base rastreada pelo Git.", ""

    ls = _git_cmd(["ls-files", "-z"], timeout=20.0)
    if ls.returncode != 0:
        return None, filename, f"git ls-files falhou: {redact(ls.stderr or ls.stdout)}", ""

    rels = [item for item in ls.stdout.split("\0") if item]
    if not rels:
        return None, filename, "git ls-files não retornou arquivos rastreados.", ""

    status = _git_cmd(["status", "--short"], timeout=12.0)
    commit = _git_cmd(["rev-parse", "HEAD"], timeout=8.0)
    branch = _git_cmd(["rev-parse", "--abbrev-ref", "HEAD"], timeout=8.0)

    skipped: list[str] = []
    added = 0
    bio = io.BytesIO()
    try:
        with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            manifest_lines = [
                "Base gerada pelo /vps",
                f"Gerado em: {_now_stamp()}",
                f"Repo root: {REPO_ROOT}",
                f"Branch: {(branch.stdout or '').strip() if branch.returncode == 0 else 'desconhecida'}",
                f"Commit HEAD: {(commit.stdout or '').strip() if commit.returncode == 0 else 'desconhecido'}",
                "Conteúdo: arquivos retornados por `git ls-files`, usando o conteúdo atual do disco.",
                "Arquivos sensíveis rastreados por engano são pulados.",
                "",
                "# git status --short",
                (status.stdout or "limpo").rstrip() if status.returncode == 0 else redact(status.stderr or status.stdout),
                "",
                "# arquivos pulados",
            ]

            for rel in rels:
                safe_rel = rel.replace("\\", "/").lstrip("/")
                if not safe_rel or safe_rel.startswith("../") or "/../" in safe_rel:
                    skipped.append(rel)
                    continue
                if _is_sensitive_tracked_file(safe_rel):
                    skipped.append(safe_rel)
                    continue
                src = REPO_ROOT / safe_rel
                if not src.is_file():
                    skipped.append(f"{safe_rel} (não é arquivo regular)")
                    continue
                zf.write(src, f"{BASE_ARCHIVE_ROOT_NAME}/{safe_rel}")
                added += 1

            manifest_lines.extend(skipped or ["nenhum"])
            manifest_lines.extend(["", f"# total de arquivos anexados: {added}"])
            manifest_text = redact("\n".join(manifest_lines)) + "\n"
    except Exception as exc:
        return None, filename, f"Falha ao montar zip da base: {type(exc).__name__}: {exc}", ""

    payload = bio.getvalue()
    if len(payload) > BASE_ARCHIVE_MAX_BYTES:
        size_mb = len(payload) / (1024 * 1024)
        return None, filename, f"Base zip ficou grande demais para anexar com segurança no Discord: {size_mb:.1f} MB.", manifest_text

    summary = f"Base git-tracked anexada: {added} arquivos; {len(skipped)} pulado(s); tamanho {len(payload) / (1024 * 1024):.2f} MB."
    return payload, filename, summary, manifest_text


async def build_git_tracked_base_archive() -> tuple[bytes | None, str, str, str]:
    return await asyncio.to_thread(build_git_tracked_base_archive_sync)


def build_music_diagnostics_report_sync(router: Any, options: DiagnosticsOptions) -> str:
    sections: list[tuple[str, str]] = []
    sections.append((
        "Resumo",
        "\n".join([
            f"Gerado em: {_now_stamp()}",
            f"Guild: {options.guild_name} ({options.guild_id})",
            f"Solicitado por: {options.requester_name} ({options.requester_id})",
            f"Python: {sys.version.split()[0]} ({sys.executable})",
            f"Sistema: {platform.platform()}",
            f"Repo root: {REPO_ROOT}",
        ]),
    ))
    sections.append((
        "Pacotes",
        "\n".join([
            f"discord.py: {_package_version('discord.py')}",
            f"wavelink: {_package_version('wavelink')}",
            f"yt-dlp: {_package_version('yt-dlp')}",
            f"PyNaCl: {_package_version('PyNaCl')}",
            f"aiohttp: {_package_version('aiohttp')}",
        ]),
    ))
    sections.append(("Variáveis relevantes (.env carregado pelo processo)", json.dumps(_safe_report_obj(_read_env_flags()), ensure_ascii=False, indent=2)))
    db_text, _ = _db_snapshot()
    sections.append(("DB musicnode", db_text))
    cfg = _lavalink_cfg_from_router(router, options.guild_id)
    sections.append(("Config Lavalink efetiva no bot", json.dumps(_safe_report_obj(cfg), ensure_ascii=False, indent=2)))
    sections.append(("Estado TTS/música em memória", _tts_runtime_snapshot(router, options.guild_id)))
    sections.append(("Teste Spotify API do bot", _spotify_api_test()))
    sections.append(("Dry-run Spotify mirror/fallback (sem tocar áudio)", _spotify_dry_run_mirror_test(cfg)))
    sections.append(("Testes Lavalink REST", _lavalink_tests(cfg)))
    sections.append(("Marcos de restart/runtime", _service_restart_markers()))
    sections.append(("Teste yt-dlp local com cookies", _yt_dlp_test()))
    sections.append(("application.yml do Lavalink (sanitizado)", _application_yml_head()))
    if options.include_local_logs:
        sections.append(("Logs locais do bot", _local_log_tail()))
    if options.include_journalctl:
        sections.append(("journalctl recente", _journalctl_tail()))

    body_parts: list[str] = []
    for title, body in sections:
        body_parts.append(f"\n\n# {title}\n{redact(body)}")
    report = "".join(body_parts).strip() + "\n"
    # Mantém o anexo menor para não bater limite do Discord.
    max_chars = 1_800_000
    if len(report) > max_chars:
        report = report[:max_chars] + "\n\n[relatório cortado por tamanho]\n"
    return redact(report)


async def build_music_diagnostics_report(router: Any, options: DiagnosticsOptions) -> str:
    return await asyncio.to_thread(build_music_diagnostics_report_sync, router, options)


def _music_diagnostics_sections(router: Any, options: DiagnosticsOptions) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    """Monta seções reutilizáveis para relatório texto e pacote modular."""
    cfg = _lavalink_cfg_from_router(router, options.guild_id)
    db_text, db_data = _db_snapshot()
    summary = "\n".join([
        f"Gerado em: {_now_stamp()}",
        f"Guild: {options.guild_name} ({options.guild_id})",
        f"Solicitado por: {options.requester_name} ({options.requester_id})",
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"Sistema: {platform.platform()}",
        f"Repo root: {REPO_ROOT}",
    ])
    package_versions = "\n".join([
        f"discord.py: {_package_version('discord.py')}",
        f"wavelink: {_package_version('wavelink')}",
        f"yt-dlp: {_package_version('yt-dlp')}",
        f"PyNaCl: {_package_version('PyNaCl')}",
        f"aiohttp: {_package_version('aiohttp')}",
    ])
    sections: list[tuple[str, str, str]] = [
        ("00-resumo.txt", "Resumo", summary),
        ("01-pacotes.txt", "Pacotes", package_versions),
        ("02-env-relevante.json", "Variáveis relevantes (.env carregado pelo processo)", json.dumps(_safe_report_obj(_read_env_flags()), ensure_ascii=False, indent=2)),
        ("03-db-musicnode.json", "DB musicnode", db_text),
        ("04-lavalink-config-efetiva.json", "Config Lavalink efetiva no bot", json.dumps(_safe_report_obj(cfg), ensure_ascii=False, indent=2)),
        ("05-runtime-tts-musica.json", "Estado TTS/música em memória", _tts_runtime_snapshot(router, options.guild_id)),
        ("tests/spotify-api.txt", "Teste Spotify API do bot", _spotify_api_test()),
        ("tests/spotify-mirror-dry-run.txt", "Dry-run Spotify mirror/fallback (sem tocar áudio)", _spotify_dry_run_mirror_test(cfg)),
        ("tests/lavalink-rest.txt", "Testes Lavalink REST", _lavalink_tests(cfg)),
        ("tests/ytdlp-local.txt", "Teste yt-dlp local com cookies", _yt_dlp_test()),
        ("system/restart-markers.txt", "Marcos de restart/runtime", _service_restart_markers()),
        ("lavalink/application-sanitized.yml", "application.yml do Lavalink (sanitizado)", _application_yml_head()),
    ]
    return sections, {"cfg": cfg, "db": db_data}


def _music_diagnostics_summary_text(sections: list[tuple[str, str, str]]) -> str:
    """Resumo textual que continua útil mesmo quando o zip é o anexo principal."""
    wanted = {
        "Resumo",
        "Pacotes",
        "Estado TTS/música em memória",
        "Teste Spotify API do bot",
        "Dry-run Spotify mirror/fallback (sem tocar áudio)",
        "Testes Lavalink REST",
        "Teste yt-dlp local com cookies",
    }
    body_parts: list[str] = []
    for _arc, title, body in sections:
        if title in wanted:
            body_parts.append(f"\n\n# {title}\n{redact(body)}")
    return ("".join(body_parts).strip() + "\n") if body_parts else "Diagnóstico musical gerado em pacote modular.\n"


def _diagnostic_log_commands() -> dict[str, list[str]]:
    return {
        "logs/relevant/music-events.txt": [
            "bash", "-lc",
            "journalctl -u tts-bot.service --since '4 hours ago' -n 2500 --no-pager -o cat "
            "| grep -Ei 'music|lavalink|spotify|soundcloud|youtube|yt-dlp|fallback|premature|TrackException|LoadException|tts_|tts |duck|resolve|resolving|FFmpeg|erro|falhou|timeout|exception|traceback' || true",
        ],
        "logs/relevant/tts-events.txt": [
            "bash", "-lc",
            "journalctl -u tts-bot.service --since '4 hours ago' -n 2200 --no-pager -o cat "
            "| grep -Ei 'tts|tts_voice|tts-audio|duck|lavalink_tts|public_url|internal_url|voice.*assumindo|timeout|falhou|erro' || true",
        ],
        "logs/relevant/errors-warnings.txt": [
            "bash", "-lc",
            "journalctl -u tts-bot.service -u lavalink.service --since '4 hours ago' -n 2500 --no-pager -o cat "
            "| grep -Ei 'warning|error|exception|traceback|falhou|erro|timeout|TrackException|LoadException|stuck|premature|invalid status|404|403|429|5[0-9][0-9]' || true",
        ],
        "logs/relevant/lavalink-events.txt": [
            "bash", "-lc",
            "journalctl -u lavalink.service --since '4 hours ago' -n 1800 --no-pager -o cat "
            "| grep -Ei 'ready|lavasrc|spotify|soundcloud|deezer|youtube|loadtracks|track|exception|failed|error|404|403|429|5[0-9][0-9]' || true",
        ],
        "logs/raw/tts-bot-journal-tail.txt": [
            "bash", "-lc",
            "journalctl -u tts-bot.service --since '4 hours ago' -n 3500 --no-pager -o short-iso",
        ],
        "logs/raw/lavalink-journal-tail.txt": [
            "bash", "-lc",
            "journalctl -u lavalink.service --since '4 hours ago' -n 2500 --no-pager -o short-iso",
        ],
    }


def _local_logs_archive_text() -> dict[str, str]:
    result: dict[str, str] = {}
    log_dir = REPO_ROOT / "logs"
    candidates: list[Path] = []
    if log_dir.exists():
        with contextlib.suppress(Exception):
            candidates.extend(sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True))
    for extra in [REPO_ROOT / "bot.log", REPO_ROOT / "logs" / "bot.log", REPO_ROOT / "logs" / "updater.log"]:
        if extra.exists() and extra not in candidates:
            candidates.append(extra)
    for path in candidates[:12]:
        try:
            rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else Path(path.name)
        except Exception:
            rel = Path(path.name)
        arc = "logs/local/" + str(rel).replace(os.sep, "_")
        result[arc] = _safe_read_file(path, max_chars=900_000)
    if not result:
        result["logs/local/sem-logs.txt"] = "Nenhum arquivo de log local encontrado.\n"
    return result


MUSIC_DIAGNOSTICS_ARCHIVE_MAX_BYTES = 24 * 1024 * 1024


def build_music_diagnostics_archive_sync(router: Any, options: DiagnosticsOptions) -> tuple[bytes | None, str, str, str]:
    """Gera diagnóstico musical em zip modular, sem perder testes/logs importantes."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"vps-music-diagnostics-{stamp}.zip"
    bio = io.BytesIO()
    added = 0
    try:
        cleanup_note = cleanup_music_diagnostics_temp_artifacts()
        sections, _meta = _music_diagnostics_sections(router, options)
        summary_text = _music_diagnostics_summary_text(sections)
        with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            _write_zip_text(zf, "README.txt", "Diagnóstico musical modular. O resumo fica em 00-resumo-curto.txt e summary.txt; logs brutas ficam em logs/raw/.\n"); added += 1
            _write_zip_text(zf, "00-resumo-curto.txt", summary_text); added += 1
            _write_zip_text(zf, "summary.txt", summary_text); added += 1
            _write_zip_text(zf, "system/diagnostic-temp-cleanup.txt", cleanup_note); added += 1
            for arc, _title, body in sections:
                _write_zip_text(zf, arc, body); added += 1
            _write_zip_text(zf, "bot/env.sanitized.txt", _sanitized_env_text()); added += 1
            for arc, cmd in _diagnostic_log_commands().items():
                timeout = 26.0 if "/raw/" in arc else 18.0
                _write_zip_text(zf, arc, _run_cmd(cmd, timeout=timeout)); added += 1
            for arc, text in _local_logs_archive_text().items():
                _write_zip_text(zf, arc, text); added += 1
            # Informações úteis para diagnosticar peso/IO sem tornar o relatório síncrono demais.
            _write_zip_text(zf, "system/disk-and-process.txt", _run_cmd(["bash", "-lc", "df -h; echo; free -m; echo; ps -eo pid,ppid,%cpu,%mem,etime,cmd --sort=-%cpu | head -40"], timeout=12.0)); added += 1
    except Exception as exc:
        return None, filename, f"Falha ao montar diagnóstico musical modular: {type(exc).__name__}: {exc}", ""
    payload = bio.getvalue()
    summary = f"Diagnóstico musical modular anexado: {added} item(ns); tamanho {len(payload) / (1024 * 1024):.2f} MB."
    if len(payload) > MUSIC_DIAGNOSTICS_ARCHIVE_MAX_BYTES:
        return None, filename, f"Diagnóstico musical modular ficou grande demais para anexar: {len(payload) / (1024 * 1024):.1f} MB.", summary_text
    # Sucesso modular: o resumo já está dentro do zip. Não retorne fallback_report,
    # para o comando /vps não anexar um segundo arquivo de resumo.
    return payload, filename, summary, ""


async def build_music_diagnostics_archive(router: Any, options: DiagnosticsOptions) -> tuple[bytes | None, str, str, str]:
    return await asyncio.to_thread(build_music_diagnostics_archive_sync, router, options)


def _local_log_tail_full() -> str:
    note = "Observação: logs locais podem conter eventos pré-restart; confira a seção Marcos de restart/runtime para contextualizar horários.\n\n"
    log_dir = REPO_ROOT / "logs"
    parts: list[str] = []

    candidates: list[Path] = []
    if log_dir.exists():
        with contextlib.suppress(Exception):
            candidates.extend(sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True))
    for extra in [REPO_ROOT / "bot.log", REPO_ROOT / "logs" / "bot.log", REPO_ROOT / "logs" / "updater.log"]:
        if extra.exists() and extra not in candidates:
            candidates.append(extra)

    if not candidates:
        return note + "Nenhum arquivo de log local encontrado."

    for path in candidates[:12]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tail = "\n".join(text.splitlines()[-1200:])
            rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
            parts.append(f"===== {rel} =====\n{tail}")
        except Exception as exc:
            parts.append(f"===== {path} =====\nERRO: {type(exc).__name__}: {exc}")
    return note + "\n\n".join(parts)


def _journalctl_full_tail() -> str:
    parts: list[str] = []
    for cmd in _journalctl_commands(full=True):
        out = _run_cmd(cmd, timeout=18.0, cwd=REPO_ROOT)
        lines = out.splitlines()
        if len(lines) > 900:
            lines = lines[:4] + ["... (cortado) ..."] + lines[-880:]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _system_status_report() -> str:
    units = _systemd_units_for_diagnostics()
    parts = [
        _run_cmd(["date", "-Is"], timeout=5.0),
        _run_cmd(["hostname"], timeout=5.0),
        _run_cmd(["uname", "-a"], timeout=5.0),
        _run_cmd(["df", "-h", "/", "/opt", "/home"], timeout=8.0),
        _run_cmd(["free", "-h"], timeout=8.0),
        _run_cmd(["ss", "-ltnp"], timeout=10.0),
        _node_process_inventory(),
        _run_cmd(["systemctl", "--no-pager", "--full", "status", *units], timeout=18.0),
        _run_cmd(["systemctl", "cat", *units], timeout=18.0),
    ]
    if not _nodelink_enabled_for_diagnostics():
        parts.append("NodeLink: não incluído nos services porque NODELINK_ENABLED=false/MUSIC_NODE_PROVIDER não seleciona NodeLink. Processos Node.js em outras portas podem ser Sinuca Activity ou outra feature; veja o inventário acima.")
    return "\n\n".join(parts)


def build_full_vps_diagnostics_report_sync(router: Any, options: DiagnosticsOptions) -> str:
    sections: list[tuple[str, str]] = []
    sections.append((
        "Resumo",
        "\n".join([
            f"Gerado em: {_now_stamp()}",
            f"Guild: {options.guild_name} ({options.guild_id})",
            f"Solicitado por: {options.requester_name} ({options.requester_id})",
            f"Python: {sys.version.split()[0]} ({sys.executable})",
            f"Sistema: {platform.platform()}",
            f"Repo root: {REPO_ROOT}",
            "Tipo: diagnóstico completo da VPS/bot",
        ]),
    ))
    sections.append((
        "Pacotes",
        "\n".join([
            f"discord.py: {_package_version('discord.py')}",
            f"wavelink: {_package_version('wavelink')}",
            f"yt-dlp: {_package_version('yt-dlp')}",
            f"PyNaCl: {_package_version('PyNaCl')}",
            f"aiohttp: {_package_version('aiohttp')}",
        ]),
    ))
    sections.append(("Variáveis relevantes (.env carregado pelo processo)", json.dumps(_safe_report_obj(_read_env_flags()), ensure_ascii=False, indent=2)))
    db_text, _ = _db_snapshot()
    sections.append(("DB musicnode", db_text))
    cfg = _lavalink_cfg_from_router(router, options.guild_id)
    sections.append(("Config Lavalink efetiva no bot", json.dumps(_safe_report_obj(cfg), ensure_ascii=False, indent=2)))
    sections.append(("Estado TTS/música em memória", _tts_runtime_snapshot(router, options.guild_id)))
    sections.append(("Teste Spotify API do bot", _spotify_api_test()))
    sections.append(("Dry-run Spotify mirror/fallback (sem tocar áudio)", _spotify_dry_run_mirror_test(cfg)))
    sections.append(("Testes Lavalink REST", _lavalink_tests(cfg)))
    sections.append(("Marcos de restart/runtime", _service_restart_markers()))
    sections.append(("Teste yt-dlp local com cookies", _yt_dlp_test()))
    sections.append(("application.yml do Lavalink (sanitizado)", _application_yml_head()))
    sections.append(("Status do sistema e services", _system_status_report()))
    sections.append(("Logs locais completas/cortadas", _local_log_tail_full()))
    sections.append(("journalctl completo/cortado", _journalctl_full_tail()))

    body_parts = [f"\n\n# {title}\n{redact(body)}" for title, body in sections]
    report = "".join(body_parts).strip() + "\n"
    max_chars = 1_900_000
    if len(report) > max_chars:
        report = report[:max_chars] + "\n\n[relatório completo cortado por tamanho]\n"
    return redact(report)


async def build_full_vps_diagnostics_report(router: Any, options: DiagnosticsOptions) -> str:
    return await asyncio.to_thread(build_full_vps_diagnostics_report_sync, router, options)


VPS_SNAPSHOT_MAX_BYTES = 24 * 1024 * 1024


def _write_zip_text(zf: zipfile.ZipFile, arcname: str, text: str) -> None:
    zf.writestr(arcname, redact(text if text is not None else ""))


def _sanitized_env_text() -> str:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return ".env não encontrado\n"
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            lines.append(raw)
            continue
        key, value = raw.split("=", 1)
        lines.append(f"{key}=***REDACTED***" if _mask_value(key, value) == "***REDACTED***" else raw)
    return "\n".join(lines) + "\n"


def _safe_read_file(path: Path, *, max_chars: int = 500_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n[arquivo cortado por tamanho]\n"
        return text
    except Exception as exc:
        return f"ERRO ao ler {path}: {type(exc).__name__}: {exc}\n"


def build_vps_snapshot_archive_sync() -> tuple[bytes | None, str, str]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"vps-snapshot-{stamp}.zip"
    bio = io.BytesIO()
    added = 0

    try:
        with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            _write_zip_text(zf, "meta/summary.txt", "\n".join([
                f"Gerado em: {_now_stamp()}",
                f"Repo root: {REPO_ROOT}",
                f"Sistema: {platform.platform()}",
                "Snapshot sanitizado da VPS para diagnóstico.",
            ]) + "\n")
            added += 1

            _write_zip_text(zf, "bot/env.sanitized.txt", _sanitized_env_text()); added += 1
            for rel in ["config.py", "requirements.txt", "cogs/music.py", "cogs/utility.py"]:
                path = REPO_ROOT / rel
                if path.exists():
                    _write_zip_text(zf, f"bot/{rel}", _safe_read_file(path)); added += 1
            for folder in ["music_system", "utility"]:
                root = REPO_ROOT / folder
                if root.exists():
                    for path in sorted(root.rglob("*.py")):
                        try:
                            arc = f"bot/{path.relative_to(REPO_ROOT)}"
                        except Exception:
                            arc = f"bot/{path.name}"
                        _write_zip_text(zf, arc, _safe_read_file(path, max_chars=250_000)); added += 1

            app_path = Path("/opt/lavalink/application.yml")
            _write_zip_text(zf, "lavalink/application.sanitized.yml", _safe_read_file(app_path, max_chars=500_000)); added += 1
            _write_zip_text(zf, "lavalink/listing.txt", _run_cmd(["bash", "-lc", "ls -lah /opt/lavalink; echo; ls -lah /opt/lavalink/plugins"], timeout=12.0)); added += 1

            _write_zip_text(zf, "db/musicnode.snapshot.txt", _db_snapshot()[0]); added += 1
            _write_zip_text(zf, "systemd/services.txt", _run_cmd(["systemctl", "cat", *_systemd_units_for_diagnostics()], timeout=18.0)); added += 1
            _write_zip_text(zf, "meta/system.txt", _system_status_report()); added += 1
            _write_zip_text(zf, "logs/tts-bot.filtered.log", _run_cmd(["bash", "-lc", "journalctl -u tts-bot.service --since '2 hours ago' -n 900 --no-pager -o cat | grep -Ei 'music|lavalink|spotify|soundcloud|youtube|yt-dlp|deezer|fallback|TrackException|LoadException|ChannelTimeout|erro|falhou|exception|traceback' || true"], timeout=18.0)); added += 1
            _write_zip_text(zf, "logs/lavalink.filtered.log", _run_cmd(["bash", "-lc", "journalctl -u lavalink.service --since '2 hours ago' -n 900 --no-pager -o cat | grep -Ei 'ready|lavasrc|spotify|soundcloud|deezer|youtube|loadtracks|master|403|404|error|exception|failed|TrackException' || true"], timeout=18.0)); added += 1
            if _nodelink_enabled_for_diagnostics():
                _write_zip_text(zf, "logs/nodelink.filtered.log", _run_cmd(["bash", "-lc", "journalctl -u nodelink.service --since '2 hours ago' -n 700 --no-pager -o cat | grep -Ei 'ready|lavalink|spotify|soundcloud|youtube|deezer|loadtracks|error|exception|failed|TrackException' || true"], timeout=18.0)); added += 1
            _write_zip_text(zf, "logs/local-bot-logs.txt", _local_log_tail()); added += 1
    except Exception as exc:
        return None, filename, f"Falha ao montar snapshot da VPS: {type(exc).__name__}: {exc}"

    payload = bio.getvalue()
    if len(payload) > VPS_SNAPSHOT_MAX_BYTES:
        return None, filename, f"Snapshot ficou grande demais para anexar com segurança: {len(payload) / (1024 * 1024):.1f} MB."
    return payload, filename, f"Snapshot da VPS anexado: {added} item(ns); tamanho {len(payload) / (1024 * 1024):.2f} MB."


async def build_vps_snapshot_archive() -> tuple[bytes | None, str, str]:
    return await asyncio.to_thread(build_vps_snapshot_archive_sync)
