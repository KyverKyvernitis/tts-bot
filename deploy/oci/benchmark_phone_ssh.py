#!/usr/bin/env python3
"""Measure the existing Phone Worker HTTP transport without provider spam.

This program runs on the VPS. Default probes are read-only. Audio probes require
the advertised cache-only streaming protocol; a cache miss never permits a
provider request. --warm-audio explicitly permits one Edge-only seed request.
The temporary baseline is optional: production SSH does not depend on a VPN.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 256 * 1024
SCHEMA = "osaka-phone-ssh-benchmark-v1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_env(path):
    result = {}
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return result
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if match:
            result[match[1]] = match[2].strip().strip("\"'")
    return result


def clean(value, secrets=()):
    if isinstance(value, dict):
        return {key: ("[OCULTO]" if re.search(r"token|password|secret|authorization|cookie|credential|api.?key", str(key), re.I)
                      else clean(item, secrets)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in sorted((str(x) for x in secrets if x), key=len, reverse=True):
        value = value.replace(secret, "[OCULTO]")
    value = re.sub(r"(?i)Bearer\s+[^\s\"'<>]+", "Bearer [OCULTO]", value)
    return re.sub(r"(?i)((?:token|password|secret|api_key|authorization|cookie)[\w-]*[\"']?\s*[:=]\s*)[^\s,}\r\n]+",
                  r"\1[OCULTO]", value)


def validate_base(value, *, ssh=False):
    parsed = urllib.parse.urlsplit(str(value).strip())
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("porta inválida no endereço do worker") from None
    host = parsed.hostname or ""
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("use somente http(s)://host:porta, sem credenciais, caminho ou parâmetros")
    try:
        address = ipaddress.ip_address(host)
        loopback = address.is_loopback
        private = address.is_private or address.is_loopback or (address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10"))
    except ValueError:
        loopback = host.lower() == "localhost"
        private = loopback or host.lower().endswith(".ts.net")
    if ssh and not loopback:
        raise ValueError("--ssh-base precisa apontar para o encaminhamento local da VPS")
    if parsed.scheme == "http" and not private:
        raise ValueError("HTTP sem TLS é aceito somente em endereço privado, loopback ou Tailscale")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    return round(ordered[max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))], 3)


def summarize(samples, field):
    values = [row[field] for row in samples if row.get("ok") and isinstance(row.get(field), (int, float))]
    return {"count": len(values), "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95)}


def probe(spec, *, opener=None, clock=time.monotonic):
    """One bounded child-process request; no implicit retry or redirect."""
    started = clock()
    out = {"ok": False, "kind": spec["kind"]}
    headers = {"Authorization": "Bearer " + spec["auth"], "Accept": "application/json"}
    data = None
    if spec["kind"] == "health":
        endpoint = "/tts-agent/health"
    else:
        endpoint = "/tts-agent/synthesize.stream"
        headers["Accept"] = "audio/mpeg"
        headers["Content-Type"] = "application/json"
        body = {
            "engine": "edge", "preferred_engine": "edge", "fallback_engine": "edge",
            "voice": spec.get("voice", "pt-BR-FranciscaNeural"), "language": "pt",
            "text": spec["text"], "rate": "+0%", "pitch": "+0Hz",
            "cache_mode": "prefer" if spec["kind"] == "warm" else "only",
            "max_audio_bytes": MAX_AUDIO_BYTES, "timeout_seconds": spec["timeout"],
        }
        if spec.get("cache_key"):
            body["cache_key"] = spec["cache_key"]
        data = json.dumps(body).encode()
    request = urllib.request.Request(spec["base"] + endpoint, data=data, headers=headers)
    transport = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with transport.open(request, timeout=spec["timeout"]) as response:
            out["http_status"] = int(response.status)
            out["headers_ms"] = round((clock() - started) * 1000, 3)
            if response.status != 200:
                raise ValueError("worker não retornou HTTP 200")
            if spec["kind"] == "health":
                raw = response.read(MAX_JSON_BYTES + 1)
                if len(raw) > MAX_JSON_BYTES:
                    raise ValueError("health excedeu limite de resposta")
                body = json.loads(raw)
                agent = body.get("tts_agent") or {}
                if body.get("worker_id") != spec["worker_id"]:
                    raise ValueError("identidade do worker não corresponde ao esperado")
                if not body.get("ok") or not agent.get("ok"):
                    raise ValueError("worker/TTS não está pronto")
                if "edge" not in agent.get("available_engines", []):
                    raise ValueError("Edge não foi anunciado pelo worker")
                out.update(worker_id=body["worker_id"], version=body.get("version", ""),
                           cache_only_protocol=int(agent.get("stream_cache_only_protocol", 0) or 0),
                           stream_protocol=int(agent.get("stream_protocol", 0) or 0))
                for key in ("pid", "process_started_at", "uptime_seconds"):
                    if isinstance(body.get(key), (int, float)):
                        out[key] = body[key]
                connection = body.get("connection") or {}
                if isinstance(connection, dict) and isinstance(connection.get("generation"), (int, str)):
                    out["transport_generation"] = connection["generation"]
            else:
                worker_id = response.headers.get("X-Core-Worker-Id", "")
                engine = response.headers.get("X-Core-Worker-Engine", "").strip().lower()
                cache_hit = response.headers.get("X-Core-Worker-Cache-Hit", "").strip().lower() == "true"
                stream_protocol = response.headers.get("X-Core-Worker-Stream-Protocol", "")
                if worker_id != spec["worker_id"] or engine != "edge" or stream_protocol != "2":
                    raise ValueError("identidade, engine ou protocolo do áudio não corresponde ao esperado")
                if spec["kind"] != "warm" and not cache_hit:
                    raise ValueError("áudio não veio do cache; novas amostras serão interrompidas")
                first = response.read(1)
                out["first_audio_ms"] = round((clock() - started) * 1000, 3)
                if not first:
                    raise ValueError("resposta de áudio vazia")
                digest = hashlib.sha256(first)
                size = len(first)
                while True:
                    if clock() - started > spec["timeout"]:
                        raise TimeoutError("prazo da leitura do áudio excedido")
                    read = getattr(response, "read1", response.read)
                    chunk = read(min(16384, MAX_AUDIO_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise ValueError("áudio excedeu 2 MiB")
                    digest.update(chunk)
                out.update(worker_id=worker_id, engine=engine, cache_hit=cache_hit,
                           audio_bytes=size, sha256=digest.hexdigest())
            out["ok"] = True
    except urllib.error.HTTPError as exc:
        out["http_status"] = exc.code
        out["error"] = "HTTP " + str(exc.code)
        exc.close()
    except Exception as exc:
        out["error"] = type(exc).__name__ + ": " + str(exc)
    out["total_ms"] = round((clock() - started) * 1000, 3)
    return out


def child_probe(spec):
    """Enforce a wall deadline even if a peer trickles headers or body bytes."""
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--_probe"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", start_new_session=True)
    try:
        stdout, _stderr = process.communicate(json.dumps(spec), timeout=float(spec["timeout"]) + .75)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        return {"kind": spec["kind"], "ok": False, "error": "deadline do subprocesso excedido"}
    if process.returncode:
        return {"kind": spec["kind"], "ok": False, "error": "subprocesso de medição falhou"}
    try:
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except ValueError:
        return {"kind": spec["kind"], "ok": False, "error": "resposta inválida do subprocesso"}


def comparison(route_summaries, metric):
    ssh = route_summaries.get("ssh", {}).get(metric, {}).get("p95_ms")
    baseline = route_summaries.get("baseline", {}).get(metric, {}).get("p95_ms")
    if not isinstance(ssh, (int, float)) or not isinstance(baseline, (int, float)) or baseline <= 0:
        return {"comparable": False}
    gain = round((baseline - ssh) / baseline * 100, 3)
    return {"comparable": True, "ssh_gain_percent": gain,
            "target_10_percent_faster": gain >= 10, "no_regression_over_5_percent": gain >= -5}


def same_runtime(before, after):
    for key in ("version", "pid", "process_started_at", "transport_generation"):
        if key in before and key in after and before[key] != after[key]:
            return False
    previous_uptime, current_uptime = before.get("uptime_seconds"), after.get("uptime_seconds")
    if isinstance(previous_uptime, (int, float)) and isinstance(current_uptime, (int, float)):
        return current_uptime + .25 >= previous_uptime
    return True


def benchmark(options, token, worker_id, *, requester=child_probe, clock=time.monotonic):
    started = clock()
    deadline = started + options.total_seconds
    routes = {"ssh": validate_base(options.ssh_base, ssh=True)}
    if options.baseline_base:
        routes["baseline"] = validate_base(options.baseline_base)
    rows = {name: {"health": [], "audio": []} for name in routes}
    notes = []
    health = {}
    warm_result = None
    halted = False

    def request(route, kind):
        remaining = deadline - clock()
        if remaining <= .1:
            return {"kind": kind, "ok": False, "error": "prazo total da coleta esgotado"}
        spec = {"base": routes[route], "kind": kind, "auth": token, "worker_id": worker_id,
                "timeout": min(options.request_timeout, remaining), "voice": options.voice,
                "text": options.audio_text, "cache_key": options.cache_key}
        return requester(spec)

    for name in routes:
        result = request(name, "health")
        health[name] = result
        if not result.get("ok"):
            notes.append("Pré-verificação de " + name + " falhou; amostragem interrompida.")
            halted = True
    if len(health) == 2 and not same_runtime(health["ssh"], health["baseline"]):
        halted = True
        notes.append("As rotas não confirmaram a mesma versão/processo/geração; comparação interrompida.")
    previous_health = {name: dict(value) for name, value in health.items()}
    audio_enabled = bool(options.audio_text) and not halted
    if audio_enabled and any(item.get("cache_only_protocol", 0) < 1 for item in health.values()):
        audio_enabled = False
        notes.append("Áudio não medido: o worker não anunciou stream_cache_only_protocol=1. Nenhuma síntese foi solicitada.")
    if audio_enabled and options.warm_audio:
        warm_result = request("ssh", "warm")
        if not warm_result.get("ok"):
            audio_enabled = False
            notes.append("A única requisição de aquecimento Edge falhou; não haverá nova tentativa de síntese.")
    if not options.audio_text:
        notes.append("Coleta de health somente; informe --audio-text para testar áudio cacheado. --warm-audio permite uma única requisição de aquecimento.")

    audio_digest = None
    for index in range(options.samples if not halted else 0):
        # AB / BA: one sample per route per round; no concurrent overload.
        order = list(routes)
        if index % 2:
            order.reverse()
        for name in order:
            if clock() >= deadline:
                halted = True
                notes.append("Prazo total atingido; resultados parciais.")
                break
            result = request(name, "health")
            if result.get("ok") and not same_runtime(previous_health[name], result):
                result["ok"] = False
                result["error"] = "processo, versão ou geração da conexão mudou durante a coleta"
            if result.get("ok"):
                previous_health[name] = {**previous_health[name], **result}
            rows[name]["health"].append(result)
            if not result.get("ok"):
                halted = True
                notes.append("Falha de health durante a coleta; não serão emitidas novas amostras.")
                break
            if audio_enabled:
                result = request(name, "audio")
                rows[name]["audio"].append(result)
                if not result.get("ok"):
                    audio_enabled = False
                    notes.append("Amostragem de áudio interrompida: cache ausente, resposta incompleta ou erro de transporte; não haverá síntese de reposição.")
                elif audio_digest is None:
                    audio_digest = result.get("sha256")
                elif audio_digest != result.get("sha256"):
                    result["ok"] = False
                    result["error"] = "o áudio mudou entre as amostras"
                    audio_enabled = False
                    notes.append("Áudio diferente entre as rotas/amostras; comparação de áudio inválida.")
        if halted:
            break

    summaries = {}
    for name, metrics in rows.items():
        summaries[name] = {
            "health": summarize(metrics["health"], "total_ms"),
            "first_audio": summarize(metrics["audio"], "first_audio_ms"),
            "audio_total": summarize(metrics["audio"], "total_ms"),
            "errors": sum(not item.get("ok") for samples in metrics.values() for item in samples),
        }
    coverage = all(summary["health"]["count"] == options.samples for summary in summaries.values())
    audio_coverage = bool(options.audio_text) and all(summary["first_audio"]["count"] == options.samples for summary in summaries.values())
    measured = {name: comparison(summaries, name) for name in ("health", "first_audio", "audio_total")}
    if not audio_coverage:
        measured["first_audio"] = {"comparable": False, "reason": "amostragem de áudio incompleta ou não solicitada"}
        measured["audio_total"] = {"comparable": False, "reason": "amostragem de áudio incompleta ou não solicitada"}
    if not coverage:
        measured["health"] = {"comparable": False, "reason": "amostragem de health incompleta"}
    comparable = [item for item in measured.values() if item.get("comparable")]
    accepted = bool(coverage and audio_coverage and comparable and all(item["no_regression_over_5_percent"] for item in comparable))
    return {
        "schema": SCHEMA, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "worker_id": worker_id, "routes": routes, "samples_requested_per_route": options.samples,
        "elapsed_seconds": round(clock() - started, 3), "preflight": health,
        "warm_request": warm_result, "summaries": summaries, "comparison": measured,
        "complete_health": coverage, "complete_audio": audio_coverage,
        "migration_speed_criterion_passed": accepted,
        "assessment": ("comparação completa" if coverage and audio_coverage and len(routes) == 2 else "diagnóstico parcial; não comprova ganho de primeiro áudio"),
        "notes": notes + ["Tempos medidos dentro de cada requisição; criação dos subprocessos não entra no RTT.",
                          "Esta coleta não mede novas sínteses, reprodução no Discord ou consumo de bateria. Uma requisição de aquecimento só ocorre com --warm-audio."],
        "samples": rows,
    }


def text_report(data):
    lines = ["Osaka — comparação SSH / transporte anterior", "Data: " + data["observed_at"],
             "Worker: " + data["worker_id"], "Resultado: " + data["assessment"], ""]
    for route, metrics in data["summaries"].items():
        lines.append(route.upper())
        for label, key in (("Health", "health"), ("Primeiro áudio cacheado", "first_audio"), ("Áudio completo", "audio_total")):
            row = metrics[key]
            lines.append(f"  {label}: n={row['count']} p50={row['p50_ms']} ms p95={row['p95_ms']} ms")
        lines.append("  Erros: " + str(metrics["errors"]))
    lines.extend(["", "Comparação p95:"])
    for metric, row in data["comparison"].items():
        if row.get("comparable"):
            lines.append(f"  {metric}: ganho SSH {row['ssh_gain_percent']}%; meta de 10%={row['target_10_percent_faster']}; sem regressão >5%={row['no_regression_over_5_percent']}")
        else:
            lines.append("  " + metric + ": sem comparação válida")
    lines.extend(["", *data["notes"]])
    return "\n".join(lines) + "\n"


def atomic_output(path, text):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".osaka-bench-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--ssh-base", default="http://127.0.0.1:18766")
    result.add_argument("--baseline-base", help="opcional: endereço privado anterior, apenas para a comparação temporária")
    result.add_argument("--env-file", default="/home/ubuntu/bot/.env")
    result.add_argument("--worker-id")
    result.add_argument("--samples", type=int, default=50)
    result.add_argument("--request-timeout", type=float, default=5)
    result.add_argument("--total-seconds", type=float, default=180)
    result.add_argument("--audio-text", default="", help="texto de áudio já cacheado no telefone; sem síntese em cache miss")
    result.add_argument("--voice", default="pt-BR-FranciscaNeural")
    result.add_argument("--cache-key", default="", help="opcional: chave completa de um MP3 já existente no cache")
    result.add_argument("--warm-audio", action="store_true", help="permite uma única requisição Edge para aquecer o áudio informado")
    result.add_argument("--output", help="prefixo dos relatórios .json e .txt")
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--_probe"]:
        try:
            spec = json.loads(sys.stdin.read(16384))
            print(json.dumps(probe(spec), ensure_ascii=False))
            return 0
        except Exception:
            print(json.dumps({"ok": False, "error": "parâmetros internos de medição inválidos"}))
            return 1
    args = parser().parse_args(argv)
    try:
        settings = read_env(args.env_file)
    except (OSError, UnicodeError) as exc:
        print(json.dumps({"ok": False, "error": "não foi possível ler o arquivo de configuração: " + type(exc).__name__}, ensure_ascii=False))
        return 1
    token = settings.get("PHONE_WORKER_TOKEN") or os.getenv("PHONE_WORKER_TOKEN", "")
    worker_id = args.worker_id or settings.get("PHONE_WORKER_ID") or settings.get("CORE_WORKER_ID") or os.getenv("PHONE_WORKER_ID", "")
    if not token or not worker_id:
        parser().error("PHONE_WORKER_TOKEN e ID do worker são necessários; o token é lido do .env, nunca por argumento")
    if not 1 <= args.samples <= 200 or not 1 <= args.request_timeout <= 20 or not 5 <= args.total_seconds <= 1800:
        parser().error("limites: 1–200 amostras, timeout 1–20s, duração total 5–1800s")
    if len(args.audio_text) > 1200 or (args.warm_audio and not args.audio_text):
        parser().error("--warm-audio exige --audio-text; texto máximo de 1200 caracteres")
    if args.cache_key and not re.fullmatch(r"[a-z0-9_-]{16,96}", args.cache_key):
        parser().error("--cache-key deve ser a chave completa válida do cache")
    if args.audio_text and not args.cache_key and args.warm_audio:
        args.cache_key = hashlib.sha256(("osaka-ssh-benchmark-v1|" + args.voice + "|" + args.audio_text).encode()).hexdigest()
    try:
        data = benchmark(args, token, worker_id)
    except (ValueError, OSError) as exc:
        print(json.dumps(clean({"ok": False, "error": str(exc)}, [token]), ensure_ascii=False))
        return 1
    data = clean(data, [token])
    output = Path(args.output or ("osaka-ssh-benchmark-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
    atomic_output(str(output) + ".json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    atomic_output(str(output) + ".txt", text_report(data))
    print(text_report(data))
    print("RELATÓRIOS: " + str(output.resolve()) + ".json / .txt")
    if not data["complete_health"]:
        return 1
    if any(row.get("comparable") and not row["no_regression_over_5_percent"] for row in data["comparison"].values()):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
