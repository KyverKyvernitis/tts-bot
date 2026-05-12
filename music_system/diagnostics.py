from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import traceback
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
        store = getattr(getattr(router, "backends", None), "lavalink_store", None)
        if store is not None:
            cfg = store.load(guild_id=guild_id)
            cfg_data = {
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
                cfg_data["summary"] = store.summary(guild_id=guild_id)
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
    if not base_url or not password:
        return "Lavalink não configurado ou senha ausente."
    lines: list[str] = []
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
    log_dir = REPO_ROOT / "logs"
    if not log_dir.exists():
        return "Pasta logs/ não existe."
    parts: list[str] = []
    for path in sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:4]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tail = "\n".join(text.splitlines()[-180:])
            parts.append(f"===== {path.relative_to(REPO_ROOT)} =====\n{tail}")
        except Exception as exc:
            parts.append(f"===== {path} =====\nERRO: {type(exc).__name__}: {exc}")
    return "\n\n".join(parts) if parts else "Nenhum .log em logs/."


def _journalctl_tail() -> str:
    commands = [
        ["journalctl", "-u", "tts-bot.service", "--since", "20 minutes ago", "-n", "450", "--no-pager", "-o", "cat"],
        ["journalctl", "-u", "lavalink.service", "--since", "20 minutes ago", "-n", "450", "--no-pager", "-o", "cat"],
        ["journalctl", "-u", "nodelink.service", "--since", "20 minutes ago", "-n", "220", "--no-pager", "-o", "cat"],
    ]
    parts = []
    for cmd in commands:
        out = _run_cmd(cmd, timeout=10.0, cwd=REPO_ROOT)
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
    sections.append(("Teste Spotify API do bot", _spotify_api_test()))
    sections.append(("Testes Lavalink REST", _lavalink_tests(cfg)))
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
