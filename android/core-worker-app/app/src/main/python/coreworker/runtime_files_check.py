from pathlib import Path
from .safe_json import load_context, ok_response, error_response, safe_path, dir_size


def _exists(path_value):
    try:
        return Path(str(path_value or "")).exists()
    except Exception:
        return False


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        files_dir = ctx.get("filesDir", "")
        cache_dir = ctx.get("cacheDir", "")
        runtime_dir = ctx.get("runtimeDir", "")
        runtime = Path(str(runtime_dir or ""))
        markers = {
            "runtimeDir": _exists(runtime_dir),
            "nativeHealth": (runtime / "native-health.json").exists() if runtime_dir else False,
            "runtimeState": (runtime / "runtime-state.json").exists() if runtime_dir else False,
            "pythonMarker": (runtime / "python" / "runtime-marker.json").exists() if runtime_dir else False,
        }
        missing = [name for name, ok in markers.items() if not ok]
        runtime_size = dir_size(runtime_dir, max_files=160)
        cache_size = dir_size(cache_dir, max_files=160)
        ok = bool(markers.get("runtimeDir")) and len(missing) <= 2
        summary = "arquivos Python/runtime ok" if ok else f"runtime com {len(missing)} item(ns) pendente(s)"
        return ok_response(
            "python_runtime_files_check",
            summary,
            ok=ok,
            scope="app-specific-internal",
            filesDir=safe_path(files_dir),
            cacheDir=safe_path(cache_dir),
            runtimeDir=safe_path(runtime_dir),
            markers=markers,
            missing=missing[:10],
            runtime=runtime_size,
            cache=cache_size,
        )
    except Exception as exc:
        return error_response("python_runtime_files_check", exc)
