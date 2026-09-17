"""Telemetry with explicit probes; caches and runtime state belong to the caller."""
from collections.abc import Callable, MutableMapping
import json
import math
import urllib.parse
from pathlib import Path
from typing import Any


def sysfs_battery_snapshot(power_supply_root: Path, *, list_candidates: Callable, path_exists: Callable,
                           read_text: Callable, empty_snapshot: Callable) -> dict[str, Any]:
    # Fallback leve quando Termux:API não está instalado ou sem permissão.
    # Em alguns Androids/Termux, apenas chamar Path.exists() em /sys pode gerar
    # PermissionError. Telemetria é sempre best-effort e nunca pode derrubar
    # heartbeat/jobs.
    base_candidates: list[Path] = []
    failure_source = "sysfs_unavailable"
    failure_error: object = ""
    primary = power_supply_root / "battery"
    if path_exists(primary):
        base_candidates.append(primary)
    try:
        for candidate in list_candidates(power_supply_root):
            if path_exists(candidate) and candidate not in base_candidates:
                base_candidates.append(candidate)
    except (PermissionError, OSError):
        failure_source = "sysfs_permission_denied"
    except Exception as exc:
        failure_source, failure_error = "sysfs_error", exc

    for base in base_candidates:
        try:
            result: dict[str, Any] = {"available": True, "source": "sysfs"}
            capacity = read_text(base / "capacity", limit=32)
            status = read_text(base / "status", limit=64).lower()
            plugged = read_text(base / "type", limit=64).lower()
            temp = read_text(base / "temp", limit=32)
            try:
                if capacity:
                    result["level"] = max(0, min(100, int(float(capacity))))
            except Exception:
                pass
            if status:
                result["status"] = status[:32]
                result["charging"] = status in {"charging", "full"}
            if plugged:
                result["plugged"] = plugged[:32]
            try:
                if temp:
                    raw_temp = float(temp)
                    # Linux power_supply temp uses tenths of Celsius, also at
                    # zero/negative temperatures; magnitude is not a unit hint.
                    if math.isfinite(raw_temp):
                        result["temperature_c"] = round(raw_temp / 10.0, 1)
            except Exception:
                pass
            if any(key in result for key in ("level", "status", "charging", "temperature_c")):
                return result
        except (PermissionError, OSError):
            continue
        except Exception:
            continue
    return empty_snapshot(failure_source, failure_error)


def battery_snapshot(*, run_json_command: Callable, sysfs_snapshot: Callable,
                     empty_snapshot: Callable) -> dict[str, Any]:
    try:
        raw = run_json_command(["termux-battery-status"], timeout=2.0)
    except Exception:
        raw = {}

    level = raw.get("percentage")
    if level is None:
        level = raw.get("level")
    charging = None
    status = str(raw.get("status") or "").strip().lower()
    plugged = str(raw.get("plugged") or "").strip().lower()
    if status:
        charging = status in {"charging", "full"}
    elif plugged:
        charging = plugged not in {"unplugged", "none", "unknown"}
    result: dict[str, Any] = {"available": True, "source": "termux-api"}
    try:
        if level is not None:
            clean_level = max(0, min(100, int(float(level))))
            result["level"] = clean_level
            result["percentage"] = clean_level
            result["percent"] = clean_level
    except Exception:
        pass
    if charging is not None:
        result["charging"] = bool(charging)
    if status:
        result["status"] = status[:32]
    if plugged:
        result["plugged"] = plugged[:32]
    try:
        temp = raw.get("temperature")
        if temp is not None:
            clean_temp = float(temp)
            if math.isfinite(clean_temp):
                result["temperature_c"] = round(clean_temp, 1)
    except Exception:
        pass
    if any(key in result for key in ("level", "charging", "temperature_c")):
        return result
    # A non-empty JSON error/metadata object is not a battery measurement.
    try:
        return sysfs_snapshot()
    except (PermissionError, OSError) as exc:
        return empty_snapshot("battery_permission_denied", exc)
    except Exception as exc:
        return empty_snapshot("battery_error", exc)


