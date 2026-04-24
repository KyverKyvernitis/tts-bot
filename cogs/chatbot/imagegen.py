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

import asyncio
import base64
import binascii
import logging
import os
import re
import time
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
IntentCategory = Literal["safe", "adult_allowed"]
FailureReason = Literal[
    "provider_blocked",
    "policy_blocked",
    "channel_not_nsfw",
    "missing_key",
    "no_worker",
    "network_error",
    "timeout",
    "no_image_returned",
    "prompt_too_vague",
]


@dataclass(frozen=True)
class ImageGenerationResult:
    ok: bool
    provider: str
    prompt_class: PromptClass
    image: Optional[GeneratedImage] = None
    reason: Optional[FailureReason] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class ImageIntent:
    requested: bool
    category: IntentCategory
    prompt: str


# -----------------------------------------------------------------------------
# Detecção de pedido implícito de imagem no texto do user
# -----------------------------------------------------------------------------

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
    return parse_image_intent(text).requested


def extract_image_prompt(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    patterns = [
        r"^(gera|gere|desenha|cria|faz|faça|imagina|me mostra)\s+",
        r"^(uma|um)\s+",
        r"^(imagem|foto|figura|desenho|arte|ilustra[cç][aã]o|cena)\s+(de\s+|com\s+)?",
    ]
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()
    return text or "uma imagem"


def parse_image_intent(text: str) -> ImageIntent:
    raw = (text or "").strip()
    if not raw:
        return ImageIntent(requested=False, category="safe", prompt="")
    requested = bool(_IMAGE_REQUEST_RE.search(raw) or looks_like_adult_image_request(raw))
    prompt = extract_image_prompt(raw)
    category: IntentCategory = "adult_allowed" if text_has_adult_hint(prompt) else "safe"
    return ImageIntent(requested=requested, category=category, prompt=prompt)


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
    r"nudez\b",
    r"sexo\b",
    r"sexual\b",
    r"sensual\b",
    r"porn[oô]\b",
    r"pelad[oa]\b",
    r"peit[oa]s?\b",
    r"seios?\b",
    r"mamil[oa]s?\b",
    r"bunda(s)?\b",
    r"raba\b",
    r"vagina\b",
    r"p[eê]nis\b",
    r"genit[aá]lia",
    r"boobs?\b",
    r"breasts?\b",
    r"naked\b",
    r"nude\b",
    r"hentai\b",
)
_ADULT_IMAGE_VERB_RE = re.compile(
    r"\b(gere|gera|gerar|cria|crie|criar|desenha|desenhe|desenhar|faz|faça|faca|mostrar?|mostre)\b",
    re.IGNORECASE | re.UNICODE,
)
_GENERIC_ADULT_WORDS_RE = re.compile(
    r"\b(nsfw|18\+|adult[oa]s?|conte[uú]do|er[oó]tic[oa]s?|sensual|sexual|sexo|porn[oô]|nudez|nude|nud[eo]s?|pelad[oa]s?|hentai|imagem|foto|arte|desenho|figura|gera|gere|gerar|cria|crie|criar|desenha|desenhe|desenhar|faz|faça|faca|mostra|mostrar|mostre|manda|mande|me|de|com|uma|um|a|o)\b",
    re.IGNORECASE | re.UNICODE,
)
_NONCONSENSUAL_LEAK_PATTERNS = (
    r"vazad[oa]s?\b",
    r"vazamento\s+de\s+nude",
    r"nudes?\s+vazad[oa]s?",
)
_REAL_PERSON_ADULT_PATTERNS = (
    r"pessoa\s+real\b",
    r"mulher\s+real\b",
    r"homem\s+real\b",
    r"minha\s+ex\b",
    r"meu\s+ex\b",
    r"minha\s+namorada\b",
    r"meu\s+namorado\b",
    r"instagram\b",
    r"onlyfans\b",
)


def text_has_adult_hint(text: str) -> bool:
    return any(re.search(pat, text or "", flags=re.IGNORECASE) for pat in _ADULT_HINT_PATTERNS)


def looks_like_adult_image_request(text: str) -> bool:
    if not text:
        return False
    return bool(_ADULT_IMAGE_VERB_RE.search(text) and text_has_adult_hint(text))


