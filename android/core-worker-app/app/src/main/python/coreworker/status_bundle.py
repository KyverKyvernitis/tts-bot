from .safe_json import load_context, ok_response, error_response, clean_text


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        battery = ctx.get("battery") or {}
        network = ctx.get("network") or {}
        runtime = ctx.get("runtime") or {}
        status = ctx.get("status") or {}
        summary_parts = []
        if isinstance(battery, dict) and battery.get("percent") is not None:
            summary_parts.append(f"bateria {battery.get('percent')}%")
        if isinstance(network, dict) and network.get("type"):
            summary_parts.append(str(network.get("type")))
        if isinstance(runtime, dict) and runtime.get("mode"):
            summary_parts.append(str(runtime.get("mode")))
        summary = " · ".join(summary_parts) or "status bundle gerado"
        return ok_response(
            "python_status_bundle",
            clean_text(summary, 180),
            appVersion=ctx.get("appVersion", ""),
            appVersionCode=ctx.get("appVersionCode", 0),
            battery=battery if isinstance(battery, dict) else {},
            network=network if isinstance(network, dict) else {},
            runtime=runtime if isinstance(runtime, dict) else {},
            statusKeys=sorted(list(status.keys()))[:40] if isinstance(status, dict) else [],
        )
    except Exception as exc:
        return error_response("python_status_bundle", exc)
