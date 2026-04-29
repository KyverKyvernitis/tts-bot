from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass
class AIResult:
    provider: str
    model: str
    text: str
    elapsed_ms: int


class DevAIClient:
    def __init__(self, session: aiohttp.ClientSession, config_module):
        self.session = session
        self.config = config_module
        self.timeout_seconds = int(getattr(config_module, "DEVAI_PROVIDER_TIMEOUT_SECONDS", 45) or 45)
        self.max_tokens = int(getattr(config_module, "DEVAI_MAX_OUTPUT_TOKENS", 12000) or 12000)
        self.temperature = float(getattr(config_module, "DEVAI_TEMPERATURE", 0.15) or 0.15)

    def provider_order(self) -> list[str]:
        raw = getattr(self.config, "DEVAI_PROVIDER_ORDER", []) or []
        if isinstance(raw, str):
            items = [p.strip().lower() for p in raw.split(",") if p.strip()]
        else:
            items = [str(p).strip().lower() for p in raw if str(p).strip()]
        return items or ["gemini", "groq", "cloudflare", "huggingface", "pollinations"]

    async def generate_patch_json(self, prompt: str) -> tuple[AIResult | None, list[str]]:
        errors: list[str] = []
        for provider in self.provider_order():
            try:
                if provider == "gemini":
                    result = await self._call_gemini(prompt)
                elif provider == "groq":
                    result = await self._call_openai_compatible(
                        provider="groq",
                        base_url=str(getattr(self.config, "DEVAI_GROQ_BASE_URL", "https://api.groq.com/openai/v1") or "https://api.groq.com/openai/v1").rstrip("/"),
                        api_key=str(getattr(self.config, "GROQ_API_KEY", "") or os.getenv("GROQ_API_KEY", "")),
                        model=str(getattr(self.config, "DEVAI_GROQ_MODEL", "openai/gpt-oss-20b") or "openai/gpt-oss-20b"),
                        prompt=prompt,
                    )
                elif provider == "cloudflare":
                    account_id = str(getattr(self.config, "CLOUDFLARE_ACCOUNT_ID", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")).strip()
                    token = str(getattr(self.config, "CLOUDFLARE_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")).strip()
                    base_url = str(getattr(self.config, "DEVAI_CLOUDFLARE_BASE_URL", "") or "").strip()
                    if not base_url and account_id:
                        base_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
                    result = await self._call_openai_compatible(
                        provider="cloudflare",
                        base_url=base_url.rstrip("/"),
                        api_key=token,
                        model=str(getattr(self.config, "DEVAI_CLOUDFLARE_MODEL", "@cf/meta/llama-3.1-8b-instruct") or "@cf/meta/llama-3.1-8b-instruct"),
                        prompt=prompt,
                    )
                elif provider == "huggingface":
                    result = await self._call_openai_compatible(
                        provider="huggingface",
                        base_url=str(getattr(self.config, "DEVAI_HUGGINGFACE_BASE_URL", "https://router.huggingface.co/v1") or "https://router.huggingface.co/v1").rstrip("/"),
                        api_key=str(getattr(self.config, "HUGGINGFACE_API_KEY", "") or os.getenv("HUGGINGFACE_API_KEY", "")),
                        model=str(getattr(self.config, "DEVAI_HUGGINGFACE_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct") or "Qwen/Qwen2.5-Coder-32B-Instruct"),
                        prompt=prompt,
                    )
                elif provider == "pollinations":
                    result = await self._call_openai_compatible(
                        provider="pollinations",
                        base_url=str(getattr(self.config, "DEVAI_POLLINATIONS_BASE_URL", "https://gen.pollinations.ai/v1") or "https://gen.pollinations.ai/v1").rstrip("/"),
                        api_key=str(getattr(self.config, "POLLINATIONS_API_KEY", "") or os.getenv("POLLINATIONS_API_KEY", "")),
                        model=str(getattr(self.config, "DEVAI_POLLINATIONS_MODEL", "openai") or "openai"),
                        prompt=prompt,
                    )
                else:
                    errors.append(f"{provider}: provider desconhecido")
                    continue
                if result and result.text.strip():
                    return result, errors
                errors.append(f"{provider}: resposta vazia")
            except Exception as exc:
                errors.append(f"{provider}: {type(exc).__name__}: {exc}")
        return None, errors

    async def _post_json(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        if not url:
            raise RuntimeError("endpoint não configurado")
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with self.session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {text[:800]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"resposta não é JSON: {text[:800]}") from exc

    async def _call_gemini(self, prompt: str) -> AIResult:
        api_key = str(getattr(self.config, "GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")).strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY ausente")
        model = str(getattr(self.config, "DEVAI_GEMINI_MODEL", "gemini-2.0-flash") or "gemini-2.0-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "systemInstruction": {
                "parts": [{"text": "Você é uma IA de manutenção de um bot Discord Python. Responda somente com JSON válido."}]
            },
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
                "responseMimeType": "application/json",
            },
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
        return AIResult("gemini", model, "\n".join(text_parts).strip(), int((time.perf_counter() - started) * 1000))

    async def _call_openai_compatible(self, *, provider: str, base_url: str, api_key: str, model: str, prompt: str) -> AIResult:
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
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "Você é uma IA de manutenção de um bot Discord Python. Responda somente com JSON válido.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        # Alguns providers compatíveis aceitam response_format, outros ignoram/rejeitam.
        if provider in {"groq", "huggingface", "pollinations"}:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            data = await self._post_json(url, headers, payload)
        except RuntimeError as first_exc:
            if "response_format" not in payload:
                raise
            payload.pop("response_format", None)
            data = await self._post_json(url, headers, payload)
        choices = data.get("choices") or []
        text = ""
        if choices:
            msg = choices[0].get("message") or {}
            text = msg.get("content") or choices[0].get("text") or ""
        return AIResult(provider, model, str(text).strip(), int((time.perf_counter() - started) * 1000))
