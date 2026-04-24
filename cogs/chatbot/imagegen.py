"""Geração de imagem via Gemini 2.5 Flash Image.

Endpoint REST, payload JSON, response tem a imagem em base64 no campo
`inlineData.data` de algum `part` dentro da primeira candidate.

Trigger pode ser explícito (comando `/chatbot imagem <prompt>`) ou implícito
(user escreve algo que parece pedido — "gera uma imagem de X", "desenha Y").
O módulo expõe detecção + geração, o cog decide quando chamar.

Importante: a resposta pode demorar 10-30s. O cog deve reagir primeiro
(ex: emoji "processando") pra o user saber que ta gerando.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from typing import Literal, Optional

import aiohttp

from . import constants as C

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeneratedImage:
    """Resultado de uma geração bem-sucedida."""
    data: bytes          # bytes da imagem (PNG ou JPEG)
    mime_type: str       # "image/png" geralmente
    caption: Optional[str] = None  # texto opcional que o modelo gerou junto


PromptClass = Literal["safe", "adult_allowed", "blocked"]
FailureReason = Literal[
    "provider_blocked",
    "policy_blocked",
    "channel_not_nsfw",
    "missing_key",
    "network_error",
    "timeout",
    "no_image_returned",
]


@dataclass(frozen=True)
class ImageGenerationResult:
    ok: bool
    provider: str
    prompt_class: PromptClass
    image: Optional[GeneratedImage] = None
    reason: Optional[FailureReason] = None
    detail: Optional[str] = None


# -----------------------------------------------------------------------------
# Detecção de pedido implícito de imagem no texto do user
# -----------------------------------------------------------------------------

# Regex: frases que parecem pedido de gerar imagem.
# Exige verbo imperativo + palavras relacionadas. Evita falsos positivos
# tipo "vi uma imagem legal ontem".
_IMAGE_REQUEST_RE = re.compile(
    r"\b("
    r"gera(r)?\s+(uma\s+|um\s+)?(imagem|foto|figura|desenho|arte|ilustra..o)"
    r"|gere\s+(uma\s+|um\s+)?(imagem|foto|figura|desenho|arte|ilustra..o)"
    r"|desenha(r)?\s+"
    r"|(faz|faça|faca|faca|faz[ae])\s+(uma\s+|um\s+)?(imagem|desenho|arte|figura|ilustra..o)"
    r"|me\s+mostra\s+(uma\s+|um\s+)?(imagem|desenho)"
    r"|cria\s+(uma\s+|um\s+)?(imagem|foto|arte|ilustra..o)"
    r"|imagina\s+(uma\s+|um\s+)?(cena|imagem)"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


def detect_image_request(text: str) -> bool:
    """True se o texto parece pedir geração de imagem.

    É uma heurística — pode errar. Por isso o cog pode gerar só quando
    o pedido é CLARO (ativo por default) e permitir override via comando
    explícito pros casos ambíguos.
    """
    if not text:
        return False
    return bool(_IMAGE_REQUEST_RE.search(text))


def extract_image_prompt(text: str) -> str:
    """Extrai o que o user quer que seja desenhado.

    Estratégia simples: remove o verbo imperativo + "imagem de" e deixa o
    resto. Ex: "desenha um dragão azul" → "um dragão azul".

    Se não conseguir extrair bem, retorna o texto completo (o modelo de
    imagem costuma aguentar verbosidade).
    """
    if not text:
        return ""
    # Remove prefixos comuns
    text = text.strip()
    patterns = [
        r"^(gera|gere|desenha|cria|faz|faça|imagina|me mostra)\s+",
        r"^(uma|um)\s+",
        r"^(imagem|foto|figura|desenho|arte|ilustra[cç][aã]o|cena)\s+(de\s+|com\s+)?",
    ]
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()
    return text or "uma imagem"


# -----------------------------------------------------------------------------
# Classificação de pedido (safe | adulto permitido | bloqueado)
# -----------------------------------------------------------------------------

_BLOCKED_PATTERNS = (
    r"menor(es)?\b",
    r"crian[cç]a(s)?\b",
    r"infantilizad[oa]s?\b",
    r"estupro\b",
    r"for[çc]ad[oa]\b",
    r"sem\s+consentimento\b",
    r"n[ãa]o\s+consensual\b",
    r"viol[êe]ncia\s+sexual\b",
    r"revenge\s*porn\b",
)
_REAL_PERSON_PATTERNS = (
    r"foto\s+da?\s+",
    r"parecid[oa]\s+com\b",
    r"realista\b",
    r"celebridade\b",
)
_ADULT_HINT_PATTERNS = (
    r"\bnsfw\b",
    r"\b18\+\b",
    r"conte[uú]do\s+adulto",
    r"er[oó]tic[oa]",
    r"nu[dz]?\b",
    r"sexo\b",
    r"porn[oô]\b",
    r"pelad[oa]\b",
)


def classify_image_prompt(prompt: str) -> PromptClass:
    text = (prompt or "").lower()
    if not text.strip():
        return "safe"

    if any(re.search(pat, text, flags=re.IGNORECASE) for pat in _BLOCKED_PATTERNS):
        return "blocked"
    if (
        any(re.search(pat, text, flags=re.IGNORECASE) for pat in _ADULT_HINT_PATTERNS)
        and any(re.search(pat, text, flags=re.IGNORECASE) for pat in _REAL_PERSON_PATTERNS)
    ):
        return "blocked"
    if any(re.search(pat, text, flags=re.IGNORECASE) for pat in _ADULT_HINT_PATTERNS):
        return "adult_allowed"
    return "safe"


def _prompt_preview(prompt: str, *, max_len: int = 120) -> str:
    compact = re.sub(r"\s+", " ", (prompt or "").strip())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


def build_image_failure_message(result: ImageGenerationResult) -> str:
    if result.reason == "policy_blocked":
        return (
            "🚫 Não posso gerar esse tipo de imagem. "
            "O pedido envolve conteúdo proibido (ex.: menor de idade, não consensual, "
            "pessoa real sem consentimento ou violência sexual)."
        )
    if result.reason == "channel_not_nsfw":
        return (
            "🔞 Pedido adulto detectado, mas este canal não é NSFW. "
            "Use um canal com restrição de idade."
        )
    if result.reason == "missing_key":
        if result.provider in ("adult", "adult_hf"):
            return (
                "⚙️ Geração adulta não configurada no bot "
                "(configure `ADULT_IMAGEGEN_PROVIDER` e as credenciais do provider)."
            )
        return "⚙️ Geração de imagem não configurada (falta `GEMINI_API_KEY`)."
    if result.reason == "timeout":
        return "⏱️ O provedor demorou demais para responder. Tenta de novo em instantes."
    if result.reason == "network_error":
        return "🌐 Falha de conexão com o provedor de imagem. Tenta novamente."
    if result.reason == "provider_blocked":
        return (
            "🛡️ O provedor bloqueou este pedido por política interna. "
            "Tenta reformular o prompt."
        )
    return (
        "🖼️ Não consegui gerar imagem agora (o provedor respondeu sem imagem). "
        "Tenta reescrever o pedido."
    )


async def _generate_with_gemini(
    session: aiohttp.ClientSession,
    *,
    api_key: str,
    prompt: str,
    timeout_seconds: float,
) -> ImageGenerationResult:
    if not api_key:
        return ImageGenerationResult(
            ok=False,
            provider="gemini",
            prompt_class="safe",
            reason="missing_key",
        )

    url = C.GEMINI_IMAGEGEN_URL.format(model=C.GEMINI_IMAGEGEN_MODEL)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt.strip()[:2000]}],
            }
        ],
        # A documentação mostra configurar responseModalities=["IMAGE"] pra
        # forçar resposta com imagem. Sem isso o modelo pode responder com texto.
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
        },
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with session.post(
            url, json=payload, headers=headers, timeout=timeout,
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                lowered = body.lower()
                reason: FailureReason = "network_error"
                if resp.status == 408:
                    reason = "timeout"
                elif resp.status in (400, 403, 422, 429):
                    reason = "provider_blocked"
                log.warning(
                    "chatbot: imagegen gemini falhou | status=%s reason=%s body=%s",
                    resp.status,
                    reason,
                    lowered[:250],
                )
                return ImageGenerationResult(
                    ok=False,
                    provider="gemini",
                    prompt_class="safe",
                    reason=reason,
                    detail=f"http_{resp.status}",
                )
            data = await resp.json()
    except aiohttp.ServerTimeoutError:
        return ImageGenerationResult(
            ok=False,
            provider="gemini",
            prompt_class="safe",
            reason="timeout",
        )
    except aiohttp.ClientError as e:
        log.warning("chatbot: imagegen erro de rede: %s", e)
        return ImageGenerationResult(
            ok=False,
            provider="gemini",
            prompt_class="safe",
            reason="network_error",
        )
    except Exception as e:
        log.warning("chatbot: imagegen erro inesperado: %s", e)
        return ImageGenerationResult(
            ok=False,
            provider="gemini",
            prompt_class="safe",
            reason="network_error",
        )

    # Parse: busca inlineData no primeiro candidate
    try:
        candidates = data.get("candidates") or []
        if not candidates:
            log.warning("chatbot: imagegen sem candidates")
            return ImageGenerationResult(
                ok=False,
                provider="gemini",
                prompt_class="safe",
                reason="no_image_returned",
            )
        parts = candidates[0].get("content", {}).get("parts", [])
    except (AttributeError, TypeError):
        log.warning("chatbot: imagegen resposta malformada")
        return ImageGenerationResult(
            ok=False,
            provider="gemini",
            prompt_class="safe",
            reason="no_image_returned",
        )

    caption_parts: list[str] = []
    image_data: Optional[bytes] = None
    image_mime = "image/png"
    for part in parts:
        if not isinstance(part, dict):
            continue
        if "text" in part and part["text"]:
            caption_parts.append(str(part["text"]))
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and isinstance(inline, dict):
            b64 = inline.get("data")
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            if b64:
                try:
                    image_data = base64.b64decode(b64)
                    image_mime = mime
                except (ValueError, TypeError) as e:
                    log.warning("chatbot: imagegen falha decodificar base64: %s", e)
                    continue

    if image_data is None:
        log.info("chatbot: imagegen gemini sem imagem (possível safety/provider block)")
        return ImageGenerationResult(
            ok=False,
            provider="gemini",
            prompt_class="safe",
            reason="no_image_returned",
        )

    caption = " ".join(caption_parts).strip() or None
    return ImageGenerationResult(
        ok=True,
        provider="gemini",
        prompt_class="safe",
        image=GeneratedImage(
            data=image_data,
            mime_type=image_mime,
            caption=caption,
        ),
    )


async def _generate_with_adult_provider(
    session: aiohttp.ClientSession,
    *,
    api_key: str,
    api_url: str,
    model: str,
    prompt: str,
    timeout_seconds: float,
) -> ImageGenerationResult:
    if not api_key or not api_url or not model:
        return ImageGenerationResult(
            ok=False,
            provider="adult",
            prompt_class="adult_allowed",
            reason="missing_key",
        )

    payload = {
        "model": model,
        "prompt": prompt.strip()[:2000],
        "response_format": "b64_json",
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with session.post(api_url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status >= 400:
                body = (await resp.text()).lower()
                reason: FailureReason = "network_error"
                if resp.status == 408:
                    reason = "timeout"
                elif resp.status in (400, 401, 403, 422, 429):
                    reason = "provider_blocked"
                log.warning(
                    "chatbot: imagegen adult falhou | status=%s reason=%s body=%s",
                    resp.status,
                    reason,
                    body[:250],
                )
                return ImageGenerationResult(
                    ok=False,
                    provider="adult",
                    prompt_class="adult_allowed",
                    reason=reason,
                    detail=f"http_{resp.status}",
                )
            data = await resp.json()
    except aiohttp.ServerTimeoutError:
        return ImageGenerationResult(
            ok=False,
            provider="adult",
            prompt_class="adult_allowed",
            reason="timeout",
        )
    except aiohttp.ClientError as e:
        log.warning("chatbot: imagegen adult erro de rede: %s", e)
        return ImageGenerationResult(
            ok=False,
            provider="adult",
            prompt_class="adult_allowed",
            reason="network_error",
        )
    except Exception as e:
        log.warning("chatbot: imagegen adult erro inesperado: %s", e)
        return ImageGenerationResult(
            ok=False,
            provider="adult",
            prompt_class="adult_allowed",
            reason="network_error",
        )

    try:
        items = data.get("data") or []
        first = items[0] if items else {}
        b64 = first.get("b64_json")
        if not b64:
            return ImageGenerationResult(
                ok=False,
                provider="adult",
                prompt_class="adult_allowed",
                reason="no_image_returned",
            )
        image_data = base64.b64decode(b64)
    except Exception:
        return ImageGenerationResult(
            ok=False,
            provider="adult",
            prompt_class="adult_allowed",
            reason="no_image_returned",
        )

    return ImageGenerationResult(
        ok=True,
        provider="adult",
        prompt_class="adult_allowed",
        image=GeneratedImage(data=image_data, mime_type="image/png"),
    )


async def _generate_with_huggingface(
    session: aiohttp.ClientSession,
    *,
    api_key: str,
    model: str,
    prompt: str,
    timeout_seconds: float,
) -> ImageGenerationResult:
    if not api_key or not model:
        return ImageGenerationResult(
            ok=False,
            provider="adult_hf",
            prompt_class="adult_allowed",
            reason="missing_key",
        )

    url = f"https://api-inference.huggingface.co/models/{model}"
    payload = {
        "inputs": prompt.strip()[:2000],
        "parameters": {"num_inference_steps": 28, "guidance_scale": 7.0},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if resp.status >= 400:
                body = (await resp.text()).lower()
                reason: FailureReason = "network_error"
                if resp.status == 408:
                    reason = "timeout"
                elif resp.status in (400, 401, 403, 422, 429, 503):
                    reason = "provider_blocked"
                log.warning(
                    "chatbot: imagegen hf falhou | status=%s reason=%s body=%s",
                    resp.status,
                    reason,
                    body[:250],
                )
                return ImageGenerationResult(
                    ok=False,
                    provider="adult_hf",
                    prompt_class="adult_allowed",
                    reason=reason,
                    detail=f"http_{resp.status}",
                )

            if "image/" in content_type:
                data = await resp.read()
                if not data:
                    return ImageGenerationResult(
                        ok=False,
                        provider="adult_hf",
                        prompt_class="adult_allowed",
                        reason="no_image_returned",
                    )
                return ImageGenerationResult(
                    ok=True,
                    provider="adult_hf",
                    prompt_class="adult_allowed",
                    image=GeneratedImage(data=data, mime_type=content_type.split(";")[0]),
                )

            payload_json = await resp.json()
            if isinstance(payload_json, dict) and payload_json.get("error"):
                return ImageGenerationResult(
                    ok=False,
                    provider="adult_hf",
                    prompt_class="adult_allowed",
                    reason="provider_blocked",
                    detail=str(payload_json.get("error"))[:200],
                )
            return ImageGenerationResult(
                ok=False,
                provider="adult_hf",
                prompt_class="adult_allowed",
                reason="no_image_returned",
            )
    except aiohttp.ServerTimeoutError:
        return ImageGenerationResult(
            ok=False,
            provider="adult_hf",
            prompt_class="adult_allowed",
            reason="timeout",
        )
    except aiohttp.ClientError as e:
        log.warning("chatbot: imagegen hf erro de rede: %s", e)
        return ImageGenerationResult(
            ok=False,
            provider="adult_hf",
            prompt_class="adult_allowed",
            reason="network_error",
        )
    except Exception as e:
        log.warning("chatbot: imagegen hf erro inesperado: %s", e)
        return ImageGenerationResult(
            ok=False,
            provider="adult_hf",
            prompt_class="adult_allowed",
            reason="network_error",
        )


async def generate_image(
    session: aiohttp.ClientSession,
    *,
    prompt: str,
    channel_is_nsfw: bool,
    timeout_seconds: float = 45.0,
) -> ImageGenerationResult:
    """Geração de imagem com roteamento multi-provider (safe/adulto)."""
    prompt_clean = (prompt or "").strip()
    pclass = classify_image_prompt(prompt_clean)
    prompt_hint = _prompt_preview(prompt_clean)
    log.info(
        "chatbot: imagegen classify | class=%s nsfw_channel=%s prompt=%r",
        pclass,
        channel_is_nsfw,
        prompt_hint,
    )

    if pclass == "blocked":
        return ImageGenerationResult(
            ok=False,
            provider="router",
            prompt_class=pclass,
            reason="policy_blocked",
        )
    if pclass == "adult_allowed" and not channel_is_nsfw:
        return ImageGenerationResult(
            ok=False,
            provider="router",
            prompt_class=pclass,
            reason="channel_not_nsfw",
        )

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    adult_key = os.environ.get("ADULT_IMAGEGEN_API_KEY", "").strip()
    adult_url = os.environ.get("ADULT_IMAGEGEN_URL", "").strip()
    adult_model = os.environ.get("ADULT_IMAGEGEN_MODEL", "").strip()
    adult_provider = (os.environ.get("ADULT_IMAGEGEN_PROVIDER", "huggingface") or "huggingface").strip().lower()

    if pclass == "adult_allowed":
        if adult_provider in ("huggingface", "hf"):
            hf_model = adult_model or "Linaqruf/anything-v3.0"
            return await _generate_with_huggingface(
                session,
                api_key=adult_key,
                model=hf_model,
                prompt=prompt_clean,
                timeout_seconds=timeout_seconds,
            )
        result = await _generate_with_adult_provider(
            session,
            api_key=adult_key,
            api_url=adult_url,
            model=adult_model,
            prompt=prompt_clean,
            timeout_seconds=timeout_seconds,
        )
        return result

    result = await _generate_with_gemini(
        session,
        api_key=gemini_key,
        prompt=prompt_clean,
        timeout_seconds=timeout_seconds,
    )
    return result
