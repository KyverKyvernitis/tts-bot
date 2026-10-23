"""Resolvedor yt-dlp quente para o Music Agent.

Mantém um processo auxiliar isolado com o módulo yt-dlp já importado. O processo
é serial por design: o scheduler do Music Agent continua controlando a
concorrência, enquanto cancelamento/timeout podem matar o helper sem deixar uma
thread Python presa dentro do extrator.
"""
from __future__ import annotations

import json
import os
import selectors
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


def _split_csv(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").replace(";", ",").split(",") if part.strip()]


def _enable_domain_path() -> None:
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / "cogs" / "musica" / "runtime_telefone").is_dir():
            text = str(candidate)
            if text not in sys.path:
                sys.path.insert(0, text)
            return


def _worker_options(yt_dlp: Any, request: dict[str, Any]) -> dict[str, Any]:
    args = [
        "--ignore-config",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        str(max(3, min(20, int(float(request.get("socket_timeout") or 12))))),
        "-f",
        str(request.get("format") or "bestaudio/best"),
        "--format-sort",
        str(request.get("format_sort") or "abr,acodec,asr"),
    ]
    cookiefile = str(request.get("cookiefile") or "").strip()
    if cookiefile and Path(cookiefile).is_file():
        args += ["--cookies", cookiefile]
    for runtime in _split_csv(request.get("js_runtimes")):
        args += ["--js-runtimes", runtime]
    options = dict(yt_dlp.parse_options(args).ydl_opts)
    options.update({
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    })
    return options