def tailscale_snapshot(*, probe_vps: bool, auth_parts: Callable, base_url_host: Callable,
                       looks_like_tailscale_host: Callable, find_command: Callable,
                       run_text_command: Callable, mask_ipv4: Callable, short_text: Callable,
                       request_factory: Callable, urlopen: Callable, wall_clock: Callable) -> dict[str, Any]:
    base_url, _token, _worker_id = auth_parts()
    base_host = base_url_host()
    base_looks_tailscale = looks_like_tailscale_host(base_host)
    result: dict[str, Any] = {
        "cli_available": bool(find_command("tailscale")),
        "connected": False,
        "state": "unknown",
        "via_vps_url": bool(base_looks_tailscale),
    }
    if base_host:
        result["vps_host_masked"] = mask_ipv4(base_host)
    ip = ""
    if result["cli_available"]:
        code, stdout, stderr = run_text_command(["tailscale", "ip", "-4"], timeout=2.5, max_bytes=4096)
        if code == 0 and stdout.strip():
            ip = stdout.strip().splitlines()[0].strip()
            result["connected"] = True
            result["ip_present"] = True
            result["ip_masked"] = mask_ipv4(ip)
        elif stderr:
            result["ip_error"] = short_text(stderr, limit=120)

        code, stdout, stderr = run_text_command(["tailscale", "status", "--json"], timeout=3.5, max_bytes=65536)
        if code == 0 and stdout:
            try:
                parsed = json.loads(stdout)
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                state = str(parsed.get("BackendState") or parsed.get("backendState") or "").strip()
                if state:
                    result["state"] = state[:48]
                    result["connected"] = result["connected"] or state.lower() == "running"
                self_info = parsed.get("Self") if isinstance(parsed.get("Self"), dict) else {}
                if self_info:
                    result["hostname"] = short_text(self_info.get("HostName"), limit=64)
                    result["online"] = bool(self_info.get("Online", result.get("connected")))
                peers = parsed.get("Peer") if isinstance(parsed.get("Peer"), dict) else {}
                result["peers"] = len(peers) if isinstance(peers, dict) else 0
        elif code != 127 and stderr:
            result["status_error"] = short_text(stderr, limit=160)
    else:
        # No Android é comum usar o app oficial do Tailscale como VPN, sem CLI no Termux.
        # Se a VPS configurada é 100.x.x.x ou MagicDNS, o heartbeat bem-sucedido já prova
        # que o Termux alcança a VPS por uma rota privada/VPN; não mostrar como "off".
        if base_looks_tailscale:
            result["connected"] = True
            result["state"] = "app/vpn"
            result["note"] = "CLI tailscale ausente; conexão inferida pelo endpoint privado da VPS"
        else:
            result["state"] = "no-cli"
            result["note"] = "CLI tailscale não encontrada no Termux; use o app oficial para a VPN"

    if probe_vps and base_url:
        health_url = base_url.rstrip("/") + "/health"
        started = wall_clock()
        try:
            req = request_factory(health_url, headers={"Accept": "application/json"}, method="GET")
            with urlopen(req, timeout=4.0) as resp:
                raw = resp.read(4096)
                result["vps_reachable"] = True
                result["vps_status"] = int(getattr(resp, "status", 200) or 200)
                result["vps_latency_ms"] = round((wall_clock() - started) * 1000, 1)
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
                    if isinstance(data, dict):
                        result["vps_health_ok"] = bool(data.get("ok", True))
                except Exception:
                    pass
        except Exception as exc:
            result["vps_reachable"] = False
            result["vps_error"] = f"{type(exc).__name__}: {short_text(exc, limit=120)}"
    return result


