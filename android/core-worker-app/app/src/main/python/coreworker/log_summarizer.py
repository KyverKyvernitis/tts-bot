from .safe_json import load_context, ok_response, error_response, clean_text


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        history = ctx.get("history") or []
        if not isinstance(history, list):
            history = []
        ok = 0
        failed = 0
        types = []
        for item in history[:24]:
            if not isinstance(item, dict):
                continue
            if item.get("ok"):
                ok += 1
            else:
                failed += 1
            typ = str(item.get("type") or "job")[:48]
            if typ not in types:
                types.append(typ)
        summary = f"histórico Python: {ok} ok · {failed} falhas"
        return ok_response(
            "python_log_summary",
            summary,
            historyCount=len(history),
            okCount=ok,
            failedCount=failed,
            recentTypes=types[:10],
            lastMessage=clean_text(history[0].get("message", "") if history and isinstance(history[0], dict) else "", 220),
        )
    except Exception as exc:
        return error_response("python_log_summary", exc)
