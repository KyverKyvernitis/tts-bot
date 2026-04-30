"""Cliente HTTP da DevAI.

Cada provider é uma função self-contained. A ordem dos providers vem do
config.DEVAI_PROVIDER_ORDER e o cog passa um prompt já completo. Este módulo
não conhece o conteúdo do prompt — ele apenas:

1. faz o request OpenAI-compatible (ou específico do Gemini),
2. valida que voltou texto não-vazio,
3. opcionalmente re-tenta o **mesmo** provider com uma mensagem de "repair"
   quando o cog detecta que o JSON não parseou ou o Python não compilou.

Mudanças desta versão (2026-04):
- Removidos modelos defasados (gemini-2.0-flash foi aposentado em 03/03/2026).
- Adicionados providers OpenRouter e Cerebras (free tiers fortes em código).
- Cap de tokens por modelo (gpt-oss-120b aceita 65k, llama-3.3-70b só 32k).
- Método repair() para o cog re-pedir correção quando JSON/compile falha.
- System prompt com metodologia clara e anti-padrões.
- Estatísticas simples por provider (sucesso/falha/latência).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import aiohttp


# Limite de tokens de saída por modelo conhecido. Quando o nome bate, usa-se
# o teto do modelo em vez do DEVAI_MAX_OUTPUT_TOKENS global. Isso evita 400 em
# modelos como llama-3.3-70b-versatile (32K) ou qwen3-32b (40K).
_MODEL_OUTPUT_LIMITS: dict[str, int] = {
    "openai/gpt-oss-120b": 65000,
    "openai/gpt-oss-20b": 65000,
    "llama-3.3-70b-versatile": 32000,
    "llama-3.1-8b-instant": 32000,
    "qwen/qwen3-32b": 40000,
    "meta-llama/llama-4-scout-17b-16e-instruct": 8000,
}


SYSTEM_PROMPT_FIX = (
    "Você é DevAI: engenheira sênior de manutenção de um bot Discord em Python "
    "(discord.py). Seu trabalho é receber um traceback + arquivos relacionados + "
    "estrutura do projeto, e devolver UM patch mínimo que corrija a causa-raiz.\n\n"
    "METODOLOGIA OBRIGATÓRIA:\n"
    "1. Leia o traceback de baixo pra cima — a última linha é o erro real.\n"
    "2. Localize a função no código fornecido. NÃO altere arquivos que não foram "
    "fornecidos no contexto, mesmo que pareçam relacionados.\n"
    "3. Faça a menor mudança possível que conserte o problema. NÃO refatore.\n"
    "4. Preserve estilo, imports e formatação do projeto.\n"
    "5. Se o erro for transitório (rate limit, network), proponha retry/backoff em "
    "vez de mudar lógica de negócio.\n"
    "6. Se NÃO tiver certeza de qual arquivo mexer, devolva files=[] e explique em "
    "cause. Não chute.\n\n"
    "ANTI-PADRÕES PROIBIDOS:\n"
    "- Devolver arquivo truncado/incompleto.\n"
    "- Inventar funções/classes que não existem no projeto.\n"
    "- Mexer em .env, tokens, .db, credenciais.\n"
    "- Adicionar dependências novas sem necessidade absoluta.\n"
    "- Reescrever do zero.\n\n"
    "FORMATO DE RESPOSTA: SOMENTE JSON válido, sem markdown, sem texto antes ou "
    "depois. Schema fornecido no prompt do usuário."
)

SYSTEM_PROMPT_REVIEW = (
    "Você é DevAI: revisora de patches de um bot Discord Python. Um ZIP foi aceito "
    "pelo auto-updater e enviado pro GitHub. Sua tarefa é ler o que mudou (e o diff "
    "quando disponível) e produzir um comentário curto, direto e útil pro dono do "
    "bot: o que mudou, por que mudou, riscos, como validar.\n\n"
    "Não invente. Se não tiver certeza, diga 'não foi possível inferir'. NÃO "
    "proponha arquivos novos — este é fluxo de revisão, não de correção. "
    "Responda SOMENTE com JSON válido."
)


@dataclass
class AIResult:
    provider: str
    model: str
    text: str
    elapsed_ms: int


@dataclass
class ProviderStats:
    """Estatísticas leves por provider — útil pra `_devai status`."""
    success: int = 0
    failure: int = 0
    last_error: str = ""
    last_latency_ms: int = 0
    by_error: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class DevAIClient:
    def __init__(self, session: aiohttp.ClientSession, config_module):
        self.session = session
        self.config = config_module
        self.timeout_seconds = int(getattr(config_module, "DEVAI_PROVIDER_TIMEOUT_SECONDS", 60) or 60)
        self.max_tokens = int(getattr(config_module, "DEVAI_MAX_OUTPUT_TOKENS", 12000) or 12000)
        self.temperature = float(getattr(config_module, "DEVAI_TEMPERATURE", 0.15) or 0.15)
        self.stats: dict[str, ProviderStats] = defaultdict(ProviderStats)

    # ------------------------------------------------------------------ utils

    def _output_tokens_for(self, model: str) -> int:
        """Respeita o teto do modelo se conhecido, senão usa o global."""
        for needle, cap in _MODEL_OUTPUT_LIMITS.items():
            if needle in model:
                return min(self.max_tokens, cap)
        return self.max_tokens

    def provider_order(self) -> list[str]:
        raw = getattr(self.config, "DEVAI_PROVIDER_ORDER", []) or []
        if isinstance(raw, str):
            items = [p.strip().lower() for p in raw.split(",") if p.strip()]
        else:
            items = [str(p).strip().lower() for p in raw if str(p).strip()]
        return items or ["gemini", "groq", "openrouter", "cerebras", "cloudflare", "huggingface", "pollinations"]

    def stats_summary(self) -> dict[str, dict[str, Any]]:
        """Snapshot dos contadores — usado em `_devai status`."""
        out: dict[str, dict[str, Any]] = {}
        for name, st in self.stats.items():
            out[name] = {
                "success": st.success,
                "failure": st.failure,
                "last_error": st.last_error[:200],
                "last_latency_ms": st.last_latency_ms,
                "errors": dict(st.by_error),
            }
        return out

    # ---------------------------------------------------------------- entrada

    async def generate_patch_json(self, prompt: str, *, system: str | None = None) -> tuple[AIResult | None, list[str]]:
        """Roda os providers em ordem, devolve o primeiro com texto não-vazio."""
        sys_prompt = system or SYSTEM_PROMPT_FIX
        errors: list[str] = []
        for provider in self.provider_order():
            try:
                result = await self._dispatch(provider, prompt, sys_prompt)
                if result and result.text.strip():
                    self._record_success(provider, result.elapsed_ms)
                    return result, errors
                errors.append(f"{provider}: resposta vazia")
                self._record_failure(provider, "empty")
            except Exception as exc:
                msg = f"{provider}: {type(exc).__name__}: {exc}"
                errors.append(msg)
                self._record_failure(provider, type(exc).__name__)
        return None, errors

    async def repair_patch_json(
        self,
        *,
        original_prompt: str,
        bad_response: str,
        error_message: str,
        system: str | None = None,
    ) -> tuple[AIResult | None, list[str]]:
        """Pede pra IA consertar a própria saída quando JSON/compile falhou.

        Reusa a mesma ordem de providers, mas com um prompt curto que cita o
        erro e exige JSON válido. Em geral acerta na 1ª tentativa em modelos
        ≥30B.
        """
        sys_prompt = system or SYSTEM_PROMPT_FIX
        repair_prompt = (
            "Sua resposta anterior NÃO foi aceita. Motivo:\n"
            f"```\n{error_message[:1500]}\n```\n\n"
            "Sua resposta anterior (truncada se grande):\n"
            f"```\n{bad_response[:6000]}\n```\n\n"
            "Pedido original abaixo. Refaça SOMENTE o JSON válido pedido. "
            "Não comente, não envolva em markdown, não escreva nada antes ou "
            "depois das chaves. Cada arquivo em files[] precisa do conteúdo "
            "COMPLETO em content (sem '...').\n\n"
            "--- PEDIDO ORIGINAL ---\n"
            f"{original_prompt}"
        )
        return await self.generate_patch_json(repair_prompt, system=sys_prompt)

    # ------------------------------------------------------------ dispatching

    async def _dispatch(self, provider: str, prompt: str, system: str) -> AIResult:
        if provider == "gemini":
            return await self._call_gemini(prompt, system)
        if provider == "groq":
            return await self._call_openai_compatible(
                provider="groq",
                base_url=str(getattr(self.config, "DEVAI_GROQ_BASE_URL", "https://api.groq.com/openai/v1") or "").rstrip("/"),
                api_key=str(getattr(self.config, "GROQ_API_KEY", "") or os.getenv("GROQ_API_KEY", "")),
                model=str(getattr(self.config, "DEVAI_GROQ_MODEL", "openai/gpt-oss-120b") or "openai/gpt-oss-120b"),
                prompt=prompt,
                system=system,
                supports_json_mode=True,
            )
        if provider == "openrouter":
            return await self._call_openai_compatible(
                provider="openrouter",
                base_url=str(getattr(self.config, "DEVAI_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1") or "").rstrip("/"),
                api_key=str(getattr(self.config, "OPENROUTER_API_KEY", "") or os.getenv("OPENROUTER_API_KEY", "")),
                model=str(getattr(self.config, "DEVAI_OPENROUTER_MODEL", "qwen/qwen3-coder:free") or "qwen/qwen3-coder:free"),
                prompt=prompt,
                system=system,
                supports_json_mode=True,
                extra_headers={
                    "HTTP-Referer": str(getattr(self.config, "DEVAI_OPENROUTER_REFERER", "https://github.com/devai-bot") or ""),
                    "X-Title": "DevAI Bot Maintainer",
                },
            )
        if provider == "cerebras":
            return await self._call_openai_compatible(
                provider="cerebras",
                base_url=str(getattr(self.config, "DEVAI_CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1") or "").rstrip("/"),
                api_key=str(getattr(self.config, "CEREBRAS_API_KEY", "") or os.getenv("CEREBRAS_API_KEY", "")),
                model=str(getattr(self.config, "DEVAI_CEREBRAS_MODEL", "gpt-oss-120b") or "gpt-oss-120b"),
                prompt=prompt,
                system=system,
                supports_json_mode=True,
            )
        if provider == "cloudflare":
            account_id = str(getattr(self.config, "CLOUDFLARE_ACCOUNT_ID", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")).strip()
            token = str(getattr(self.config, "CLOUDFLARE_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")).strip()
            base_url = str(getattr(self.config, "DEVAI_CLOUDFLARE_BASE_URL", "") or "").strip()
            if not base_url and account_id:
                base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
            return await self._call_openai_compatible(
                provider="cloudflare",
                base_url=base_url.rstrip("/"),
                api_key=token,
                model=str(getattr(self.config, "DEVAI_CLOUDFLARE_MODEL", "@cf/qwen/qwen2.5-coder-32b-instruct") or "@cf/qwen/qwen2.5-coder-32b-instruct"),
                prompt=prompt,
                system=system,
                supports_json_mode=False,  # Workers AI ignora response_format em alguns modelos
            )
        if provider == "huggingface":
            return await self._call_openai_compatible(
                provider="huggingface",
                base_url=str(getattr(self.config, "DEVAI_HUGGINGFACE_BASE_URL", "https://router.huggingface.co/v1") or "").rstrip("/"),
                api_key=str(getattr(self.config, "HUGGINGFACE_API_KEY", "") or os.getenv("HUGGINGFACE_API_KEY", "")),
                model=str(getattr(self.config, "DEVAI_HUGGINGFACE_MODEL", "Qwen/Qwen3-Coder-30B-A3B-Instruct") or "Qwen/Qwen3-Coder-30B-A3B-Instruct"),
                prompt=prompt,
                system=system,
                supports_json_mode=True,
            )
        if provider == "pollinations":
            return await self._call_openai_compatible(
                provider="pollinations",
                base_url=str(getattr(self.config, "DEVAI_POLLINATIONS_BASE_URL", "https://gen.pollinations.ai/v1") or "").rstrip("/"),
                api_key=str(getattr(self.config, "POLLINATIONS_API_KEY", "") or os.getenv("POLLINATIONS_API_KEY", "")),
                model=str(getattr(self.config, "DEVAI_POLLINATIONS_MODEL", "openclaw") or "openclaw"),
                prompt=prompt,
                system=system,
                supports_json_mode=True,
            )
        raise RuntimeError(f"provider desconhecido: {provider}")

    # ----------------------------------------------------------------- HTTP

    async def _post_json(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        if not url:
            raise RuntimeError("endpoint não configurado")
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with self.session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
            text = await resp.text()
            if resp.status >= 400:
                # 401/403 = key inválida; 429 = rate limit; 5xx = servidor — todos
                # caem pro próximo provider sem precisar de tratamento especial.
                raise RuntimeError(f"HTTP {resp.status}: {text[:800]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"resposta não é JSON: {text[:800]}") from exc

    async def _call_gemini(self, prompt: str, system: str) -> AIResult:
        api_key = str(getattr(self.config, "GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")).strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY ausente")
        model = str(getattr(self.config, "DEVAI_GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self._output_tokens_for(model),
                "responseMimeType": "application/json",
            },
            # 'thinking_budget' é específico do 2.5-pro mas é ignorado nas Flash;
            # mantém zero pra responder rápido e barato.
            "safetySettings": [
                {"category": cat, "threshold": "BLOCK_NONE"}
                for cat in (
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                )
            ],
        }
        started = time.perf_counter()
        data = await self._post_json(url, {"Content-Type": "application/json"}, payload)
        text_parts: list[str] = []
        for cand in data.get("candidates", []) or []:
            content = cand.get("content") or {}
            for part in content.get("parts", []) or []:
                value = part.get("text")
                if value:
                    text_parts.append(str(value))
        return AIResult(
            "gemini",
            model,
            "\n".join(text_parts).strip(),
            int((time.perf_counter() - started) * 1000),
        )

    async def _call_openai_compatible(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        prompt: str,
        system: str,
        supports_json_mode: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> AIResult:
        api_key = (api_key or "").strip()
        if not api_key:
            raise RuntimeError(f"API key ausente para {provider}")
        if not base_url:
            raise RuntimeError(f"base_url ausente para {provider}")
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update({k: v for k, v in extra_headers.items() if v})
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self._output_tokens_for(model),
            "stream": False,
        }
        if supports_json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            data = await self._post_json(url, headers, payload)
        except RuntimeError as first_exc:
            # Fallback automático: alguns endpoints aceitam o campo `response_format`
            # mas erram com 400 dependendo do modelo. Tenta sem.
            if "response_format" not in payload:
                raise
            err_text = str(first_exc).lower()
            if "response_format" in err_text or "400" in err_text or "unsupported" in err_text:
                payload.pop("response_format", None)
                data = await self._post_json(url, headers, payload)
            else:
                raise
        choices = data.get("choices") or []
        text = ""
        if choices:
            msg = choices[0].get("message") or {}
            text = msg.get("content") or choices[0].get("text") or ""
            # Alguns modelos reasoning (DeepSeek R1, QwQ) põem o JSON no
            # reasoning_content em vez de content. Pega o que vier.
            if not text:
                text = msg.get("reasoning_content") or ""
        return AIResult(
            provider,
            model,
            str(text).strip(),
            int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------------- bookkeeping

    def _record_success(self, provider: str, latency_ms: int) -> None:
        st = self.stats[provider]
        st.success += 1
        st.last_latency_ms = latency_ms

    def _record_failure(self, provider: str, kind: str) -> None:
        st = self.stats[provider]
        st.failure += 1
        st.last_error = kind
        st.by_error[kind] += 1
