"""Resolução yt-dlp exposta pelo Phone Worker para o domínio de música."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .streams import register_stream, short_text

def resolve_ytdlp(body: dict[str, Any], *, job_timeout: int) -> dict[str, Any]:
    query = str(body.get("query") or body.get("url") or body.get("q") or "").strip()
    if not query:
        raise ValueError("query vazia")
    limit = max(1, min(10, int(float(body.get("limit") or body.get("max_results") or 5))))
    timeout = max(5, min(job_timeout, int(float(body.get("timeout_seconds") or min(job_timeout, 30)))))
    fmt = str(body.get("format") or os.getenv("PHONE_WORKER_MUSIC_YTDLP_FORMAT") or "bestaudio/best").strip() or "bestaudio/best"
    is_url = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", query) or query.lower().startswith("www."))
    metadata_only = str(body.get("metadata_only") if body.get("metadata_only") is not None else body.get("search_only") or "").strip().lower() in {"1", "true", "yes", "y", "on", "sim"}
    allow_playlist = str(body.get("allow_playlist") or "").strip().lower() in {"1", "true", "yes", "y", "on", "sim"}

    def _default_search_prefix() -> str:
        raw = str(
            body.get("default_search")
            or os.getenv("PHONE_WORKER_MUSIC_YTDLP_DEFAULT_SEARCH")
            or os.getenv("MUSIC_WORKER_YTDLP_DEFAULT_SEARCH")
            or "ytsearch"
        ).strip().lower()
        raw = raw.rstrip(":")
        if raw in {"ytsearch", "ytsearchdate", "ytsearchall", "ytmsearch"}:
            raw = f"{raw}{limit}"
        if not raw:
            raw = f"ytsearch{limit}"
        return raw

    default_search = _default_search_prefix()
    if is_url or query.lower().startswith(("ytsearch:", "ytsearch", "ytmsearch:")):
        target = query
    else:
        # A pesquisa textual precisa ser explícita. Sem isso, o YouTube pode
        # interpretar "megalovania" como ID/URL e retornar "Video unavailable".
        target = f"{default_search}:{query}"

    try:
        import yt_dlp  # type: ignore
    except Exception as exc:
        raise RuntimeError("yt-dlp não está instalado no phone worker") from exc

    def _split_csv(value: Any) -> list[str]:
        items: list[str] = []
        for part in re.split(r"[,;\s]+", str(value or "")):
            clean = part.strip()
            if clean:
                items.append(clean)
        return items

    def _safe_url_for_log(value: str) -> str:
        if not value:
            return ""
        try:
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme and parsed.netloc:
                return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path[:80], "", ""))
        except Exception:
            pass
        return short_text(value, limit=160)

    def select_stream(entry: dict[str, Any]) -> str:
        for item in entry.get("requested_downloads") or []:
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                if url.startswith(("http://", "https://")):
                    return url
        url = str(entry.get("url") or "").strip()
        if url.startswith(("http://", "https://")) and "youtube.com/watch" not in url and "youtu.be/" not in url:
            return url
        best_url = ""
        best_score = -1.0
        for fmt_item in entry.get("formats") or []:
            if not isinstance(fmt_item, dict):
                continue
            candidate = str(fmt_item.get("url") or "").strip()
            if not candidate.startswith(("http://", "https://")):
                continue
            acodec = str(fmt_item.get("acodec") or "").lower()
            vcodec = str(fmt_item.get("vcodec") or "").lower()
            if acodec in {"", "none"}:
                continue
            score = float(fmt_item.get("abr") or fmt_item.get("tbr") or 0)
            if vcodec in {"", "none"}:
                score += 10000
            if score > best_score:
                best_score = score
                best_url = candidate
        return best_url

    configured_cookies = str(
        os.getenv("PHONE_WORKER_MUSIC_YTDLP_COOKIES_FILE")
        or os.getenv("MUSIC_WORKER_YTDLP_COOKIES_FILE")
        or os.getenv("MUSIC_YTDLP_COOKIES_FILE")
        or os.getenv("YTDLP_COOKIES_FILE")
        or ""
    ).strip()
    default_cookies = Path.home() / "phone-worker" / "secrets" / "youtube-cookies.txt"
    cookies_path = Path(configured_cookies).expanduser() if configured_cookies else default_cookies
    cookies_ok = bool(cookies_path.exists() and cookies_path.is_file() and cookies_path.stat().st_size > 0)

    js_runtime_raw = str(
        body.get("js_runtimes")
        or body.get("js_runtime")
        or os.getenv("PHONE_WORKER_MUSIC_YTDLP_JS_RUNTIMES")
        or os.getenv("MUSIC_WORKER_YTDLP_JS_RUNTIMES")
        or "node"
    ).strip()
    js_runtimes = _split_csv(js_runtime_raw)
    remote_components = str(
        body.get("remote_components")
        or os.getenv("PHONE_WORKER_MUSIC_YTDLP_REMOTE_COMPONENTS")
        or os.getenv("MUSIC_WORKER_YTDLP_REMOTE_COMPONENTS")
        or ""
    ).strip()

    ydl_opts: dict[str, Any] = {
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": not allow_playlist,
        "ignoreerrors": True,
        "socket_timeout": max(3, min(20, timeout - 1)),
        "retries": max(0, int(float(os.getenv("PHONE_WORKER_MUSIC_YTDLP_RETRIES", "1") or 1))),
        "fragment_retries": max(0, int(float(os.getenv("PHONE_WORKER_MUSIC_YTDLP_FRAGMENT_RETRIES", "1") or 1))),
        "cachedir": str(Path(os.getenv("PHONE_WORKER_MUSIC_YTDLP_CACHE_DIR") or str(Path.home() / "phone-worker" / "cache" / "yt-dlp")).expanduser()),
    }
    if cookies_ok:
        ydl_opts["cookiefile"] = str(cookies_path)
    if metadata_only:
        # Busca textual leve: retorna 5 candidatos sem resolver stream_url, sem
        # ffmpeg e sem abrir endpoint de áudio. A resolução pesada acontece
        # apenas depois que o usuário escolhe uma faixa.
        ydl_opts["extract_flat"] = "in_playlist"
        ydl_opts.pop("format", None)

    # O suporte Python para js_runtimes pode variar por versão do yt-dlp.
    # A chamada via API continua rápida quando funcionar; se retornar vazio
    # por challenge/EJS, o fallback CLI abaixo usa exatamente os flags que
    # funcionaram no Termux: --js-runtimes node + ytsearch1:<query>.
    started = time.time()
    info: Any = None
    api_error = ""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as exc:
        api_error = f"{type(exc).__name__}: {short_text(exc, limit=220)}"
        info = None

    entries: list[Any]
    if isinstance(info, dict) and isinstance(info.get("entries"), list):
        entries = list(info.get("entries") or [])
        playlist_title = str(info.get("title") or "")
        is_playlist = True
    elif info is not None:
        entries = [info]
        playlist_title = ""
        is_playlist = False
    else:
        entries = []
        playlist_title = ""
        is_playlist = False

    tracks: list[dict[str, Any]] = []

    def entry_webpage_url(entry: dict[str, Any], *, stream_url: str = "") -> str:
        webpage_url = str(entry.get("webpage_url") or entry.get("original_url") or entry.get("url") or "").strip()
        if webpage_url and webpage_url != stream_url and webpage_url.startswith(("http://", "https://")):
            return webpage_url
        entry_id = str(entry.get("id") or entry.get("display_id") or "").strip()
        source_key = str(entry.get("extractor_key") or entry.get("extractor") or "").lower()
        if entry_id and ("youtube" in source_key or target.lower().startswith(("ytsearch", "ytmsearch"))):
            return f"https://www.youtube.com/watch?v={entry_id}"
        return str(entry.get("webpage_url_basename") or entry.get("display_id") or query).strip() or query

    def append_metadata_track(entry: dict[str, Any], *, source_label: str | None = None) -> bool:
        if not isinstance(entry, dict):
            return False
        webpage_url = entry_webpage_url(entry)
        if not webpage_url:
            return False
        title_value = entry.get("title") or entry.get("fulltitle") or entry.get("alt_title") or ""
        title = short_text(title_value, limit=160, default=query)
        uploader = short_text(entry.get("uploader") or entry.get("channel") or entry.get("creator") or entry.get("artist") or "", limit=120)
        source_value = source_label or entry.get("extractor_key") or entry.get("extractor") or "worker-ytdlp-search"
        tracks.append({
            "title": title,
            "uploader": uploader,
            "duration": entry.get("duration"),
            "thumbnail": short_text(entry.get("thumbnail") or "", limit=500),
            "webpage_url": webpage_url,
            "original_url": webpage_url,
            "original_query": query,
            "source": short_text(source_value, limit=80, default="worker-ytdlp-search"),
            "extractor": "worker-ytdlp",
            "is_live": bool(entry.get("is_live")),
            "metadata_only": True,
            "search_only": True,
            "is_direct_stream": False,
        })
        return True

    def append_entry_track(entry: dict[str, Any], *, source_label: str | None = None) -> bool:
        if not isinstance(entry, dict):
            return False
        if metadata_only:
            return append_metadata_track(entry, source_label=source_label)
        stream_url = select_stream(entry)
        if not stream_url:
            return False
        webpage_url = entry_webpage_url(entry, stream_url=stream_url)
        title_value = entry.get("title") or entry.get("fulltitle") or entry.get("alt_title") or ""
        title = short_text(title_value, limit=160, default=(query if not is_url else "Música"))
        uploader = short_text(entry.get("uploader") or entry.get("channel") or entry.get("creator") or entry.get("artist") or "", limit=120)
        source_value = source_label or entry.get("extractor_key") or entry.get("extractor") or "worker-ytdlp"
        track_payload = {
            "title": title,
            "uploader": uploader,
            "duration": entry.get("duration"),
            "thumbnail": short_text(entry.get("thumbnail") or "", limit=500),
            "webpage_url": webpage_url,
            "original_url": webpage_url,
            "original_query": query,
            "stream_url": stream_url,
            "direct_url": stream_url,
            "source": short_text(source_value, limit=80, default="worker-ytdlp"),
            "extractor": "worker-ytdlp",
            "is_live": bool(entry.get("is_live")),
            "ext": short_text(entry.get("ext") or "", limit=20),
            "format_id": short_text(entry.get("format_id") or "", limit=80),
            "http_headers": entry.get("http_headers") if isinstance(entry.get("http_headers"), dict) else {},
            "is_direct_stream": True,
        }
        stream_id = register_stream(track_payload)
        if stream_id:
            track_payload["worker_stream_id"] = stream_id
            track_payload["worker_stream_path"] = f"/music/stream/{stream_id}"
            track_payload["worker_stream_transport"] = "pcm_s16le_48k_stereo"
        tracks.append(track_payload)
        return True

    for entry in entries:
        append_entry_track(entry)
        if len(tracks) >= limit:
            break

    cli_stderr = ""
    cli_rc: int | None = None
    if not tracks:
        cmd_json = [shutil.which("python") or "python", "-m", "yt_dlp"]
        if cookies_ok:
            cmd_json += ["--cookies", str(cookies_path)]
        if js_runtimes:
            cmd_json += ["--js-runtimes", ",".join(js_runtimes)]
        if remote_components:
            cmd_json += ["--remote-components", remote_components]
        if metadata_only:
            cmd_json += ["--flat-playlist"]
        cmd_json += [
            "--no-warnings",
            "--socket-timeout",
            str(max(3, min(20, timeout - 1))),
        ]
        if not allow_playlist:
            cmd_json += ["--no-playlist"]
        if not metadata_only:
            cmd_json += ["-f", fmt]
        cmd_json += ["-J", target]
        try:
            proc_json = subprocess.run(
                cmd_json,
                cwd=str(Path.home() / "phone-worker"),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            cli_rc = int(proc_json.returncode)
            cli_stderr = short_text(proc_json.stderr or "", limit=800)
            parsed: Any = json.loads(proc_json.stdout or "{}") if proc_json.stdout else {}
            json_entries: list[Any]
            if isinstance(parsed, dict) and isinstance(parsed.get("entries"), list):
                json_entries = list(parsed.get("entries") or [])
                playlist_title = playlist_title or str(parsed.get("title") or "")
                is_playlist = True
            elif isinstance(parsed, dict) and parsed:
                json_entries = [parsed]
            else:
                json_entries = []
            for entry in json_entries:
                if isinstance(entry, dict):
                    append_entry_track(entry, source_label="worker-ytdlp-cli-json")
                if len(tracks) >= limit:
                    break
        except Exception as exc:
            cli_stderr = f"{type(exc).__name__}: {short_text(exc, limit=500)}"

    if not tracks and not metadata_only:
        cmd = [shutil.which("python") or "python", "-m", "yt_dlp"]
        if cookies_ok:
            cmd += ["--cookies", str(cookies_path)]
        for runtime in js_runtimes:
            # yt-dlp aceita lista separada por vírgula em uma única opção.
            # Mantemos uma opção para todos os runtimes para preservar sintaxe CLI.
            pass
        if js_runtimes:
            cmd += ["--js-runtimes", ",".join(js_runtimes)]
        if remote_components:
            cmd += ["--remote-components", remote_components]
        cmd += [
            "--no-warnings",
            "--socket-timeout",
            str(max(3, min(20, timeout - 1))),
        ]
        if not allow_playlist:
            cmd += ["--no-playlist"]
        cmd += [
            "-f",
            fmt,
            "-g",
            target,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(Path.home() / "phone-worker"),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            cli_rc = int(proc.returncode)
            cli_stderr = short_text(proc.stderr or "", limit=800)
            urls = [
                line.strip()
                for line in (proc.stdout or "").splitlines()
                if line.strip().startswith(("http://", "https://"))
            ]
            for idx, stream_url in enumerate(urls[:limit], start=1):
                track_payload = {
                    "title": short_text(query if not is_url else "Música", limit=160, default="Música"),
                    "uploader": "",
                    "duration": None,
                    "thumbnail": "",
                    "webpage_url": query,
                    "original_url": query,
                    "original_query": query,
                    "stream_url": stream_url,
                    "direct_url": stream_url,
                    "source": "worker-ytdlp-cli",
                    "extractor": "worker-ytdlp",
                    "is_live": False,
                    "ext": "",
                    "format_id": "",
                    "http_headers": {},
                    "is_direct_stream": True,
                }
                stream_id = register_stream(track_payload)
                if stream_id:
                    track_payload["worker_stream_id"] = stream_id
                    track_payload["worker_stream_path"] = f"/music/stream/{stream_id}"
                    track_payload["worker_stream_transport"] = "pcm_s16le_48k_stereo"
                tracks.append(track_payload)
        except Exception as exc:
            cli_stderr = f"{type(exc).__name__}: {short_text(exc, limit=500)}"

    if not tracks:
        reason = cli_stderr or api_error or "yt-dlp não retornou URL de áudio"
        print(
            "[music-ytdlp] tracks=0 "
            f"target={short_text(target, limit=120)!r} cookies={'on' if cookies_ok else 'off'} "
            f"js={','.join(js_runtimes) or 'off'} rc={cli_rc} erro={short_text(reason, limit=240)}",
            flush=True,
        )

    return {
        "ok": True,
        "summary": ("busca leve resolvida pelo yt-dlp no worker" if metadata_only and tracks else "música resolvida pelo yt-dlp no worker" if tracks else "yt-dlp não encontrou áudio tocável no worker"),
        "query": query,
        "target": target,
        "tracks": tracks,
        "tracks_found": len(tracks),
        "is_playlist": is_playlist,
        "playlist_title": playlist_title,
        "truncated": bool(len(entries) > len(tracks)),
        "metadata_only": bool(metadata_only),
        "allow_playlist": bool(allow_playlist),
        "elapsed_ms": round((time.time() - started) * 1000.0, 1),
        "cookies": "on" if cookies_ok else "off",
        "js_runtime": ",".join(js_runtimes) if js_runtimes else "",
        "default_search": default_search,
        "api_error": short_text(api_error, limit=240),
        "cli_rc": cli_rc,
        "cli_error": short_text(cli_stderr, limit=240),
    }
