from __future__ import annotations


def normalizar_localidade_atts(valor: object, padrao: str = "pt-BR") -> str:
    bruto = str(valor or padrao or "").strip().replace("_", "-")
    if not bruto:
        return str(padrao or "").strip()
    partes = [p for p in bruto.split("-") if p]
    if len(partes) == 1:
        return partes[0].lower()
    return f"{partes[0].lower()}-{partes[1].upper()}"


def normalizar_fator_atts(valor: object, padrao: str = "1.0") -> str | None:
    bruto = str(valor or padrao or "1.0").strip().lower().replace("x", "")
    bruto = bruto.replace(",", ".")
    try:
        numero = float(bruto)
    except Exception:
        return None
    numero = max(0.5, min(2.0, numero))
    texto = f"{numero:.2f}".rstrip("0").rstrip(".")
    return texto if texto else "1"


def normalizar_fator_personalizado_atts(valor: object) -> str | None:
    bruto = str(valor or "").strip().lower().replace("x", "").replace(",", ".")
    if not bruto:
        return None
    try:
        numero = float(bruto)
    except Exception:
        return None
    if numero < 0.5 or numero > 2.0:
        return None
    texto = f"{numero:.2f}".rstrip("0").rstrip(".")
    return texto if texto else "1"


def padrao_radio_modal_atts(atual: str, predefinidos: set[str]) -> str:
    normalizado = normalizar_fator_atts(atual, "1.0") or "1"
    return normalizado if normalizado in predefinidos else "custom"


def separar_valores_personalizados_atts(
    valor: object,
    *,
    taxa_padrao: str = "1.0",
    tom_padrao: str = "1.0",
) -> tuple[str, str]:
    bruto = str(valor or "").strip()
    if not bruto:
        return str(taxa_padrao or "1.0"), str(tom_padrao or "1.0")
    if "/" in bruto:
        partes = bruto.split("/", 1)
    elif ";" in bruto:
        partes = bruto.split(";", 1)
    else:
        partes = bruto.split(None, 1)
    if len(partes) == 1:
        primeiro = partes[0].strip()
        return primeiro, primeiro
    return partes[0].strip(), partes[1].strip()