def _worker_main() -> int:
    _enable_domain_path()
    import_started = time.perf_counter()
    try:
        import yt_dlp  # type: ignore
        from cogs.musica.runtime_telefone.agente.utilitarios import select_stream_info, select_playback_stream
        from cogs.musica.runtime_telefone.agente.correspondencia import avaliar_correspondencia
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "fatal": True, "error": f"{type(exc).__name__}: {exc}"}) + "\n")
        sys.stdout.flush()
        return 2

    unreported_import_ms = (time.perf_counter() - import_started) * 1000.0
    instance: Any = None
    signature = ""
    for raw in sys.stdin:
        try:
            request = json.loads(raw)
            request_id = int(request.get("id") or 0)
            action = str(request.get("action") or "resolve")
            target = str(request.get("target") or "").strip()
            if action not in {"initialize", "resolve"}:
                raise ValueError("ação inválida")
            if action == "resolve" and not target:
                raise ValueError("target vazio")
            init_started = time.perf_counter()
            options = _worker_options(yt_dlp, request)
            next_signature = json.dumps(
                {
                    "format": options.get("format"),
                    "format_sort": options.get("format_sort"),
                    "cookiefile": options.get("cookiefile"),
                    "js_runtimes": str(request.get("js_runtimes") or ""),
                    "socket_timeout": options.get("socket_timeout"),
                },
                sort_keys=True,
                default=str,
            )
            if instance is None or next_signature != signature:
                close = getattr(instance, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                instance = yt_dlp.YoutubeDL(options)
                signature = next_signature
            init_ms = round(unreported_import_ms + (time.perf_counter() - init_started) * 1000.0, 1)
            unreported_import_ms = 0.0
            if action == "initialize":
                # Somente importação/opções/instância. Nunca extraia uma URL
                # fictícia para aquecer: isso faria rede e alteraria o cache.
                sys.stdout.write(json.dumps({"ok": True, "id": request_id, "ready": True, "init_ms": init_ms}) + "\n")
                sys.stdout.flush()
                continue
            started = time.perf_counter()
            info = instance.extract_info(target, download=False)
            if isinstance(info, dict) and isinstance(info.get("entries"), list):
                entries = [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]
                expected = request.get("expected_metadata")
                if isinstance(expected, dict):
                    candidates = []
                    for entry in entries:
                        valid, score, _reason = avaliar_correspondencia(expected, entry)
                        if valid and select_stream_info(entry).get("stream_url"):
                            candidates.append((score, entry))
                    if not candidates:
                        raise RuntimeError("nenhuma fonte encontrada corresponde à faixa solicitada")
                    info = max(candidates, key=lambda item: item[0])[1]
                else:
                    info = next(iter(entries), {})
            if not isinstance(info, dict) or not info:
                raise RuntimeError("yt-dlp não retornou mídia")
            stream = select_playback_stream(
                info, format_selector=str(request.get("format") or ""),
                format_sort=str(request.get("format_sort") or ""),
            )
            if not stream.get("stream_url"):
                raise RuntimeError("yt-dlp não retornou stream_url")
            payload = {
                "ok": True,
                "id": request_id,
                "init_ms": init_ms,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
                "title": info.get("title") or info.get("fulltitle") or target,
                "uploader": info.get("uploader") or info.get("channel") or info.get("creator") or "",
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail") or "",
                "is_live": bool(info.get("is_live")),
                "webpage_url": info.get("webpage_url") or info.get("original_url") or target,
                **stream,
            }
        except Exception as exc:
            payload = {
                "ok": False,
                "id": int(request.get("id") or 0) if isinstance(locals().get("request"), dict) else 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    close = getattr(instance, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
    return 0


class WarmYTDLPResolver:
    """Cliente síncrono para o helper quente, seguro para uma chamada por vez."""

    def __init__(self, *, script_path: str | None = None) -> None:
        self.script_path = str(script_path or Path(__file__).resolve())
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._sequence = 0
        self._state_lock = threading.Lock()
        self._prewarm_event: threading.Event | None = None
        self._prewarming = False
        self._prewarm_ok = False
        self._initialized = False
        self._closing = threading.Event()

    @property
    def state(self) -> str:
        with self._state_lock:
            if self._closing.is_set():
                return "closed"
            if self._prewarming:
                return "warming"
            proc = self._process
            return "warm" if self._initialized and proc is not None and proc.poll() is None else "cold"

    def _stop_locked(self) -> None:
        proc, self._process = self._process, None
        self._initialized = False
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=0.4)
            except Exception:
                try:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    pass
            # SIGKILL precisa ser recolhido antes de outro helper ocupar a vaga.
            try:
                proc.wait(timeout=0.6)
            except Exception:
                pass
        if os.name == "posix":
            # O helper pode já ter saído e deixado um runtime JavaScript no
            # seu grupo. Ele continua pertencendo a esta geração do helper.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def close(self) -> None:
        # Interrompe também uma inicialização/leitura que atualmente possui o
        # lock. O chamador async executa este cleanup fora do event loop.
        self._closing.set()
        with self._lock:
            self._stop_locked()

    def _ensure_locked(self) -> subprocess.Popen[str]:
        proc = self._process
        if proc is not None and proc.poll() is None and proc.stdin is not None and proc.stdout is not None:
            return proc
        self._stop_locked()
        worker_cwd = Path.home() / "phone-worker"
        proc = subprocess.Popen(
            [shutil.which("python") or sys.executable or "python", self.script_path, "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=os.name == "posix",
            cwd=str(worker_cwd) if worker_cwd.is_dir() else None,
        )
        self._process = proc
        return proc

    @staticmethod
    def _options_request(*, format_selector: str, format_sort: str, cookiefile: str,
                         js_runtimes: str, socket_timeout: int) -> dict[str, Any]:
        return {"format": format_selector, "format_sort": format_sort, "cookiefile": cookiefile,
                "js_runtimes": js_runtimes, "socket_timeout": socket_timeout}

    def _exchange_locked(self, request: dict[str, Any], *, deadline: float,
                         cancel_event: threading.Event | None = None) -> dict[str, Any] | None:
        if self._closing.is_set() or time.monotonic() >= deadline:
            return None
        proc = self._ensure_locked()
        assert proc.stdin is not None and proc.stdout is not None
        self._sequence += 1
        request_id = self._sequence
        request = dict(request, id=request_id)
        proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        pending = bytearray()
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._stop_locked()
                    raise RuntimeError("resolução yt-dlp cancelada")
                remaining = deadline - time.monotonic()
                if self._closing.is_set() or remaining <= 0 or proc.poll() is not None:
                    self._stop_locked()
                    return None
                if not selector.select(timeout=min(0.1, remaining)):
                    continue
                # readline() poderia esperar para sempre por '\n' após um
                # helper travado escrever só metade da resposta.
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    self._stop_locked()
                    return None
                pending.extend(chunk)
                if len(pending) > 1024 * 1024:
                    self._stop_locked()
                    return None
                if b"\n" not in pending:
                    continue
                line, extra = bytes(pending).split(b"\n", 1)
                if extra.strip():
                    self._stop_locked()
                    return None
                response = json.loads(line)
                if int(response.get("id") or 0) != request_id:
                    self._stop_locked()
                    return None
                if response.get("ok") is not True:
                    return None
                self._initialized = True
                return dict(response)
        finally:
            selector.close()

    def prewarm(self, *, format_selector: str, format_sort: str = "abr,acodec,asr",
                cookiefile: str = "", js_runtimes: str = "", socket_timeout: int = 12,
                timeout: float = 5.0) -> bool:
        """Inicialização local compartilhada; não ocupa uma fila de resolução."""
        deadline = time.monotonic() + max(0.05, min(5.0, float(timeout)))
        with self._state_lock:
            if self._closing.is_set() or self._prewarm_event is not None:
                return False
            # Se um play já possui o helper, ele está fazendo a inicialização
            # necessária. Não o atrase nem inicie outro processo.
            if not self._lock.acquire(blocking=False):
                return False
            event = self._prewarm_event = threading.Event()
            self._prewarming = True
        ok = False
        try:
            response = self._exchange_locked(
                dict(self._options_request(format_selector=format_selector, format_sort=format_sort,
                                           cookiefile=cookiefile, js_runtimes=js_runtimes,
                                           socket_timeout=socket_timeout), action="initialize"),
                deadline=deadline,
            )
            ok = bool(response and response.get("ready") is True)
            return ok
        except Exception:
            return False
        finally:
            if not ok:
                self._stop_locked()
            # Publique prontidão somente após terminar toda a leitura/limpeza.
            self._lock.release()
            with self._state_lock:
                self._prewarm_ok = ok
                self._prewarming = False
                event.set()

    def resolve(
        self,
        target: str,
        *,
        format_selector: str,
        format_sort: str = "abr,acodec,asr",
        cookiefile: str = "",
        js_runtimes: str = "",
        socket_timeout: int = 12,
        timeout: float = 10.0,
        cancel_event: threading.Event | None = None,
        expected_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.5, float(timeout))
        with self._state_lock:
            prewarm = self._prewarm_event if self._prewarming else None
        if prewarm is not None:
            # Inicialização compartilhada não é outra resolução. Uma chamada
            # cancelada não encerra o aquecimento que pode servir outra call.
            while not prewarm.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("resolução yt-dlp cancelada")
                remaining = deadline - time.monotonic()
                if self._closing.is_set() or remaining <= 0:
                    return None
                prewarm.wait(min(0.05, remaining))
            if not self._prewarm_ok:
                return None
        # Outra resolução ocupada mantém o fallback imediato existente.
        if self._closing.is_set():
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            request = dict(self._options_request(format_selector=format_selector, format_sort=format_sort,
                                                 cookiefile=cookiefile, js_runtimes=js_runtimes,
                                                 socket_timeout=socket_timeout), target=target)
            if expected_metadata is not None:
                request["expected_metadata"] = dict(expected_metadata)
            return self._exchange_locked(request, deadline=deadline, cancel_event=cancel_event)
        except RuntimeError:
            raise
        except Exception:
            self._stop_locked()
            return None
        finally:
            self._lock.release()


if __name__ == "__main__" and "--worker" in sys.argv:
    raise SystemExit(_worker_main())
