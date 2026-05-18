from .safe_json import load_context, ok_response, error_response, dir_size, safe_path


def run(context_json=None):
    try:
        ctx = load_context(context_json)
        files_dir = ctx.get("filesDir", "")
        cache_dir = ctx.get("cacheDir", "")
        runtime_dir = ctx.get("runtimeDir", "")
        files = dir_size(files_dir)
        cache = dir_size(cache_dir)
        runtime = dir_size(runtime_dir)
        summary = f"storage ok · files {files.get('files', 0)} · cache {cache.get('bytes', 0)} B"
        return ok_response(
            "python_storage_check",
            summary,
            scope="app-specific-internal",
            filesDir=safe_path(files_dir),
            cacheDir=safe_path(cache_dir),
            runtimeDir=safe_path(runtime_dir),
            files=files,
            cache=cache,
            runtime=runtime,
        )
    except Exception as exc:
        return error_response("python_storage_check", exc)
