from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import traceback
from types import ModuleType
from typing import Iterable


class RuntimeSmokeError(RuntimeError):
    pass


def _normalize_root(value: str | os.PathLike[str]) -> Path:
    root = Path(value).resolve()
    if not (root / "bot.py").is_file():
        raise RuntimeSmokeError(f"bot.py não encontrado em {root}")
    if not (root / "cogs").is_dir():
        raise RuntimeSmokeError(f"diretório cogs não encontrado em {root}")
    return root


def _bot_probe(bot_module: ModuleType, root: Path):
    bot_class = getattr(bot_module, "BotLocal", None)
    if not inspect.isclass(bot_class):
        raise RuntimeSmokeError("bot.BotLocal não foi encontrado")

    # Não executa BotLocal.__init__: o smoke não deve abrir recursos, criar
    # clientes ou preparar conexões. Só reutilizamos o contrato de descoberta
    # de extensões que o próprio bot usará no boot real.
    probe = object.__new__(bot_class)
    probe._repo_root = root
    probe.skipped_extensions = {}
    try:
        critical = set(bot_class._read_critical_extensions(probe))
    except Exception as exc:  # pragma: no cover - mensagem exercitada via CLI
        raise RuntimeSmokeError(f"falha ao ler cogs críticas: {type(exc).__name__}: {exc}") from exc
    probe.critical_extensions = critical

    try:
        extensions = list(bot_class._discover_cog_extensions(probe))
    except Exception as exc:
        raise RuntimeSmokeError(f"falha ao descobrir extensões: {type(exc).__name__}: {exc}") from exc

    extensions = [str(item).strip() for item in extensions if str(item).strip()]
    if not extensions:
        raise RuntimeSmokeError("loader do bot não descobriu nenhuma extensão")
    if len(extensions) != len(set(extensions)):
        raise RuntimeSmokeError("loader do bot retornou extensões duplicadas")

    missing_critical = sorted(critical.difference(extensions))
    if missing_critical:
        raise RuntimeSmokeError(
            "cog(s) crítica(s) não aparecem no loader: " + ", ".join(missing_critical)
        )
    return extensions, critical, dict(getattr(probe, "skipped_extensions", {}) or {})


def _companion_cog_module(root: Path, extension: str) -> str | None:
    package_dir = root.joinpath(*extension.split("."))
    if package_dir.is_dir() and (package_dir / "cog.py").is_file():
        return f"{extension}.cog"
    return None


def _import_extension_surface(root: Path, extension: str) -> list[ModuleType]:
    try:
        module = importlib.import_module(extension)
    except Exception as exc:
        raise RuntimeSmokeError(
            f"falha ao importar extensão {extension}: {type(exc).__name__}: {exc}"
        ) from exc

    setup = getattr(module, "setup", None)
    if not callable(setup):
        raise RuntimeSmokeError(f"extensão {extension} não expõe setup()")
    if not inspect.iscoroutinefunction(setup):
        raise RuntimeSmokeError(f"extensão {extension} expõe setup() não assíncrono")

    modules = [module]
    companion = _companion_cog_module(root, extension)
    if companion and companion != extension:
        try:
            modules.append(importlib.import_module(companion))
        except Exception as exc:
            raise RuntimeSmokeError(
                f"falha ao importar implementação {companion}: {type(exc).__name__}: {exc}"
            ) from exc
    return modules


def _unique_modules(modules: Iterable[ModuleType]) -> list[ModuleType]:
    result: list[ModuleType] = []
    seen: set[str] = set()
    for module in modules:
        name = str(getattr(module, "__name__", "") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(module)
    return result


def _command_metadata(modules: Iterable[ModuleType]) -> tuple[int, int, list[str]]:
    """Inspeciona comandos sem instanciar cogs nem executar cog_load/setup.

    Os decorators do discord.py constroem os objetos de comando na importação.
    Ler os metadados de classe captura superfícies quebradas sem iniciar loops,
    banco, voz ou qualquer conexão externa.
    """

    try:
        discord_commands = importlib.import_module("discord.ext.commands")
        cog_base = getattr(discord_commands, "Cog")
    except Exception:
        # Só ocorre em fixtures sintéticas; no runtime real bot.py já depende de
        # discord.py e a importação teria falhado antes de chegar aqui.
        return 0, 0, []

    prefix_count = 0
    app_count = 0
    cog_classes: list[str] = []
    seen_classes: set[int] = set()

    for module in modules:
        module_name = str(getattr(module, "__name__", "") or "")
        for value in vars(module).values():
            if not inspect.isclass(value) or id(value) in seen_classes:
                continue
            try:
                is_cog = issubclass(value, cog_base) and value is not cog_base
            except TypeError:
                continue
            if not is_cog:
                continue
            # Não conte classes importadas de outro módulo duas vezes.
            if str(getattr(value, "__module__", "") or "") != module_name:
                continue
            seen_classes.add(id(value))
            cog_classes.append(f"{module_name}.{value.__name__}")

            for command in tuple(getattr(value, "__cog_commands__", ()) or ()):
                name = str(getattr(command, "name", "") or "").strip()
                if not name:
                    raise RuntimeSmokeError(f"comando prefixo sem nome em {value.__name__}")
                prefix_count += 1

            for command in tuple(getattr(value, "__cog_app_commands__", ()) or ()):
                name = str(getattr(command, "name", "") or "").strip()
                if not name:
                    raise RuntimeSmokeError(f"app command sem nome em {value.__name__}")
                app_count += 1

    return prefix_count, app_count, sorted(cog_classes)


def run_smoke(root: str | os.PathLike[str]) -> dict[str, object]:
    project_root = _normalize_root(root)
    os.chdir(project_root)
    root_text = str(project_root)
    if not sys.path or sys.path[0] != root_text:
        sys.path.insert(0, root_text)

    # Evita que o smoke suje o worktree com __pycache__. O updater também
    # exporta esta variável no subprocesso; mantemos aqui para uso direto.
    sys.dont_write_bytecode = True

    try:
        bot_module = importlib.import_module("bot")
    except Exception as exc:
        raise RuntimeSmokeError(f"falha ao importar bot.py: {type(exc).__name__}: {exc}") from exc

    extensions, critical, skipped = _bot_probe(bot_module, project_root)
    imported: list[ModuleType] = []
    for extension in extensions:
        imported.extend(_import_extension_surface(project_root, extension))
    imported = _unique_modules(imported)

    prefix_count, app_count, cog_classes = _command_metadata(imported)
    return {
        "ok": True,
        "extensions": len(extensions),
        "extension_names": extensions,
        "critical_extensions": sorted(critical),
        "skipped_extensions": skipped,
        "imported_modules": [module.__name__ for module in imported],
        "cog_classes": cog_classes,
        "prefix_commands": prefix_count,
        "app_commands": app_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test offline do runtime Python do candidato")
    parser.add_argument("--root", default=os.getenv("UPDATE_RUNTIME_SMOKE_ROOT", "."))
    args = parser.parse_args(argv)

    try:
        payload = run_smoke(args.root)
    except Exception as exc:
        print(f"[runtime-smoke] ERRO: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
