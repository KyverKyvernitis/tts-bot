"""Providers de imagem adicionais e roteamento por perfil (estilo, NSFW).

Estende o imagegen.py com:
- Classificação de estilo (realistic/anime/generic) além do nsfw/safe.
- Provider Pollinations (URL-based, sem chave pra SFW, com token opcional pra NSFW).
- Provider Cloudflare Workers AI (10k neurons/dia grátis, FLUX schnell, só SFW).
- Seleção de modelos do AI Horde por perfil (anime NSFW vai pro Pony, realistic NSFW
  vai pro Juggernaut, etc).

Env vars usadas:
- POLLINATIONS_API_KEY: opcional, aumenta rate limit e libera NSFW.
- CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN: opcional, habilita Cloudflare Workers AI.

Ambos os providers novos são pulados silenciosamente se as chaves não tiverem setadas.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

Style = Literal["realistic", "anime", "generic"]


# -----------------------------------------------------------------------------
# Classificação de estilo
# -----------------------------------------------------------------------------

# Palavras que indicam estilo anime/desenho/ilustração. Pegadas em PT e EN.
_STYLE_ANIME_RE = re.compile(
    r"\b("
    r"anime|manga|mang[aá]|hentai|cartoon|desenho|ilustra[cç][aã]o|"
    r"pixiv|waifu|chibi|kawaii|"
    r"estilo\s+anime|estilo\s+mang[aá]|estilo\s+cartoon|estilo\s+desenho|"
    r"2d|toon"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# Palavras que indicam estilo fotorrealista.
_STYLE_REALISTIC_RE = re.compile(
    r"\b("
    r"realista|realistic|realismo|"
    r"foto|photo|photograph|photorealistic|hyperrealistic|hyper-realistic|"
    r"fotorre?alista|fotorre?alismo|fotogr[aá]fic[oa]|"
    r"retrato\s+real|portrait\s+photo|"
    r"dslr|cinematic|raw\s+photo"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


def detect_style(prompt: str) -> Style:
    """Infere o estilo visual do pedido pela menção de palavras-chave.

    Retorna "anime" se encontra marcadores de estilo 2D/desenho,
    "realistic" se encontra marcadores fotográficos, "generic" caso contrário.
    Prioriza anime sobre realistic se as duas aparecerem (raro; geralmente
    o pedido mais específico é o que conta).
    """
    text = prompt or ""
    if _STYLE_ANIME_RE.search(text):
        return "anime"
    if _STYLE_REALISTIC_RE.search(text):
        return "realistic"
    return "generic"


@dataclass(frozen=True)
class ImageProfile:
    """Perfil do pedido, usado pelo router pra escolher providers e modelos."""
    nsfw: bool
    style: Style

    def describe(self) -> str:
        return f"{'nsfw' if self.nsfw else 'sfw'}/{self.style}"


# -----------------------------------------------------------------------------
# Seleção de modelos do AI Horde por perfil
# -----------------------------------------------------------------------------
# Estes são os checkpoints do Horde (nomes canônicos) mais fortes em cada nicho.
# Ordem importa: o primeiro é o preferido, demais são fallback se o primeiro
# não tiver worker. `_AIHORDE_MODELS_OVERRIDE` respeita env var se setada.

# Pra NSFW, lista curta e focada. O Horde só roteia pra workers que rodam
# *exatamente* esses modelos, então lista grande = fila longa. Com 1-2 modelos
# principais, o pool ainda é bom porque são os modelos mais populares entre
# voluntários.
#
# Pra NSFW "generic" (sem marcador claro de estilo), lista vazia = qualquer
# worker NSFW pega. Pool máximo, fila mínima. Qualidade: workers NSFW
# voluntários geralmente rodam Pony/Juggernaut mesmo, então na prática
# as imagens ficam ótimas.

_AIHORDE_MODELS_NSFW_ANIME = [
    # Pony V6 XL é o padrão-ouro de NSFW anime em 2026.
    "Pony Diffusion XL",
]

_AIHORDE_MODELS_NSFW_REALISTIC = [
    # Juggernaut é o mais popular entre workers NSFW realistas.
    "Juggernaut XL",
]

_AIHORDE_MODELS_NSFW_GENERIC: list[str] = []  # Qualquer worker NSFW.

_AIHORDE_MODELS_SFW_ANIME = [
    "Animagine XL",
    "Illustrious XL",
]

_AIHORDE_MODELS_SFW_REALISTIC = [
    "AlbedoBase XL",
    "Juggernaut XL",
]

_AIHORDE_MODELS_SFW_GENERIC = [
    "AlbedoBase XL",
    "Deliberate",
]


def aihorde_models_for_profile(profile: ImageProfile, override: str = "") -> list[str]:
    """Escolhe a lista de modelos do Horde pro perfil, ou respeita override do .env."""
    raw = (override or "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]

    if profile.nsfw:
        if profile.style == "anime":
            return list(_AIHORDE_MODELS_NSFW_ANIME)
        if profile.style == "realistic":
            return list(_AIHORDE_MODELS_NSFW_REALISTIC)
        return list(_AIHORDE_MODELS_NSFW_GENERIC)

    if profile.style == "anime":
        return list(_AIHORDE_MODELS_SFW_ANIME)
    if profile.style == "realistic":
        return list(_AIHORDE_MODELS_SFW_REALISTIC)
    return list(_AIHORDE_MODELS_SFW_GENERIC)


# -----------------------------------------------------------------------------
# Provider: Pollinations
# -----------------------------------------------------------------------------
# Endpoint: https://image.pollinations.ai/prompt/{prompt}?model=X&width=W&height=H
# SFW funciona sem chave. NSFW precisa de token (registra em auth.pollinations.ai).
# O token vai na query string como `token=...` ou no header `Authorization: Bearer ...`.
# Retorno é a imagem direto (binary), com Content-Type image/jpeg ou image/png.

_POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"


def _pollinations_model(profile: ImageProfile) -> str:
    """Escolhe variante do FLUX no Pollinations pelo perfil."""
    if profile.style == "anime":
        return "flux-anime"
    if profile.style == "realistic":
        return "flux-realism"
    return "flux"


async def generate_with_pollinations(
    session: aiohttp.ClientSession,
    *,
    api_key: str,
    prompt: str,
    profile: ImageProfile,
    timeout_seconds: float,
) -> tuple[bool, Optional[bytes], Optional[str], str]:
    """Gera imagem via Pollinations.

    Retorna (ok, image_bytes, mime_type, failure_reason). Se ok=True, os 3
    primeiros estão preenchidos. Se ok=False, `failure_reason` identifica
    o motivo ("missing_key" só se NSFW sem token, "provider_blocked",
    "network_error", "timeout", "no_image_returned").
    """
    # SFW roda sem chave. Pra NSFW, chave é necessária (política do provider).
    if profile.nsfw and not api_key:
        return False, None, None, "missing_key"

    model = _pollinations_model(profile)
    width, height = (768, 1024) if profile.style != "generic" else (1024, 1024)

    # Monta URL: prompt vai no path (encoded), params na query.
    encoded_prompt = quote(prompt[:1500], safe="")
    url = f"{_POLLINATIONS_BASE}/{encoded_prompt}"
    params = {
        "model": model,
        "width": str(width),
        "height": str(height),
        "nologo": "true",
        "enhance": "false",  # já temos nosso próprio augment
        "safe": "false" if profile.nsfw else "true",
    }
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with session.get(
            url, params=params, headers=headers, timeout=timeout,
        ) as resp:
            if resp.status >= 400:
                body = (await resp.text())[:250].lower()
                if resp.status == 408:
                    reason = "timeout"
                elif resp.status == 429:
                    reason = "no_worker"
                elif resp.status in (400, 401, 403, 422):
                    reason = "provider_blocked"
                elif resp.status in (500, 502, 503, 504):
                    reason = "no_worker"
                else:
                    reason = "network_error"
                log.warning(
                    "chatbot: imagegen pollinations falhou | status=%s reason=%s body=%s",
                    resp.status, reason, body,
                )
                return False, None, None, reason

            content_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0]
            if "image/" not in content_type:
                # Às vezes retorna JSON com erro e status 200.
                text = (await resp.text())[:250]
                log.warning(
                    "chatbot: imagegen pollinations sem imagem | content_type=%s body=%s",
                    content_type, text,
                )
                return False, None, None, "no_image_returned"

            data = await resp.read()
            if not data:
                return False, None, None, "no_image_returned"
            return True, data, content_type, ""
    except aiohttp.ServerTimeoutError:
        return False, None, None, "timeout"
    except aiohttp.ClientError as e:
        log.warning("chatbot: imagegen pollinations erro de rede: %s", e)
        return False, None, None, "network_error"
    except Exception as e:
        log.warning("chatbot: imagegen pollinations erro inesperado: %s", e)
        return False, None, None, "network_error"


# -----------------------------------------------------------------------------
# Provider: Cloudflare Workers AI
# -----------------------------------------------------------------------------
# Endpoint: https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}
# Auth: Bearer token. Free tier: 10k neurons/dia (reset 00:00 UTC).
# FLUX schnell custa ~100 neurons por imagem 1024x1024 → ~100 imagens/dia grátis.
# Limitação: Cloudflare aplica filtros de conteúdo. Só usar pra SFW.

_CLOUDFLARE_FLUX_SCHNELL = "@cf/black-forest-labs/flux-1-schnell"


async def generate_with_cloudflare(
    session: aiohttp.ClientSession,
    *,
    account_id: str,
    api_token: str,
    prompt: str,
    profile: ImageProfile,
    timeout_seconds: float,
) -> tuple[bool, Optional[bytes], Optional[str], str]:
    """Gera imagem via Cloudflare Workers AI (FLUX schnell).

    Só funciona pra SFW. Se `profile.nsfw=True`, retorna missing_key imediato
    (sinaliza ao router que deve pular pro próximo).
    """
    if profile.nsfw:
        return False, None, None, "missing_key"
    if not account_id or not api_token:
        return False, None, None, "missing_key"

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/ai/run/{_CLOUDFLARE_FLUX_SCHNELL}"
    )
    payload = {
        "prompt": prompt[:2000],
        "steps": 8,  # FLUX schnell é rápido, 4-8 steps basta
    }
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with session.post(
            url, json=payload, headers=headers, timeout=timeout,
        ) as resp:
            if resp.status >= 400:
                body = (await resp.text())[:250].lower()
                if resp.status == 408:
                    reason = "timeout"
                elif resp.status == 429:
                    reason = "no_worker"
                elif resp.status in (400, 401, 403, 422):
                    reason = "provider_blocked"
                elif resp.status in (500, 502, 503, 504):
                    reason = "no_worker"
                else:
                    reason = "network_error"
                log.warning(
                    "chatbot: imagegen cloudflare falhou | status=%s reason=%s body=%s",
                    resp.status, reason, body,
                )
                return False, None, None, reason

            # CF retorna JSON com {"result": {"image": "<base64>"}, "success": true}
            # ou image/png binário dependendo do modelo. FLUX schnell → JSON base64.
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "application/json" in content_type:
                body = await resp.json()
                if not body.get("success"):
                    errors = body.get("errors") or []
                    log.warning(
                        "chatbot: imagegen cloudflare JSON error: %s",
                        str(errors)[:250],
                    )
                    return False, None, None, "provider_blocked"
                import base64 as _b64
                b64 = (body.get("result") or {}).get("image")
                if not b64:
                    return False, None, None, "no_image_returned"
                try:
                    data = _b64.b64decode(b64)
                except Exception:
                    return False, None, None, "no_image_returned"
                return True, data, "image/png", ""

            # Alguns modelos CF retornam binário direto.
            if "image/" in content_type:
                data = await resp.read()
                if not data:
                    return False, None, None, "no_image_returned"
                return True, data, content_type.split(";")[0], ""

            return False, None, None, "no_image_returned"
    except aiohttp.ServerTimeoutError:
        return False, None, None, "timeout"
    except aiohttp.ClientError as e:
        log.warning("chatbot: imagegen cloudflare erro de rede: %s", e)
        return False, None, None, "network_error"
    except Exception as e:
        log.warning("chatbot: imagegen cloudflare erro inesperado: %s", e)
        return False, None, None, "network_error"


# -----------------------------------------------------------------------------
# Ordem de preferência de providers por perfil
# -----------------------------------------------------------------------------
# Ranking pensado com "qualidade primeiro", baseado em avaliações públicas
# (abril 2026). Router tenta em ordem, pula quem não tá configurado, e faz
# fallback pro próximo se o primeiro der timeout/no_worker/network_error.
# "provider_blocked" (política, não rate limit) também vira fallback — pode
# ser que um provider filtrou um termo que outro aceita.

ProviderName = Literal[
    "pollinations",
    "cloudflare",
    "gemini",
    "aihorde",
    "huggingface",
    "adult_custom",
]


def provider_order_for_profile(profile: ImageProfile) -> list[ProviderName]:
    """Retorna ordem preferencial de providers pra tentar. Router pula os
    que não estão configurados (sem chave/conta)."""
    if profile.nsfw:
        # NÃO incluir Pollinations em NSFW: o provider tem safety checker
        # (LlamaGuard) que filtra conteúdo explícito mesmo com chave e
        # `safe=false`, então acaba entregando imagem SFW sorrateiramente.
        # Melhor falhar explicitamente do que gerar o conteúdo errado.
        return ["aihorde", "huggingface", "adult_custom"]

    # SFW — Pollinations e Cloudflare têm FLUX, qualidade topo.
    if profile.style == "realistic":
        return ["pollinations", "cloudflare", "gemini", "aihorde", "huggingface"]
    if profile.style == "anime":
        # Pollinations flux-anime > Horde Animagine pro SFW anime.
        return ["pollinations", "aihorde", "huggingface", "cloudflare", "gemini"]
    # SFW genérico: FLUX em Pollinations é o melhor all-around.
    return ["pollinations", "cloudflare", "gemini", "aihorde", "huggingface"]
