"""Helpers de campos dinâmicos do formulário."""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any

from .constants import (
    DEFAULT_MODAL,
    DEFAULT_MODAL_FIELDS,
    FIELD_VALUE_LONG_MAX,
    FIELD_VALUE_SHORT_MAX,
    MODAL_FIELD_LIMIT,
    TEXT_INPUT_LABEL_MAX,
    TEXT_INPUT_PLACEHOLDER_MAX,
)

DISCORD_TEXT_INPUT_MAX = 4000


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def default_form_fields() -> list[dict[str, Any]]:
    return deepcopy(DEFAULT_MODAL_FIELDS)


def _legacy_fields_from_modal(modal: dict[str, Any] | None) -> list[dict[str, Any]]:
    modal = modal or {}
    defaults = default_form_fields()
    result: list[dict[str, Any]] = []
    for idx, default in enumerate(defaults[:3], start=1):
        label = str(modal.get(f"field{idx}_label") or default["label"])
        placeholder = str(modal.get(f"field{idx}_placeholder") or default["placeholder"])
        # Migra os nomes que vieram da referência, sem destruir customizações reais.
        if idx == 2 and label.strip().lower() == "idade":
            label = "Idade e pronome"
            if placeholder.strip() in {"", "17"}:
                placeholder = "17, ele/dele"
        if idx == 3 and label.strip().lower() == "motivo":
            label = "Descrição"
        result.append({
            **default,
            "label": label,
            "placeholder": placeholder,
            "response_label": label,
            "required": bool(modal.get(f"field{idx}_required", default.get("required", True))),
        })
    return result


def normalize_form_fields(modal_or_fields: dict[str, Any] | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normaliza a lista de campos e migra configs antigas field1/field2/field3.

    Retorna sempre de 1 a MODAL_FIELD_LIMIT campos, prontos para montar modal,
    resposta da staff e resumo do painel.
    """
    if isinstance(modal_or_fields, dict):
        raw_fields = modal_or_fields.get("fields")
        if isinstance(raw_fields, list) and raw_fields:
            raw_list = raw_fields
        else:
            raw_list = _legacy_fields_from_modal(modal_or_fields)
    elif isinstance(modal_or_fields, list):
        raw_list = modal_or_fields
    else:
        raw_list = default_form_fields()

    defaults = default_form_fields()
    fields: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for idx, raw in enumerate(raw_list[:MODAL_FIELD_LIMIT]):
        raw = raw if isinstance(raw, dict) else {}
        fallback = defaults[idx] if idx < len(defaults) else {
            "id": f"field{idx + 1}",
            "label": f"Campo {idx + 1}",
            "placeholder": "",
            "response_label": f"Campo {idx + 1}",
            "required": False,
            "long": False,
            "show_in_response": True,
            "enabled": True,
            "min_length": 0,
            "max_length": FIELD_VALUE_SHORT_MAX,
        }

        field_id = str(raw.get("id") or raw.get("key") or fallback.get("id") or f"field{idx + 1}").strip()
        field_id = re.sub(r"[^a-zA-Z0-9_:-]", "_", field_id)[:48] or f"field{idx + 1}"
        if field_id in seen_ids:
            field_id = next_field_id(fields)
        seen_ids.add(field_id)

        long = bool(raw.get("long", fallback.get("long", False)))
        default_max = FIELD_VALUE_LONG_MAX if long else FIELD_VALUE_SHORT_MAX
        max_length = _clamp(_to_int(raw.get("max_length"), default_max), 1, DISCORD_TEXT_INPUT_MAX)
        min_length = _clamp(_to_int(raw.get("min_length"), int(fallback.get("min_length") or 0)), 0, max_length)

        label = _truncate(raw.get("label") or fallback.get("label") or f"Campo {idx + 1}", TEXT_INPUT_LABEL_MAX)
        if idx == 1 and label.strip().lower() == "idade":
            label = "Idade e pronome"
        if idx == 2 and label.strip().lower() == "motivo":
            label = "Descrição"

        response_label = _truncate(raw.get("response_label") or label, TEXT_INPUT_LABEL_MAX)
        if response_label.strip().lower() == "motivo":
            response_label = "Descrição"

        fields.append({
            "id": field_id,
            "label": label or f"Campo {idx + 1}",
            "placeholder": _truncate(raw.get("placeholder") or fallback.get("placeholder") or "", TEXT_INPUT_PLACEHOLDER_MAX),
            "response_label": response_label or label or f"Campo {idx + 1}",
            "required": bool(raw.get("required", fallback.get("required", True))),
            "long": long,
            "show_in_response": bool(raw.get("show_in_response", True)),
            "enabled": bool(raw.get("enabled", True)),
            "min_length": min_length,
            "max_length": max_length,
        })

    if not fields:
        fields = default_form_fields()[:1]
    return fields[:MODAL_FIELD_LIMIT]


def sync_legacy_field_keys(modal: dict[str, Any] | None) -> dict[str, Any]:
    modal = dict(modal or {})
    fields = normalize_form_fields(modal)
    modal["fields"] = fields
    for idx, field in enumerate(fields[:3], start=1):
        modal[f"field{idx}_label"] = field["label"]
        modal[f"field{idx}_placeholder"] = field["placeholder"]
        modal[f"field{idx}_required"] = bool(field.get("required", True))
    return modal


def normalize_modal_config(modal: dict[str, Any] | None) -> dict[str, Any]:
    modal = dict(modal or {})
    modal["title"] = _truncate(modal.get("title") or DEFAULT_MODAL["title"], 45)
    return sync_legacy_field_keys(modal)


def next_field_id(fields: list[dict[str, Any]]) -> str:
    used = {str(f.get("id") or "") for f in fields}
    idx = 1
    while f"field{idx}" in used:
        idx += 1
    return f"field{idx}"


def field_display_summary(fields: list[dict[str, Any]], *, limit: int = 120) -> str:
    parts = []
    for field in normalize_form_fields(fields):
        label = str(field.get("label") or "Campo")
        suffix = "obrigatório" if field.get("required", True) else "opcional"
        if not field.get("show_in_response", True):
            suffix += ", oculto na resposta"
        parts.append(f"{label} ({suffix})")
    text = " • ".join(parts)
    return text[:limit - 1] + "…" if len(text) > limit else text


def get_field_value(field_values: dict[str, str] | None, field: dict[str, Any], index: int) -> str:
    values = field_values or {}
    field_id = str(field.get("id") or f"field{index + 1}")
    return str(values.get(field_id) or values.get(f"field{index + 1}") or "").strip()


def slugify_placeholder(label: str) -> str:
    text = unicodedata.normalize("NFKD", str(label or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text[:40]