def vps_tcp_ping_snapshot(*, timeout: float, cache_ttl: float, auth_parts: Callable,
                          ping_cache: MutableMapping, monotonic: Callable, perf_counter: Callable,
                          create_connection: Callable, mask_ipv4: Callable, short_text: Callable) -> dict[str, Any]:
    """Mede RTT TCP do worker até a VPS/orquestrador.

    Não usa ICMP/root. Apenas abre uma conexão TCP curta para a URL já
    configurada em CORE_WORKER_VPS_URL. Resultado é cacheado por poucos
    segundos porque o payload também é usado no polling de jobs.
    """
    base_url, _token, _worker_id = auth_parts()
    if not base_url:
        return {"available": False, "reachable": False, "source": "not_configured"}
    try:
        parsed = urllib.parse.urlparse(base_url)
        host = parsed.hostname or ""
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception as exc:
        return {"available": False, "reachable": False, "source": "invalid_url", "error": short_text(exc, limit=100)}
    if not host:
        return {"available": False, "reachable": False, "source": "missing_host"}

    cache_key = f"{host}:{port}"
    now = monotonic()
    cached = ping_cache.get(cache_key)
    if isinstance(cached, dict) and now - float(cached.get("monotonic_at") or 0.0) <= max(0.5, cache_ttl):
        result = dict(cached.get("result") or {})
        result["cached"] = True
        return result

    started = perf_counter()
    result: dict[str, Any] = {
        "available": True,
        "source": "tcp_connect",
        "host_masked": mask_ipv4(host),
        "port": port,
    }
    try:
        with create_connection((host, port), timeout=max(0.3, timeout)):
            pass
        latency_ms = round((perf_counter() - started) * 1000, 1)
        result.update({
            "reachable": True,
            "ping_ms": latency_ms,
            "latency_ms": latency_ms,
            "vps_ping_ms": latency_ms,
        })
    except Exception as exc:
        result.update({
            "reachable": False,
            "error": f"{type(exc).__name__}: {short_text(exc, limit=100)}",
        })
    ping_cache[cache_key] = {"monotonic_at": now, "result": dict(result)}
    return result


def network_snapshot(*, run_json_command: Callable, heartbeat_configured: Callable,
                     tailscale_snapshot: Callable, safe_telemetry: Callable,
                     ping_snapshot: Callable, short_text: Callable) -> dict[str, Any]:
    result: dict[str, Any] = {}
    wifi = run_json_command(["termux-wifi-connectioninfo"], timeout=2.0)
    if wifi and not wifi.get("error"):
        result["type"] = "wifi"
        result["source"] = "termux-api"
        ssid = str(wifi.get("ssid") or "").strip()
        if ssid and ssid != "<unknown ssid>":
            result["name"] = short_text(ssid, limit=48)
        try:
            result["rssi"] = int(wifi.get("rssi"))
        except Exception:
            pass
    else:
        # Sem Termux:API, ainda conseguimos dizer que há conectividade se o worker
        # está alcançando a VPS por heartbeat/poll.
        result["type"] = "connected" if heartbeat_configured() else "unknown"
        result["source"] = "inferred"
    tailscale = tailscale_snapshot(probe_vps=False)
    result["tailscale"] = bool(tailscale.get("connected"))
    result["tailscale_cli"] = bool(tailscale.get("cli_available"))
    result["tailscale_state"] = short_text(tailscale.get("state"), limit=48, default="unknown")
    result["tailscale_via_vps_url"] = bool(tailscale.get("via_vps_url"))
    if tailscale.get("ip_masked"):
        result["tailscale_ip_masked"] = tailscale.get("ip_masked")
    elif tailscale.get("vps_host_masked") and tailscale.get("via_vps_url"):
        result["tailscale_ip_masked"] = tailscale.get("vps_host_masked")
    if tailscale.get("note"):
        result["tailscale_note"] = short_text(tailscale.get("note"), limit=100)
    ping = safe_telemetry("vps ping", ping_snapshot, {"available": False, "reachable": False, "source": "telemetry_failed"})
    if isinstance(ping, dict):
        result["vps_reachable"] = bool(ping.get("reachable"))
        result["vps_ping_available"] = bool(ping.get("available", True))
        if ping.get("ping_ms") is not None:
            result["vps_ping_ms"] = ping.get("ping_ms")
            result["ping_ms"] = ping.get("ping_ms")
        elif ping.get("latency_ms") is not None:
            result["vps_ping_ms"] = ping.get("latency_ms")
            result["ping_ms"] = ping.get("latency_ms")
        if ping.get("host_masked"):
            result["vps_host_masked"] = ping.get("host_masked")
        if ping.get("port"):
            result["vps_port"] = ping.get("port")
        if ping.get("error"):
            result["vps_ping_error"] = short_text(ping.get("error"), limit=120)
    return result