def is_prompt_too_vague_for_adult_image(prompt: str) -> bool:
    text = re.sub(r"[^\wÀ-ÿ+]+", " ", (prompt or "").lower(), flags=re.UNICODE)
    text = _GENERIC_ADULT_WORDS_RE.sub(" ", text)
    words = [w for w in text.split() if len(w) >= 3]
    return not words


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
    if (
        any(re.search(pat, text, flags=re.IGNORECASE) for pat in _ADULT_HINT_PATTERNS)
        and any(re.search(pat, text, flags=re.IGNORECASE) for pat in _REAL_PERSON_ADULT_PATTERNS)
    ):
        return "blocked"
    if any(re.search(pat, text, flags=re.IGNORECASE) for pat in _NONCONSENSUAL_LEAK_PATTERNS):
        return "blocked"
    if text_has_adult_hint(text):
        return "adult_allowed"
    return "safe"


def _prompt_preview(prompt: str, *, max_len: int = 120) -> str:
    compact = re.sub(r"\s+", " ", (prompt or "").strip())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3] + "..."


def build_image_failure_message(result: ImageGenerationResult) -> str:
    if result.reason == "prompt_too_vague":
        return (
            "🖼️ O pedido de imagem ficou vago demais. "
            "Diga o que deve aparecer na imagem, com assunto e estilo. "
            "Ex.: `gere uma imagem de uma personagem adulta fictícia, estilo anime, ...`."
        )
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
            return "⚙️ Geração adulta está indisponível no momento."
        return "⚙️ Geração de imagem não configurada no momento."
    if result.reason == "no_worker":
        return "🧵 Nenhum worker adulto disponível agora. Tente novamente em instantes."
    if result.reason == "timeout":
        return "⏱️ O provedor adulto demorou demais para responder."
    if result.reason == "network_error":
        return "🌐 Falha de conexão com o provedor de imagem. Tenta novamente."
    if result.reason == "provider_blocked":
        return "🛡️ O provedor adulto bloqueou este pedido por política interna."
    if result.reason == "no_image_returned":
        return "🖼️ O provedor adulto respondeu sem imagem. Tente descrever melhor a cena."
    return (
        "🖼️ Não consegui gerar imagem agora (o provedor respondeu sem imagem). "
        "Tenta reescrever o pedido."
    )


def _is_retryable_adult_failure(result: ImageGenerationResult) -> bool:
    return (
        not result.ok
        and result.prompt_class == "adult_allowed"
        and result.reason in ("timeout", "no_worker", "network_error")
    )


def _hf_fallback_models(adult_model: str) -> list[str]:
    raw = (adult_model or "").strip()
    if raw:
        candidates = [m.strip() for m in raw.split(",") if m.strip()]
        if candidates:
            return candidates
    # Modelos atualizados pra HF Inference API em 2026. Os antigos
    # (runwayml/stable-diffusion-v1-5, Linaqruf/animagine-xl-3.1) foram
    # removidos/gated em 2025. FLUX.1-schnell é o default serverless agora.
    # Nota: HF Inference API aplica moderation no serverless pra NSFW, então
    # pedidos explícitos frequentemente retornam 403 — é esperado; o router
    # trata como provider_blocked e segue pro próximo.
    return [
        "black-forest-labs/FLUX.1-schnell",
        "stabilityai/stable-diffusion-xl-base-1.0",
    ]


def _aihorde_default_models(adult_model: str) -> list[str]:
    raw = (adult_model or "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return [
        "AlbedoBase XL",
        "Hassaku",
        "AOM3",
        "Deliberate",
    ]


_ADULT_QUALITY_PREFIX = (
    "masterpiece, best quality, highly detailed, sharp focus, "
    "beautiful lighting, intricate details, "
)

_ADULT_NEGATIVE_PROMPT = (
    "lowres, worst quality, low quality, normal quality, jpeg artifacts, "
    "blurry, out of focus, bad anatomy, bad hands, extra fingers, "
    "fewer fingers, extra digits, missing fingers, missing limbs, "
    "extra limbs, malformed limbs, deformed, disfigured, mutation, mutated, "
    "ugly, poorly drawn face, poorly drawn hands, bad proportions, "
    "cloned face, long neck, signature, watermark, text, username, "
    "logo, cropped, duplicate, error, child, underage, loli, shota"
)


