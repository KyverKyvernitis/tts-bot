"""Pure configuration transformations; environment and file IO belong to the caller."""
from collections.abc import Callable, Mapping, Sequence
import json
import re
from typing import Any


def safe_env_key(value: Any) -> str:
    key = str(value or "").strip()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
        raise ValueError(f"chave de env inválida: {key or '<vazia>'}")
    return key


def format_env_value(value: Any) -> str:
    text = str(value if value is not None else "")
    if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]*", text):
        return text
    # Start scripts source this file. JSON quoting alone still expands $ and
    # command substitutions inside double quotes; these two escapes are decoded
    # by the facade's bootstrap-compatible reader when no shell has loaded it.
    return json.dumps(text, ensure_ascii=False).replace("$", "\\$").replace("`", "\\`")


def render_env_lines(existing: Sequence[str], wanted: Mapping[str, str],
                     format_value: Callable[[Any], str]) -> str:
    """Preserve unrelated lines, update all occurrences, then append missing keys."""
    seen: set[str] = set()
    output: list[str] = []
    assign_re = re.compile(r"^(?:export\s+)?([A-Z_][A-Z0-9_]*)=")
    for line in existing:
        match = assign_re.match(line.strip())
        if match and match.group(1) in wanted:
            key = match.group(1)
            output.append(f"{key}={format_value(wanted[key])}")
            seen.add(key)
        else:
            output.append(line)
    missing = [key for key in wanted if key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Core Worker pareado automaticamente. Não envie estes valores ao GitHub.")
        for key in missing:
            output.append(f"{key}={format_value(wanted[key])}")
    return "\n".join(output).rstrip() + "\n"
