"""Clientes HTTP mínimos para Groq e Gemini.

Decisão explícita de NÃO usar SDKs (groq, google-genai). Motivos:
- groq SDK instala httpx + pydantic v2 (~30MB, redundantes com aiohttp que já temos)
- google-genai instala protobuf + grpc (~50MB, pesado demais pra VPS de 1GB)
- chamadas HTTP diretas com aiohttp são ~40 linhas e dão controle total
  sobre timeout, retry, cancelamento e tratamento de 429

Este módulo expõe uma interface uniforme:

    client = ProviderRouter(aiohttp_session, groq_key, gemini_key)
    reply = await client.chat(messages=[...], system="...", temperature=0.8)

`messages` é formato padrão OpenAI ({"role": "user"|"assistant", "content": "..."}).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from . import constants as C

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """Erro ao chamar o provider. Pode incluir causa HTTP."""

    def __init__(self, message: str, *, status: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RateLimitError(ProviderError):
    """HTTP 429. retry_after indica quanto esperar (ou None)."""


class AllProvidersExhausted(ProviderError):
    """Todos os providers falharam — usuário vai receber mensagem genérica."""


@dataclass
class ChatMessage:
    """Mensagem do histórico enviada ao modelo.

    Se `image_urls` estiver preenchido, a mensagem usa o formato multimodal
    da OpenAI: `content` vira uma lista com um bloco `text` + N blocos
    `image_url`. O provider decide se suporta — provider sem visão ignora
    as imagens (recomendação: usar ChatMessage.content simples pra texto
    puro e só adicionar image_urls quando realmente tem imagem, pra não
    fazer request mais cara à toa).
    """
    role: str  # "user" ou "assistant"
    content: str
    image_urls: list[str] = field(default_factory=list)

    def to_openai_payload(self) -> dict:
        """Serializa pro formato esperado pela API OpenAI-compatible.

        Sem imagens: `{role, content: str}`.
        Com imagens: `{role, content: [{type:"text",...}, {type:"image_url",...}, ...]}`.
        """
        if not self.image_urls:
            return {"role": self.role, "content": self.content}
        # Formato multimodal
        blocks: list[dict] = [{"type": "text", "text": self.content}]
        for url in self.image_urls[:5]:  # Groq limita a 5 imagens
            blocks.append({
                "type": "image_url",
                "image_url": {"url": url},
            })
        return {"role": self.role, "content": blocks}


@dataclass
class _ProviderState:
    """Estado de cooldown interno após 429. Evita marretar um provider que
    acabou de retornar rate-limit. Não persiste — reseta a cada reinício."""

    next_allowed_monotonic: float = 0.0
    consecutive_failures: int = 0

    def is_available(self) -> bool:
        return time.monotonic() >= self.next_allowed_monotonic

    def mark_success(self) -> None:
        self.consecutive_failures = 0
        self.next_allowed_monotonic = 0.0

    def mark_failure(self, cooldown_seconds: float) -> None:
        self.consecutive_failures += 1
        # exponential backoff com teto em 5 minutos
        backoff = min(300.0, cooldown_seconds * (2 ** min(self.consecutive_failures - 1, 4)))
        self.next_allowed_monotonic = time.monotonic() + backoff


class _GroqClient:
    """Chamadas ao endpoint OpenAI-compatível do Groq."""

    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key

    async def chat(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        temperature: float,
        model: str,
    ) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}]
            + [m.to_openai_payload() for m in messages],
            "temperature": max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, temperature)),
            "max_tokens": C.MAX_RESPONSE_TOKENS,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=C.PROVIDER_TIMEOUT_SECONDS)
        try:
            async with self._session.post(
                self.BASE_URL,
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status == 429:
                    retry_after_hdr = resp.headers.get("retry-after")
                    try:
                        retry_after = float(retry_after_hdr) if retry_after_hdr else None
                    except ValueError:
                        retry_after = None
                    raise RateLimitError(
                        f"Groq rate-limit ({model})",
                        status=429,
                        retry_after=retry_after,
                    )
                if resp.status >= 400:
                    body = await resp.text()
                    raise ProviderError(
                        f"Groq HTTP {resp.status}: {body[:300]}",
                        status=resp.status,
                    )
                data = await resp.json()
        except asyncio.TimeoutError:
            raise ProviderError(f"Groq timeout após {C.PROVIDER_TIMEOUT_SECONDS}s")
        except aiohttp.ClientError as e:
            raise ProviderError(f"Groq erro de rede: {e}")

        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"Groq resposta malformada: {e}")


class _GeminiClient:
    """Chamadas ao endpoint REST do Gemini (não o SDK)."""

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key

    async def chat(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        temperature: float,
        model: str,
    ) -> str:
        # Gemini tem formato próprio: "contents" ao invés de "messages",
        # roles são "user"/"model" (não "assistant"), system vai em campo separado.
        # Imagens: Gemini aceita inlineData base64, mas implementar o download
        # local é trabalhoso. Como Groq já cobre o caminho feliz de visão,
        # o fallback Gemini ignora imagens e responde baseado só no texto.
        # O prompt do bot menciona que há imagem, então não fica totalmente cego.
        any_has_images = any(getattr(m, "image_urls", None) for m in messages)
        if any_has_images:
            log.info("chatbot: Gemini fallback com imagem — respondendo só texto")

        contents = []
        for m in messages:
            role = "user" if m.role == "user" else "model"
            # Nota pro modelo se a mensagem tinha imagem que não pudemos anexar
            content_text = m.content
            if getattr(m, "image_urls", None):
                content_text = (
                    f"{content_text}\n"
                    f"(nota: o usuário anexou {len(m.image_urls)} imagem(ns), "
                    f"mas neste fallback a visão não está disponível — "
                    f"responda reconhecendo que vê a imagem mas sem detalhes)"
                )
            contents.append({"role": role, "parts": [{"text": content_text}]})

        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "temperature": max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, temperature)),
                "maxOutputTokens": C.MAX_RESPONSE_TOKENS,
            },
        }
        url = self.BASE_URL.format(model=model) + f"?key={self._api_key}"
        headers = {"Content-Type": "application/json"}

        timeout = aiohttp.ClientTimeout(total=C.PROVIDER_TIMEOUT_SECONDS)
        try:
            async with self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                if resp.status == 429:
                    retry_after_hdr = resp.headers.get("retry-after")
                    try:
                        retry_after = float(retry_after_hdr) if retry_after_hdr else None
                    except ValueError:
                        retry_after = None
                    raise RateLimitError(
                        f"Gemini rate-limit ({model})",
                        status=429,
                        retry_after=retry_after,
                    )
                if resp.status >= 400:
                    body = await resp.text()
                    raise ProviderError(
                        f"Gemini HTTP {resp.status}: {body[:300]}",
                        status=resp.status,
                    )
                data = await resp.json()
        except asyncio.TimeoutError:
            raise ProviderError(f"Gemini timeout após {C.PROVIDER_TIMEOUT_SECONDS}s")
        except aiohttp.ClientError as e:
            raise ProviderError(f"Gemini erro de rede: {e}")

        try:
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts).strip()
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError(f"Gemini resposta malformada: {e}")


class ProviderRouter:
    """Fachada: tenta Groq primeiro, cai em Gemini se falhar.

    Uso:
        async with aiohttp.ClientSession() as session:
            router = ProviderRouter(session, groq_key="...", gemini_key="...")
            reply = await router.chat(
                system="Você é um pirata alegre.",
                messages=[ChatMessage("user", "oi")],
                temperature=0.9,
            )
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        groq_key: Optional[str] = None,
        gemini_key: Optional[str] = None,
    ):
        self._session = session
        self._groq = _GroqClient(session, groq_key) if groq_key else None
        self._gemini = _GeminiClient(session, gemini_key) if gemini_key else None
        self._groq_state = _ProviderState()
        self._gemini_state = _ProviderState()

        if not self._groq and not self._gemini:
            log.warning(
                "ProviderRouter: nenhuma API key configurada. Chamadas vão falhar."
            )

    async def chat(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        temperature: float = C.DEFAULT_TEMPERATURE,
    ) -> str:
        # Detecta se há imagens em alguma mensagem. Se sim, precisamos usar
        # um modelo de visão — os modelos padrão (Llama 3.3/3.1) não aceitam
        # blocos `image_url`. Prepend do vision model na lista de tentativas.
        has_images = any(getattr(m, "image_urls", None) for m in messages)

        attempts: list[tuple[str, _ProviderState, object, tuple[str, ...]]] = []
        if self._groq is not None and self._groq_state.is_available():
            if has_images:
                # Com imagem: só o vision model no Groq. Se falhar, cai pro
                # Gemini (que também suporta imagem nativamente).
                groq_models = (C.GROQ_VISION_MODEL,)
            else:
                groq_models = C.GROQ_MODELS
            attempts.append(("groq", self._groq_state, self._groq, groq_models))
        if self._gemini is not None and self._gemini_state.is_available():
            # Gemini 2.0 Flash aceita imagem nativamente — mesmos modelos.
            attempts.append(("gemini", self._gemini_state, self._gemini, C.GEMINI_MODELS))

        if not attempts:
            raise AllProvidersExhausted(
                "Todos os providers estão em cooldown ou não configurados"
            )

        last_error: Optional[Exception] = None
        for provider_name, state, client, models in attempts:
            for model in models:
                try:
                    reply = await client.chat(
                        system=system,
                        messages=messages,
                        temperature=temperature,
                        model=model,
                    )
                    state.mark_success()
                    log.debug("chatbot: %s/%s respondeu %d chars", provider_name, model, len(reply))
                    return reply
                except RateLimitError as e:
                    cooldown = float(e.retry_after) if e.retry_after else 30.0
                    state.mark_failure(cooldown)
                    last_error = e
                    log.warning("chatbot: %s/%s rate-limited (retry=%s), próximo modelo", provider_name, model, cooldown)
                    break  # não insiste no mesmo provider, próximo
                except ProviderError as e:
                    last_error = e
                    log.warning("chatbot: %s/%s falhou: %s", provider_name, model, e)
                    # erros não-429 em modelo específico: tenta o próximo modelo do mesmo provider
                    continue

        # chegou aqui? todos os modelos de todos providers falharam.
        raise AllProvidersExhausted(f"Todos providers falharam. Último erro: {last_error}")