def _augment_adult_prompt(prompt: str) -> tuple[str, str]:
    text = (prompt or "").strip()
    if "###" in text:
        pos, _, neg_user = text.partition("###")
        pos = pos.strip()
        neg_user = neg_user.strip()
    else:
        pos = text
        neg_user = ""

    if pos and not pos.lower().startswith(("masterpiece", "best quality")):
        pos = _ADULT_QUALITY_PREFIX + pos

    negative = _ADULT_NEGATIVE_PROMPT
    if neg_user:
        negative = f"{neg_user}, {negative}"
    return pos, negative


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

    pos, neg = _augment_adult_prompt(prompt)
    payload = {
        "model": model,
        "prompt": pos[:2000],
        "negative_prompt": neg[:1500],
        "response_format": "b64_json",
        "size": "512x768",
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
                elif resp.status == 429:
                    # Rate limit — tratável como "sem worker agora", retryable
                    reason = "no_worker"
                elif resp.status in (400, 401, 403, 422):
                    reason = "provider_blocked"
                elif resp.status in (500, 502, 503, 504):
                    reason = "no_worker"
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
    pos, neg = _augment_adult_prompt(prompt)
    payload = {
        "inputs": pos[:2000],
        "parameters": {
            "num_inference_steps": 35,
            "guidance_scale": 7.5,
            "negative_prompt": neg[:1500],
            "width": 512,
            "height": 768,
        },
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
                elif resp.status == 429:
                    # Rate limit do HF — retryable, não é bloqueio de política
                    reason = "no_worker"
                elif resp.status in (400, 401, 403, 422):
                    reason = "provider_blocked"
                elif resp.status in (500, 502, 503, 504):
                    reason = "no_worker"
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


async def _generate_with_aihorde(
    session: aiohttp.ClientSession,
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    timeout_seconds: float,
    is_nsfw: bool = True,
) -> ImageGenerationResult:
    base = (base_url or "https://aihorde.net/api").rstrip("/")
    key = (api_key or "0000000000").strip() or "0000000000"
    # Pra SFW, não aplica o prompt adulto (que adiciona negativos NSFW-specific
    # e prefixos de qualidade de anime/NSFW). Usa prompt limpo direto.
    if is_nsfw:
        pos, neg = _augment_adult_prompt(prompt)
        prompt_for_horde = f"{pos} ### {neg}" if neg else pos
    else:
        prompt_for_horde = prompt
    prompt_for_horde = prompt_for_horde[:2000]
    # Timeouts desacoplados:
    #   - http_timeout: limite por chamada HTTP individual (submit, check, status, fetch).
    #     Curto porque cada request é leve; se travar, desiste rápido sem queimar o deadline.
    #   - poll_deadline: limite total pro loop de polling da fila do Horde. Fila NSFW
    #     pode levar minutos em horário de pico, então damos no mínimo 4 minutos.
    http_timeout = aiohttp.ClientTimeout(total=20)
    poll_deadline = time.monotonic() + max(timeout_seconds, 240.0)
    headers = {
        "Content-Type": "application/json",
        "apikey": key,
        "Client-Agent": "tts-bot:adult-imagegen:1.1",
    }
    models = _aihorde_default_models(model)
    # params enxuto: cada campo extra estreita o pool de workers voluntários que topam
    # o job. Removemos post_processing (exige GFPGAN instalado), karras e clip_skip
    # (suporte inconsistente em workers antigos). steps=15 é suficiente pra SDXL
    # (sampler k_euler_a converge rápido) e corta ~50% do tempo de geração no worker.
    submit_payload: dict[str, object] = {
        "prompt": prompt_for_horde,
        "nsfw": is_nsfw,
        "censor_nsfw": False,
        "replacement_filter": False,
        "trusted_workers": False,
        "slow_workers": True,
        "allow_downgrade": True,
        "r2": True,  # resultado vai pro storage do Horde; worker libera memória antes
        "params": {
            "n": 1,
            "width": 512,
            "height": 768,
            "steps": 15,
            "cfg_scale": 7.0,
            "sampler_name": "k_euler_a",
        },
    }
    if models:
        submit_payload["models"] = models

    try:
        async with session.post(
            f"{base}/v2/generate/async",
            json=submit_payload,
            headers=headers,
            timeout=http_timeout,
        ) as resp:
            if resp.status >= 400:
                body = (await resp.text()).lower()
                reason: FailureReason = "network_error"
                if resp.status == 408:
                    reason = "timeout"
                elif resp.status == 429:
                    reason = "no_worker"
                elif resp.status in (400, 401, 403, 422):
                    reason = "provider_blocked"
                elif resp.status in (500, 502, 503, 504):
                    reason = "no_worker"
                log.warning(
                    "chatbot: imagegen aihorde submit falhou | status=%s reason=%s body=%s",
                    resp.status,
                    reason,
                    body[:250],
                )
                return ImageGenerationResult(
                    ok=False,
                    provider="aihorde",
                    prompt_class="adult_allowed",
                    reason=reason,
                    detail=f"http_{resp.status}",
                )
            submit_data = await resp.json()
    except aiohttp.ServerTimeoutError:
        return ImageGenerationResult(
            ok=False,
            provider="aihorde",
            prompt_class="adult_allowed",
            reason="timeout",
        )
    except aiohttp.ClientError as e:
        log.warning("chatbot: imagegen aihorde erro de rede submit: %s", e)
        return ImageGenerationResult(
            ok=False,
            provider="aihorde",
            prompt_class="adult_allowed",
            reason="network_error",
        )

    req_id = str((submit_data or {}).get("id") or "").strip()
    if not req_id:
        return ImageGenerationResult(
            ok=False,
            provider="aihorde",
            prompt_class="adult_allowed",
            reason="no_image_returned",
        )

    deadline = poll_deadline
    done = False
    faulted = False
    last_logged_queue: tuple[int, float] | None = None
    try:
        while time.monotonic() < deadline:
            async with session.get(
                f"{base}/v2/generate/check/{req_id}",
                headers=headers,
                timeout=http_timeout,
            ) as resp:
                if resp.status >= 400:
                    return ImageGenerationResult(
                        ok=False,
                        provider="aihorde",
                        prompt_class="adult_allowed",
                        reason="network_error",
                        detail=f"http_{resp.status}",
                    )
                check = await resp.json()

            done = bool((check or {}).get("done"))
            faulted = bool((check or {}).get("faulted"))
            # Observabilidade: loga posição na fila e wait_time estimado.
            # Só loga quando muda significativamente pra não poluir o journalctl.
            queue_pos = int((check or {}).get("queue_position") or 0)
            wait_time = float((check or {}).get("wait_time") or 0)
            snapshot = (queue_pos, wait_time)
            if last_logged_queue is None or abs(snapshot[0] - last_logged_queue[0]) >= 3:
                log.info(
                    "chatbot: imagegen aihorde fila | id=%s queue_pos=%s wait_time=%.0fs",
                    req_id,
                    queue_pos,
                    wait_time,
                )
                last_logged_queue = snapshot
            if done or faulted:
                break
            await asyncio.sleep(1.5)
    except aiohttp.ServerTimeoutError:
        return ImageGenerationResult(
            ok=False,
            provider="aihorde",
            prompt_class="adult_allowed",
            reason="timeout",
        )
    except aiohttp.ClientError as e:
        log.warning("chatbot: imagegen aihorde erro de rede check: %s", e)
        return ImageGenerationResult(
            ok=False,
            provider="aihorde",
            prompt_class="adult_allowed",
            reason="network_error",
        )

    if not done or faulted:
        return ImageGenerationResult(
            ok=False,
            provider="aihorde",
            prompt_class="adult_allowed",
            reason=("no_worker" if faulted else "timeout"),
            detail=("faulted" if faulted else "queue_timeout"),
        )

    try:
        async with session.get(
            f"{base}/v2/generate/status/{req_id}",
            headers=headers,
            timeout=http_timeout,
        ) as resp:
            if resp.status >= 400:
                return ImageGenerationResult(
                    ok=False,
                    provider="aihorde",
                    prompt_class="adult_allowed",
                    reason="network_error",
                    detail=f"http_{resp.status}",
                )
            status_data = await resp.json()
    except aiohttp.ServerTimeoutError:
        return ImageGenerationResult(
            ok=False,
            provider="aihorde",
            prompt_class="adult_allowed",
            reason="timeout",
        )
    except aiohttp.ClientError as e:
        log.warning("chatbot: imagegen aihorde erro de rede status: %s", e)
        return ImageGenerationResult(
            ok=False,
            provider="aihorde",
            prompt_class="adult_allowed",
            reason="network_error",
        )

    gens = (status_data or {}).get("generations") or []
    first = gens[0] if gens else {}
    img_ref = ""
    if isinstance(first, dict):
        img_ref = str(first.get("img") or first.get("image") or "").strip()
    if not img_ref:
        return ImageGenerationResult(
            ok=False,
            provider="aihorde",
            prompt_class="adult_allowed",
            reason="no_image_returned",
        )

    if img_ref.startswith(("http://", "https://")):
        try:
            async with session.get(img_ref, timeout=http_timeout) as img_resp:
                if img_resp.status >= 400:
                    return ImageGenerationResult(
                        ok=False,
                        provider="aihorde",
                        prompt_class="adult_allowed",
                        reason="no_image_returned",
                    )
                content_type = (img_resp.headers.get("Content-Type") or "image/png").split(";")[0]
                data = await img_resp.read()
                if not data:
                    return ImageGenerationResult(
                        ok=False,
                        provider="aihorde",
                        prompt_class="adult_allowed",
                        reason="no_image_returned",
                    )
                return ImageGenerationResult(
                    ok=True,
                    provider="aihorde",
                    prompt_class="adult_allowed",
                    image=GeneratedImage(data=data, mime_type=content_type),
                )
        except aiohttp.ClientError as e:
            log.warning("chatbot: imagegen aihorde erro baixar img: %s", e)
            return ImageGenerationResult(
                ok=False,
                provider="aihorde",
                prompt_class="adult_allowed",
                reason="network_error",
            )

    raw_b64 = img_ref.split(",", 1)[-1].strip()
    try:
        decoded = base64.b64decode(raw_b64)
    except (binascii.Error, ValueError):
        return ImageGenerationResult(
            ok=False,
            provider="aihorde",
            prompt_class="adult_allowed",
            reason="no_image_returned",
        )
    return ImageGenerationResult(
        ok=True,
        provider="aihorde",
        prompt_class="adult_allowed",
        image=GeneratedImage(data=decoded, mime_type="image/png"),
    )


async def generate_image(
    session: aiohttp.ClientSession,
    *,
    prompt: str,
    channel_is_nsfw: bool,
    timeout_seconds: float = 45.0,
) -> ImageGenerationResult:
    """Geração de imagem com roteamento multi-provider por perfil.

    Perfil = (nsfw, style) onde style ∈ {realistic, anime, generic}.
    Cada perfil tem uma ordem de preferência de providers (ver
    `image_providers_ext.provider_order_for_profile`). Router itera a ordem,
    pula providers não configurados, faz fallback se erro for retryable.
    """
    from . import image_providers_ext as ext

    prompt_clean = (prompt or "").strip()
    pclass = classify_image_prompt(prompt_clean)

    # Gates de política/canal (mantidos exatamente como antes).
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
    if pclass == "adult_allowed" and is_prompt_too_vague_for_adult_image(prompt_clean):
        return ImageGenerationResult(
            ok=False,
            provider="router",
            prompt_class=pclass,
            reason="prompt_too_vague",
        )

    # Perfil derivado: nsfw (binário) + estilo (anime/realistic/generic).
    profile = ext.ImageProfile(
        nsfw=(pclass == "adult_allowed"),
        style=ext.detect_style(prompt_clean),
    )
    log.info(
        "chatbot: imagegen classify | profile=%s nsfw_channel=%s prompt=%r",
        profile.describe(),
        channel_is_nsfw,
        ("<adult:redacted>" if profile.nsfw else _prompt_preview(prompt_clean)),
    )

    # Env vars (todas opcionais — providers sem chave são pulados).
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    raw_adult_key = (os.environ.get("ADULT_IMAGEGEN_API_KEY") or "").strip()
    aihorde_key = raw_adult_key or "0000000000"
    hf_key = os.environ.get("HUGGINGFACE_API_KEY", "").strip() or raw_adult_key
    custom_key = raw_adult_key
    adult_url = (
        os.environ.get("ADULT_IMAGEGEN_URL", "https://aihorde.net/api").strip()
        or "https://aihorde.net/api"
    )
    adult_model_override = os.environ.get("ADULT_IMAGEGEN_MODEL", "").strip()
    pollinations_key = os.environ.get("POLLINATIONS_API_KEY", "").strip()
    cf_account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()

    # Override de provider via .env continua funcionando (bypass do router).
    adult_provider = (
        os.environ.get("ADULT_IMAGEGEN_PROVIDER", "auto") or "auto"
    ).strip().lower()
    use_auto_router = adult_provider in ("auto", "auto_free", "free")

    # Timeout mais generoso pra NSFW (fila do Horde) e razoável pra SFW.
    eff_timeout = max(timeout_seconds, 120.0 if profile.nsfw else 60.0)

    # Modo legacy: usuário forçou um provider específico. Respeita e sai.
    if not use_auto_router and profile.nsfw:
        return await _run_legacy_nsfw_provider(
            session,
            adult_provider=adult_provider,
            profile=profile,
            prompt_clean=prompt_clean,
            aihorde_key=aihorde_key,
            hf_key=hf_key,
            custom_key=custom_key,
            adult_url=adult_url,
            adult_model_override=adult_model_override,
            eff_timeout=eff_timeout,
        )

    # Router: itera providers na ordem de preferência pro perfil.
    order = ext.provider_order_for_profile(profile)
    log.info(
        "chatbot: imagegen router order | profile=%s order=%s",
        profile.describe(),
        order,
    )

    attempts: list[ImageGenerationResult] = []
    for provider_name in order:
        result = await _try_provider(
            provider_name,
            session=session,
            profile=profile,
            prompt_clean=prompt_clean,
            timeout_seconds=eff_timeout,
            gemini_key=gemini_key,
            aihorde_key=aihorde_key,
            hf_key=hf_key,
            custom_key=custom_key,
            adult_url=adult_url,
            adult_model_override=adult_model_override,
            pollinations_key=pollinations_key,
            cf_account=cf_account,
            cf_token=cf_token,
        )
        if result is None:
            # Provider não configurado (sem chave) ou não aplicável ao perfil.
            continue
        attempts.append(result)
        if result.ok:
            log.info(
                "chatbot: imagegen router ok | provider=%s profile=%s",
                result.provider,
                profile.describe(),
            )
            return result

    # Todos os providers falharam. Retorna o erro mais informativo.
    if not attempts:
        return ImageGenerationResult(
            ok=False,
            provider="router",
            prompt_class=pclass,
            reason="missing_key",
        )
    # Prioriza erros "acionáveis" pro user sobre erros de infra.
    # Ordem: missing_key > provider_blocked > no_image_returned > no_worker
    #      > timeout > network_error.
    # (Missing_key fica no topo se TODOS os providers faltarem chave — sinal
    # claro pro admin que precisa configurar algo.)
    if all(a.reason == "missing_key" for a in attempts):
        return attempts[0]
    priority = [
        "provider_blocked",
        "no_image_returned",
        "no_worker",
        "timeout",
        "network_error",
        "missing_key",
    ]
    for want in priority:
        for a in attempts:
            if a.reason == want:
                return a
    return attempts[-1]


async def _try_provider(
    name: str,
    *,
    session: aiohttp.ClientSession,
    profile,  # ext.ImageProfile
    prompt_clean: str,
    timeout_seconds: float,
    gemini_key: str,
    aihorde_key: str,
    hf_key: str,
    custom_key: str,
    adult_url: str,
    adult_model_override: str,
    pollinations_key: str,
    cf_account: str,
    cf_token: str,
) -> ImageGenerationResult | None:
    """Chama o provider `name`. Retorna None se o provider não está
    configurado (router deve pular e ir pro próximo)."""
    from . import image_providers_ext as ext

    if name == "gemini":
        if profile.nsfw or not gemini_key:
            return None
        return await _generate_with_gemini(
            session,
            api_key=gemini_key,
            prompt=prompt_clean,
            timeout_seconds=timeout_seconds,
        )

    if name == "pollinations":
        # Pollinations: SFW livre (sem key). NSFW exige token.
        if profile.nsfw and not pollinations_key:
            return None
        ok, data, mime, reason = await ext.generate_with_pollinations(
            session,
            api_key=pollinations_key,
            prompt=prompt_clean,
            profile=profile,
            timeout_seconds=timeout_seconds,
        )
        if ok:
            return ImageGenerationResult(
                ok=True,
                provider="pollinations",
                prompt_class=("adult_allowed" if profile.nsfw else "safe"),
                image=GeneratedImage(data=data, mime_type=mime or "image/jpeg"),
            )
        return ImageGenerationResult(
            ok=False,
            provider="pollinations",
            prompt_class=("adult_allowed" if profile.nsfw else "safe"),
            reason=reason or "network_error",
        )

    if name == "cloudflare":
        # Cloudflare Workers AI: só SFW. Se não configurado ou se NSFW, pula.
        if profile.nsfw or not cf_account or not cf_token:
            return None
        ok, data, mime, reason = await ext.generate_with_cloudflare(
            session,
            account_id=cf_account,
            api_token=cf_token,
            prompt=prompt_clean,
            profile=profile,
            timeout_seconds=timeout_seconds,
        )
        if ok:
            return ImageGenerationResult(
                ok=True,
                provider="cloudflare",
                prompt_class="safe",
                image=GeneratedImage(data=data, mime_type=mime or "image/png"),
            )
        return ImageGenerationResult(
            ok=False,
            provider="cloudflare",
            prompt_class="safe",
            reason=reason or "network_error",
        )

    if name == "aihorde":
        model_list = ext.aihorde_models_for_profile(profile, override=adult_model_override)
        model_str = ",".join(model_list)
        return await _generate_with_aihorde(
            session,
            api_key=aihorde_key,
            base_url=adult_url,
            model=model_str,
            prompt=prompt_clean,
            timeout_seconds=timeout_seconds,
            is_nsfw=profile.nsfw,
        )

    if name == "huggingface":
        hf_models = _hf_fallback_models(adult_model_override)
        if not hf_models:
            return None
        return await _generate_with_huggingface(
            session,
            api_key=hf_key,
            model=hf_models[0],
            prompt=prompt_clean,
            timeout_seconds=timeout_seconds,
        )

    if name == "adult_custom":
        has_config = bool(
            adult_model_override and adult_url
            and "aihorde.net/api" not in adult_url
        )
        if not has_config:
            return None
        return await _generate_with_adult_provider(
            session,
            api_key=custom_key,
            api_url=adult_url,
            model=adult_model_override,
            prompt=prompt_clean,
            timeout_seconds=timeout_seconds,
        )

    return None


async def _run_legacy_nsfw_provider(
    session: aiohttp.ClientSession,
    *,
    adult_provider: str,
    profile,  # ext.ImageProfile
    prompt_clean: str,
    aihorde_key: str,
    hf_key: str,
    custom_key: str,
    adult_url: str,
    adult_model_override: str,
    eff_timeout: float,
) -> ImageGenerationResult:
    """Modo legacy: ADULT_IMAGEGEN_PROVIDER=aihorde|huggingface|custom força
    usar só aquele provider (bypass do router). Mantido pra compatibilidade
    com quem tinha essa var setada antes."""
    from . import image_providers_ext as ext

    if adult_provider == "aihorde":
        model_list = ext.aihorde_models_for_profile(profile, override=adult_model_override)
        return await _generate_with_aihorde(
            session,
            api_key=aihorde_key,
            base_url=adult_url,
            model=",".join(model_list),
            prompt=prompt_clean,
            timeout_seconds=eff_timeout,
            is_nsfw=profile.nsfw,
        )
    if adult_provider in ("huggingface", "hf"):
        attempts: list[ImageGenerationResult] = []
        for hf_model in _hf_fallback_models(adult_model_override):
            result = await _generate_with_huggingface(
                session,
                api_key=hf_key,
                model=hf_model,
                prompt=prompt_clean,
                timeout_seconds=eff_timeout,
            )
            attempts.append(result)
            if result.ok:
                return result
            if not _is_retryable_adult_failure(result):
                return result
        return attempts[-1] if attempts else ImageGenerationResult(
            ok=False,
            provider="adult_hf",
            prompt_class="adult_allowed",
            reason="missing_key",
        )
    # Custom endpoint.
    return await _generate_with_adult_provider(
        session,
        api_key=custom_key,
        api_url=adult_url,
        model=adult_model_override,
        prompt=prompt_clean,
        timeout_seconds=eff_timeout,
    )
