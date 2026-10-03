"""Router HTTP leve com fallback real de texto e visão.

Estados são mantidos por provider/modelo, então um modelo removido não derruba
os demais. Há deadline total, respostas vazias são rejeitadas e o fallback
Gemini recebe os bytes das imagens — nunca afirma que viu algo que não recebeu.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import aiohttp

from . import constants as C
from .action_protocol import (
    ChatReply, InvalidActionProposal, MAX_PROPOSALS, enabled_actions, parse_proposals,
    private_reply_text, proposal_tool,
)
from .media import ImagePreparationError, MediaAttachment, PreparedImage, prepare_image_attachments

log = logging.getLogger(__name__)

# Apenas valores documentados entram no diagnóstico. Esse campo vem da API e
# não deve servir como caminho alternativo para registrar conteúdo da resposta.
_DIAGNOSTIC_FINISH_REASONS = frozenset({
    "stop", "length", "content_filter", "tool_calls", "function_call",
    "STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "LANGUAGE", "OTHER",
    "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "MALFORMED_FUNCTION_CALL",
    "UNEXPECTED_TOOL_CALL", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_OTHER", "NO_IMAGE", "FINISH_REASON_UNSPECIFIED",
})


def _diagnostic_finish_reason(reason: Optional[str]) -> str:
    if reason is None:
        return "none"
    return reason if isinstance(reason, str) and reason in _DIAGNOSTIC_FINISH_REASONS else "other"


class ProviderError(Exception):
    def __init__(
        self, message: str, *, status: Optional[int] = None,
        retry_after: Optional[float] = None, kind: Optional[str] = None,
        stage: str = "api", finish_reason: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.stage = stage
        self.finish_reason = finish_reason
        self.kind = kind or (
            "auth" if status in (401, 403) else
            "rate_limit" if status == 429 else
            "model" if status in (400, 404, 422) else "network"
        )


class RateLimitError(ProviderError):
    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("kind", "rate_limit")
        kwargs.setdefault("status", 429)
        super().__init__(message, **kwargs)


class AllProvidersExhausted(ProviderError):
    pass


@dataclass
class ChatMessage:
    role: str
    content: str
    image_urls: list[str] = field(default_factory=list)
    images: list[PreparedImage] = field(default_factory=list)

    def to_openai_payload(self) -> dict:
        if not self.images and not self.image_urls:
            return {"role": self.role, "content": self.content}
        blocks: list[dict] = [{"type": "text", "text": self.content}]
        if self.images:
            urls = [
                f"data:{image.mime_type};base64,{base64.b64encode(image.data).decode('ascii')}"
                for image in self.images[:C.MAX_IMAGES_PER_MESSAGE]
            ]
        else:
            urls = self.image_urls[:C.MAX_IMAGES_PER_MESSAGE]
        for url in urls:
            blocks.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": self.role, "content": blocks}


@dataclass
class _ProviderState:
    next_allowed_monotonic: float = 0.0
    consecutive_failures: int = 0
    last_status: int = 0

    def is_available(self) -> bool:
        return time.monotonic() >= self.next_allowed_monotonic

    def mark_success(self) -> None:
        self.consecutive_failures = 0
        self.next_allowed_monotonic = 0.0
        self.last_status = 0

    def mark_failure(self, cooldown_seconds: float, *, status: int = 0) -> None:
        self.consecutive_failures += 1
        factor = 2 ** min(self.consecutive_failures - 1, 4)
        self.next_allowed_monotonic = time.monotonic() + min(900.0, cooldown_seconds * factor)
        self.last_status = int(status)


def _retry_after(resp: aiohttp.ClientResponse) -> Optional[float]:
    raw = resp.headers.get("retry-after")
    try:
        return max(1.0, min(900.0, float(raw))) if raw else None
    except (TypeError, ValueError):
        return None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError("deadline total dos providers esgotado", kind="deadline", stage="routing")
    return min(C.PROVIDER_TIMEOUT_SECONDS, remaining)


async def _read_limited_bytes(
    response: aiohttp.ClientResponse, *, limit: int,
) -> bytes:
    raw_length = response.headers.get("Content-Length")
    try:
        declared = int(raw_length) if raw_length else 0
    except (TypeError, ValueError):
        declared = 0
    if declared > limit:
        raise ProviderError("resposta do provider excede o limite", kind="invalid_response", stage="output")
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > limit:
            raise ProviderError("resposta do provider excede o limite", kind="invalid_response", stage="output")
    return bytes(body)


async def _read_json_limited(response: aiohttp.ClientResponse):
    body = await _read_limited_bytes(
        response, limit=C.MAX_PROVIDER_RESPONSE_BYTES,
    )
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("provider retornou JSON inválido", kind="invalid_response", stage="output") from exc


async def _http_error(response: aiohttp.ClientResponse) -> ProviderError:
    """Extrai apenas a categoria; o corpo pode conter prompt ou dados da conta."""
    body = bytearray()
    async for chunk in response.content.iter_chunked(1024):
        body.extend(chunk)
        if len(body) >= 4096:
            break
    excerpt = bytes(body[:4096]).decode("utf-8", errors="replace").lower()
    status = response.status
    if status == 401 or (status == 403 and any(marker in excerpt for marker in (
        "invalid_api_key", "api_key_invalid", "invalid api key", "api key not valid",
        "api key expired", "authentication_error",
    ))):
        kind = "auth"
    elif status >= 500:
        kind = "network"
    elif any(marker in excerpt for marker in ("content_filter", "safety blocked", "prohibited_content")):
        kind = "blocked"
    elif status in (400, 422) and any(marker in excerpt for marker in (
        "does not support tool", "tools are not supported", "tool calling is not supported",
        "function calling is not supported", "tool use is not supported",
        "does not support function calling", "does not support function declarations",
    )):
        kind = "tools_unsupported"
    elif status == 404 or any(marker in excerpt for marker in (
        "model_not_found", "model_decommissioned", "model has been decommissioned",
        "not a valid model", "model is not supported", "unknown model",
        "does not support image", "does not support vision",
        "model_permission_denied", "model_access_denied", "model_access_restricted",
        "does not have access to model", "does not have access to the model",
        "not allowed to access model",
    )):
        kind = "model"
    elif status == 403:
        kind = "auth"
    else:
        kind = "request"
    return ProviderError("provider rejeitou a requisição", status=status, kind=kind)


def _output_tokens(messages: list[ChatMessage]) -> int:
    if any(message.images or message.image_urls for message in messages):
        return getattr(C, "MAX_VISION_RESPONSE_TOKENS", C.MAX_RESPONSE_TOKENS)
    return C.MAX_RESPONSE_TOKENS


def _action_reply(
    text: str, calls: list[tuple[object, object]], actions: tuple[str, ...],
    *, provider: str, model: str, finish_reason: Optional[str],
) -> ChatReply:
    try:
        proposals = parse_proposals(calls, actions)
    except InvalidActionProposal as exc:
        # Um lote inválido pode misturar pedido privado com outra chamada
        # malformada. Descartar também o texto público, que pode antecipar a fala.
        raise ProviderError(
            "provider retornou proposta inválida", kind="invalid_response",
            stage="output", finish_reason=finish_reason,
        ) from exc
    if not text and not proposals:
        raise ProviderError(
            "provider retornou resposta vazia", kind="empty", stage="output",
            finish_reason=finish_reason,
        )
    return ChatReply(private_reply_text(text, proposals), proposals, provider, model)


class _GroqClient:
    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key

    async def chat(
        self, *, system: str, messages: list[ChatMessage], temperature: float,
        model: str, timeout_seconds: float, actions: tuple[str, ...] = (),
        target_refs: tuple[str, ...] = (),
    ) -> str | ChatReply:
        actions = enabled_actions(actions)
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}]
            + [message.to_openai_payload() for message in messages],
            "temperature": max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, temperature)),
            "max_completion_tokens": _output_tokens(messages),
            "stream": False,
        }
        if actions:
            payload["tools"] = [{"type": "function", "function": proposal_tool(actions, target_refs)}]
            payload["tool_choice"] = "auto"
        # Evita gastar tokens de raciocínio oculto em conversa casual.
        if model.startswith("openai/gpt-oss"):
            payload.update({"reasoning_effort": "low", "include_reasoning": False})
        elif model == "qwen/qwen3.8-27b":
            # Valores documentados pelo Groq para este modelo específico.
            payload.update({"reasoning_effort": "none", "include_reasoning": False})
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=max(0.1, timeout_seconds))
        try:
            async with self._session.post(
                self.BASE_URL, json=payload, headers=headers, timeout=timeout,
            ) as resp:
                if resp.status == 429:
                    raise RateLimitError(
                        f"Groq rate-limit ({model})", status=429,
                        retry_after=_retry_after(resp),
                    )
                if resp.status >= 400:
                    raise await _http_error(resp)
                data = await _read_json_limited(resp)
        except asyncio.TimeoutError as exc:
            raise ProviderError("Groq timeout", kind="timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("Groq erro de rede", kind="network") from exc
        try:
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason")
            message = choice["message"]
            if finish_reason == "content_filter":
                raise ProviderError(
                    "resposta bloqueada pelo provider", kind="blocked", stage="output",
                    finish_reason=finish_reason,
                )
            content = message.get("content")
            refusal_present = bool(message.get("refusal"))
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
            # Alguns endpoints colocam a recusa textual num campo separado.
            # Ela continua sendo uma resposta válida, sem troca para outro modelo.
            if not (isinstance(content, str) and content.strip()):
                refusal = message.get("refusal")
                if isinstance(refusal, str) and refusal.strip():
                    content = refusal
                elif refusal:
                    raise ProviderError("resposta bloqueada pelo provider", kind="blocked", stage="output")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError("Groq resposta malformada", kind="invalid_response", stage="output") from exc
        reply = content.strip() if isinstance(content, str) else ""
        if actions:
            calls = []
            raw_calls = message.get("tool_calls") or []
            # Uma recusa explícita é terminal e não carrega ações anexas.
            if not refusal_present:
                if not isinstance(raw_calls, list):
                    raw_calls = [None]
                for call in raw_calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict) or call.get("type", "function") != "function":
                        calls.append((None, None))
                    else:
                        calls.append((function.get("name"), function.get("arguments")))
                    if len(calls) > MAX_PROPOSALS:
                        break
            return _action_reply(
                reply, calls, actions, provider="groq", model=model, finish_reason=finish_reason,
            )
        if not reply:
            raise ProviderError(
                "Groq retornou resposta vazia", kind="empty", stage="output",
                finish_reason=finish_reason,
            )
        return reply


class _GeminiClient:
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    _ALLOWED_IMAGE_HOST_SUFFIXES = (
        ".discordapp.com", ".discordapp.net", ".discord.com",
    )

    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._api_key = api_key

    async def _download_inline_image(self, url: str, deadline: float) -> dict:
        """Compatibilidade para chamadores antigos; falha do CDN nunca é credencial."""
        filename = urlsplit(url).path.rsplit("/", 1)[-1] or "image"
        try:
            images = await prepare_image_attachments(
                self._session, [MediaAttachment(url, filename, "", 0, "image")],
                timeout_seconds=max(.01, deadline - time.monotonic()),
            )
        except ImagePreparationError as exc:
            raise ProviderError(str(exc), kind=exc.kind, stage=exc.stage, status=exc.status) from exc
        return self._image_part(images[0])

    @staticmethod
    def _image_part(image: PreparedImage) -> dict:
        return {"inlineData": {
            "mimeType": image.mime_type,
            "data": base64.b64encode(image.data).decode("ascii"),
        }}

    async def chat(
        self, *, system: str, messages: list[ChatMessage], temperature: float,
        model: str, timeout_seconds: float, actions: tuple[str, ...] = (),
        target_refs: tuple[str, ...] = (),
    ) -> str | ChatReply:
        actions = enabled_actions(actions)
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        contents: list[dict] = []
        for message in messages:
            parts: list[dict] = [{"text": message.content}]
            if message.images:
                parts.extend(self._image_part(image) for image in message.images[:C.MAX_IMAGES_PER_MESSAGE])
            else:
                for url in message.image_urls[:C.MAX_IMAGES_PER_MESSAGE]:
                    parts.append(await self._download_inline_image(url, deadline))
            role = "user" if message.role == "user" else "model"
            # Contexto citado e mensagem atual podem ser blocos user seguidos.
            # Preservar cada parte na ordem original, sem atravessar uma resposta.
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {
                "temperature": max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, temperature)),
                "maxOutputTokens": _output_tokens(messages),
            },
        }
        if actions:
            tool = proposal_tool(actions, target_refs)
            # Gemini usa o subconjunto OpenAPI de Schema. A validação estrita
            # continua no host, incluindo a rejeição de propriedades extras.
            tool["parameters"].pop("additionalProperties", None)
            payload["tools"] = [{"functionDeclarations": [tool]}]
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        # Flash/Lite 2.5 aceitam desativar pensamento para conversa curta.
        # Sem isso o orçamento de saída pode acabar antes do texto visível.
        if model.startswith(("gemini-2.5-flash", "gemini-2.5-flash-lite")):
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._api_key}
        timeout = aiohttp.ClientTimeout(total=_remaining(deadline))
        try:
            async with self._session.post(
                self.BASE_URL.format(model=model), json=payload,
                headers=headers, timeout=timeout,
            ) as resp:
                if resp.status == 429:
                    raise RateLimitError(
                        f"Gemini rate-limit ({model})", status=429,
                        retry_after=_retry_after(resp),
                    )
                if resp.status >= 400:
                    raise await _http_error(resp)
                data = await _read_json_limited(resp)
        except asyncio.TimeoutError as exc:
            raise ProviderError("Gemini timeout", kind="timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("Gemini erro de rede", kind="network") from exc
        if not isinstance(data, dict):
            raise ProviderError("Gemini resposta malformada", kind="invalid_response", stage="output")
        feedback = data.get("promptFeedback") or {}
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ProviderError("pedido bloqueado pelo provider", kind="blocked", stage="output")
        try:
            candidate = data["candidates"][0]
            finish_reason = candidate.get("finishReason")
            if finish_reason in {
                "SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY",
            }:
                raise ProviderError(
                    "resposta bloqueada pelo provider", kind="blocked", stage="output",
                    finish_reason=finish_reason,
                )
            parts = candidate.get("content", {}).get("parts", [])
            reply = "".join(
                part.get("text", "") for part in parts
                if isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str)
            ).strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderError("Gemini resposta malformada", kind="invalid_response", stage="output") from exc
        if actions:
            calls = []
            for part in parts:
                if not isinstance(part, dict) or part.get("thought") or "functionCall" not in part:
                    continue
                function = part["functionCall"]
                if not isinstance(function, dict):
                    calls.append((None, None))
                else:
                    calls.append((function.get("name"), function.get("args")))
                if len(calls) > MAX_PROPOSALS:
                    break
            return _action_reply(
                reply, calls, actions, provider="gemini", model=model, finish_reason=finish_reason,
            )
        if not reply:
            raise ProviderError(
                "Gemini retornou resposta vazia", kind="empty", stage="output",
                finish_reason=finish_reason,
            )
        return reply


class ProviderRouter:
    def __init__(
        self, session: aiohttp.ClientSession, *, groq_key: Optional[str] = None,
        gemini_key: Optional[str] = None,
    ) -> None:
        self._session = session
        self._groq = _GroqClient(session, groq_key) if groq_key else None
        self._gemini = _GeminiClient(session, gemini_key) if gemini_key else None
        self._states: dict[tuple[str, str], _ProviderState] = {}
        self._unsupported_tool_models: set[tuple[str, str]] = set()
        if not self._groq and not self._gemini:
            log.warning("ProviderRouter: nenhuma API key configurada")

    def _state(self, provider: str, model: str) -> _ProviderState:
        return self._states.setdefault((provider, model), _ProviderState())

    def _mark_provider_failure(
        self,
        provider: str,
        models: tuple[str, ...],
        cooldown_seconds: float,
        *,
        status: int,
    ) -> None:
        """Abre o circuito de todos os modelos quando a falha é da conta/rede."""
        for candidate in models:
            self._state(provider, candidate).mark_failure(
                cooldown_seconds, status=status,
            )

    def snapshot(self) -> dict[str, dict[str, float | int | bool]]:
        now = time.monotonic()
        return {
            f"{provider}/{model}": {
                "available": state.is_available(),
                "cooldown_seconds": max(0.0, state.next_allowed_monotonic - now),
                "failures": state.consecutive_failures,
                "last_status": state.last_status,
            }
            for (provider, model), state in self._states.items()
        }

    async def chat(
        self, *, system: str, messages: list[ChatMessage],
        temperature: float = C.DEFAULT_TEMPERATURE, actions: tuple[str, ...] = (),
        target_refs: tuple[str, ...] = (),
    ) -> str | ChatReply:
        actions = enabled_actions(actions)
        started = time.monotonic()
        has_images = any(message.images or message.image_urls for message in messages)
        mode = "vision" if has_images else "text"
        attempts: list[tuple[str, object, tuple[str, ...]]] = []
        if self._groq:
            attempts.append((
                "groq", self._groq,
                C.GROQ_VISION_MODELS if has_images else C.GROQ_MODELS,
            ))
        if self._gemini:
            attempts.append((
                "gemini", self._gemini,
                getattr(C, "GEMINI_VISION_MODELS", C.GEMINI_MODELS) if has_images else C.GEMINI_MODELS,
            ))
        if not has_images:
            # Uma prioridade parcial mantém os outros providers como fallback.
            # Ignorar nomes desconhecidos/repetidos não afeta a cadeia de visão.
            by_provider = {attempt[0]: attempt for attempt in attempts}
            priority = getattr(C, "TEXT_PROVIDER_ORDER", ("groq", "gemini"))
            ordered_names = dict.fromkeys((*priority, *by_provider))
            attempts = [by_provider[name] for name in ordered_names if name in by_provider]
        if not attempts:
            raise AllProvidersExhausted("nenhum provider configurado", kind="unconfigured", stage="routing")

        deadline = started + C.PROVIDER_ROUTER_TIMEOUT_SECONDS
        # Chamadores que ainda passam URLs também recebem o mesmo preparo único.
        # Nunca remover anexos para tentar um fallback apenas de texto.
        normalized: list[ChatMessage] = []
        try:
            for message in messages:
                if message.images or not message.image_urls:
                    normalized.append(message)
                    continue
                attachments = [
                    MediaAttachment(url, urlsplit(url).path.rsplit("/", 1)[-1], "", 0, "image")
                    for url in message.image_urls[:C.MAX_IMAGES_PER_MESSAGE]
                ]
                images = await prepare_image_attachments(
                    self._session, attachments, timeout_seconds=_remaining(deadline),
                )
                normalized.append(ChatMessage(message.role, message.content, images=images))
        except ImagePreparationError as exc:
            raise AllProvidersExhausted(
                str(exc), kind=exc.kind, stage=exc.stage, status=exc.status,
            ) from exc
        messages = normalized
        last_error: Optional[ProviderError] = None
        attempted = 0
        for provider_name, client, models in attempts:
            for model in models:
                state = self._state(provider_name, model)
                if not state.is_available():
                    continue
                try:
                    timeout = _remaining(deadline)
                    attempted += 1
                    kwargs = dict(
                        system=system, messages=messages, temperature=temperature,
                        model=model, timeout_seconds=timeout,
                    )
                    # Não adicionar um argumento sequer aos chamadores/mocks
                    # antigos quando nenhuma ferramenta foi disponibilizada.
                    if actions and (provider_name, model) not in self._unsupported_tool_models:
                        kwargs["actions"] = actions
                        if target_refs:
                            kwargs["target_refs"] = target_refs
                    try:
                        reply = await client.chat(**kwargs)
                    except ProviderError as exc:
                        if exc.kind != "tools_unsupported" or "actions" not in kwargs:
                            raise
                        # A API recusou a capacidade antes de gerar resposta.
                        # Este modelo passa a conversar apenas em texto, sem
                        # transformar uma frase comum numa ação executável.
                        self._unsupported_tool_models.add((provider_name, model))
                        kwargs.pop("actions")
                        kwargs.pop("target_refs", None)
                        kwargs["timeout_seconds"] = _remaining(deadline)
                        attempted += 1
                        reply = await client.chat(**kwargs)
                    if actions and isinstance(reply, str):
                        reply = ChatReply(reply, provider=provider_name, model=model)
                    state.mark_success()
                    log.info(
                        "chatbot: result=success provider=%s model=%s mode=%s elapsed_ms=%d message_count=%d",
                        provider_name, model, mode,
                        max(0, int((time.monotonic() - started) * 1000)), len(messages),
                    )
                    return reply
                except RateLimitError as exc:
                    last_error = exc
                    self._mark_provider_failure(
                        provider_name, models, float(exc.retry_after or 30.0), status=429,
                    )
                    log.warning(
                        "chatbot: provider=%s model=%s mode=%s stage=%s kind=rate_limit status=429 finish_reason=%s",
                        provider_name, model, mode, exc.stage,
                        _diagnostic_finish_reason(exc.finish_reason),
                    )
                    break
                except ProviderError as exc:
                    last_error = exc
                    status = int(exc.status or 0)
                    log.warning(
                        "chatbot: provider=%s model=%s mode=%s stage=%s kind=%s status=%s finish_reason=%s",
                        provider_name, model, mode, exc.stage, exc.kind, status,
                        _diagnostic_finish_reason(exc.finish_reason),
                    )
                    # Um anexo inválido ou bloqueio não indica indisponibilidade.
                    if exc.stage == "attachment" or exc.kind == "blocked":
                        raise AllProvidersExhausted(
                            "pedido não pôde ser processado", kind=exc.kind,
                            stage=exc.stage, status=exc.status, finish_reason=exc.finish_reason,
                        ) from exc
                    if exc.kind == "auth":
                        all_models = tuple(dict.fromkeys(
                            (*C.GROQ_MODELS, *C.GROQ_VISION_MODELS) if provider_name == "groq" else
                            (*C.GEMINI_MODELS, *getattr(C, "GEMINI_VISION_MODELS", C.GEMINI_MODELS))
                        ))
                        self._mark_provider_failure(provider_name, all_models, 900.0, status=status)
                        break
                    if exc.kind == "model":
                        state.mark_failure(300.0, status=status)
                    elif exc.kind in {"network", "timeout"}:
                        self._mark_provider_failure(provider_name, models, 20.0, status=status)
                        break
                    elif exc.kind in {"empty", "invalid_response"}:
                        # Resposta inválida pertence à tentativa/modelo, não à conta.
                        # Esgotar tokens é um limite desta saída, não indisponibilidade.
                        if exc.finish_reason not in {"length", "MAX_TOKENS"}:
                            state.mark_failure(20.0, status=status)
                    elif exc.kind == "deadline":
                        break
                    # Erro do payload não coloca um modelo válido em cooldown.
                    if time.monotonic() >= deadline:
                        break
            if time.monotonic() >= deadline:
                break
        if attempted == 0:
            if last_error and last_error.kind == "deadline":
                raise AllProvidersExhausted("prazo dos providers esgotado", kind="deadline", stage="routing")
            raise AllProvidersExhausted("todos os modelos estão em cooldown", kind="cooldown", stage="routing")
        raise AllProvidersExhausted(
            "todos os providers falharam",
            kind=last_error.kind if last_error else "network",
            stage=last_error.stage if last_error else "api",
            status=last_error.status if last_error else None,
            finish_reason=last_error.finish_reason if last_error else None,
        )
