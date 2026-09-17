"""PCM IO without runtime state, background work or provider imports at import.

The facade owns stream/preparation maps, locks, TTL and publication authority.
Callbacks and IO bindings are supplied per call; each process belongs to its call.
"""
from __future__ import annotations

import contextlib
from http import HTTPStatus
from typing import Any


def build_ffmpeg_input_cmd(item: dict[str, Any], *, output: str, which, header_lines) -> list[str]:
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg não encontrado no worker")
    stream_url = str(item.get("stream_url") or item.get("direct_url") or "").strip()
    if not stream_url.startswith(("http://", "https://")):
        raise ValueError("stream inválido")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_at_eof",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "403,404,408,429,5xx",
        "-reconnect_delay_max",
        "5",
        "-rw_timeout",
        "10000000",
    ]
    ff_headers = header_lines(item.get("http_headers"))
    if ff_headers:
        cmd += ["-headers", ff_headers]
    cmd += [
        "-i",
        stream_url,
        "-vn",
        "-sn",
        "-dn",
        "-f",
        "s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        output,
    ]
    return cmd


def prepare_file(stream_id: str, item: dict[str, Any], *, cleanup_cache, cache_dir,
                 max_duration_seconds, timeout_seconds, build_command, wall_time,
                 temporary_directory, path_type, subprocess_api, publish, short_text) -> dict[str, Any]:
    """Transcode to private staging; publish only while the original owner is live."""
    prepared_path = str(item.get("prepared_pcm_path") or "").strip()
    if prepared_path:
        p = path_type(prepared_path)
        if p.exists() and p.is_file() and p.stat().st_size > 0:
            return item

    duration = float(item.get("duration") or 0.0)
    max_duration = max_duration_seconds()
    if max_duration > 0 and duration > max_duration:
        raise TimeoutError(f"faixa muito longa para cache completo no worker ({duration:.0f}s > {max_duration:.0f}s)")

    cleanup_cache()
    directory = cache_dir()
    out_path = directory / f"{stream_id}.pcm"
    with temporary_directory(prefix=f".{stream_id}-", dir=str(directory)) as staging:
        tmp_path = path_type(staging) / "audio.pcm"
        timeout = timeout_seconds(item)
        cmd = build_command(item, output=str(tmp_path))
        started = wall_time()
        print(
            f"[music-stream] cache_started id={stream_id} title={short_text(item.get('title'), limit=80)!r} duration={duration:.1f}s timeout={timeout:.1f}s",
            flush=True,
        )
        proc = subprocess_api.run(
            cmd,
            stdout=subprocess_api.DEVNULL,
            stderr=subprocess_api.PIPE,
            cwd=str(path_type.home() / "phone-worker"),
            timeout=timeout,
        )
        elapsed = wall_time() - started
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace") if isinstance(proc.stderr, (bytes, bytearray)) else str(proc.stderr or "")
            raise RuntimeError(f"ffmpeg cache falhou rc={proc.returncode}: {short_text(stderr, limit=220)}")
        if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            raise RuntimeError("ffmpeg não gerou PCM no worker")
        size = tmp_path.stat().st_size
        prepared_seconds = size / 192000.0
        updated = dict(item)
        updated["prepared_pcm_path"] = str(out_path)
        updated["prepared_pcm_bytes"] = size
        updated["prepared_pcm_seconds"] = prepared_seconds
        updated["prepared_at"] = wall_time()
        updated["stream_mode"] = "prepared_pcm"
        publish(tmp_path, out_path, updated)
    print(
        f"[music-stream] cache_ready id={stream_id} bytes={size} seconds={prepared_seconds:.1f} elapsed={elapsed:.1f}s",
        flush=True,
    )
    cleanup_cache()
    return updated


def serve_prepared(handler, stream_id: str, item: dict[str, Any], *, path_type, fstat, frame_bytes, short_text) -> None:
    path = path_type(str(item.get("prepared_pcm_path") or ""))
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("PCM preparado não encontrado")
    with path.open("rb") as fh:
        size = fstat(fh.fileno()).st_size
        seconds = f"{float(item.get('prepared_pcm_seconds') or 0.0):.3f}"
        try:
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", str(size))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Core-Worker-Stream-Id", stream_id)
            handler.send_header("X-Core-Worker-Stream-Mode", "prepared-pcm")
            handler.send_header("X-Core-Worker-Prepared-Seconds", seconds)
            handler.end_headers()
            while True:
                chunk = fh.read(frame_bytes * 64)
                if not chunk:
                    break
                handler.wfile.write(chunk)
            with contextlib.suppress(Exception):
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            handler.close_connection = True
        except Exception as exc:
            handler.close_connection = True
            print(f"[music-stream] prepared_failed id={stream_id} erro={type(exc).__name__}: {short_text(exc, limit=160)}", flush=True)


def stream_live(handler, stream_id: str, item: dict[str, Any], *, build_command,
                subprocess_api, path_type, frame_bytes, send_error, short_text):
    proc: subprocess_api.Popen[bytes] | None = None
    headers_sent = False
    try:
        cmd = build_command(item, output="pipe:1")
        proc = subprocess_api.Popen(cmd, stdout=subprocess_api.PIPE, stderr=subprocess_api.DEVNULL, cwd=str(path_type.home() / "phone-worker"))
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Core-Worker-Stream-Id", stream_id)
        handler.send_header("X-Core-Worker-Stream-Mode", "live-pcm")
        headers_sent = True
        handler.end_headers()
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(frame_bytes * 16)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
        with contextlib.suppress(Exception):
            handler.wfile.flush()
    except Exception as exc:
        try:
            if not headers_sent:
                send_error(handler, HTTPStatus.INTERNAL_SERVER_ERROR, f"stream falhou: {type(exc).__name__}")
        except Exception:
            pass
        print(f"[music-stream] falhou id={stream_id} erro={type(exc).__name__}: {short_text(exc, limit=160)}", flush=True)
    finally:
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    with contextlib.suppress(Exception):
                        pipe.close()
