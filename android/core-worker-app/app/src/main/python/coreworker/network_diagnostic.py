from urllib.parse import urlparse
from .safe_json import load_context, ok_response, error_response, clean_text


def _bool_label(value):
    return "sim" if bool(value) else "não"


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        network = ctx.get("network") or {}
        if not isinstance(network, dict):
            network = {}
        server_configured = bool(ctx.get("serverUrlConfigured"))
        server_url = str(ctx.get("serverUrl") or ctx.get("vpsUrl") or "")
        parsed = urlparse(server_url) if server_url else None
        net_type = clean_text(network.get("type") or "desconhecida", 40)
        vpn = bool(network.get("vpn"))
        available = bool(network.get("available", True))
        ping = network.get("vps_ping_ms", network.get("ping_ms", network.get("vpsPingMs", -1)))
        try:
            ping_int = int(ping)
        except Exception:
            ping_int = -1
        ok = available and server_configured and (ping_int >= 0 or bool(network.get("private_network_hint")) or bool(network.get("vpn")))
        summary = f"rede Python: {net_type} · VPN {_bool_label(vpn)}"
        if ping_int >= 0:
            summary += f" · VPS {ping_int}ms"
        if not server_configured:
            summary += " · VPS não configurada"
        return ok_response(
            "python_network_diagnostic",
            summary,
            available=available,
            networkType=net_type,
            vpn=vpn,
            serverConfigured=server_configured,
            serverHost=clean_text(parsed.hostname if parsed else "", 120),
            vpsPingMs=ping_int,
            privateNetworkHint=clean_text(network.get("private_network_hint", ""), 120),
            source="python-context-no-arbitrary-network",
            ok=ok,
        )
    except Exception as exc:
        return error_response("python_network_diagnostic", exc)
