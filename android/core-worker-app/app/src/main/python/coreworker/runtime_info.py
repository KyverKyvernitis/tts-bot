import os
import platform
import sys
from .safe_json import load_context, ok_response, error_response, safe_path, clean_text


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        return ok_response(
            "python_runtime_info",
            "runtime Python embutido disponível",
            pythonVersion=sys.version,
            implementation=platform.python_implementation(),
            platform=clean_text(platform.platform(), 180),
            cacheTag=getattr(sys.implementation, "cache_tag", ""),
            moduleSearchPaths=[safe_path(p) for p in list(sys.path)[:8]],
            home=safe_path(os.environ.get("HOME", "")),
            filesDir=safe_path(ctx.get("filesDir", "")),
            cacheDir=safe_path(ctx.get("cacheDir", "")),
            runtimeDir=safe_path(ctx.get("runtimeDir", "")),
            arbitraryCode=False,
        )
    except Exception as exc:
        return error_response("python_runtime_info", exc)
