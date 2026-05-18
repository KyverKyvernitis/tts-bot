import os
import platform
import sys
from .safe_json import load_context, ok_response, error_response, safe_path


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        return ok_response(
            "python_health_check",
            "Python interno Chaquopy ok",
            pythonVersion=sys.version.split()[0],
            implementation=platform.python_implementation(),
            executable=safe_path(getattr(sys, "executable", "")),
            home=safe_path(os.environ.get("HOME", "")),
            appVersion=ctx.get("appVersion", ""),
            appVersionCode=ctx.get("appVersionCode", 0),
            arbitraryCode=False,
        )
    except Exception as exc:
        return error_response("python_health_check", exc)
