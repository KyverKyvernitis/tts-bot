from __future__ import annotations

import asyncio
import json
import time
import traceback
import urllib.request
from typing import TYPE_CHECKING

import discord

import config
from ..common import _shorten as encurtar
from .componentes import (
    valor_tts_atual,
    valor_unico_componente,
    opcoes_com_valor_padrao,
    rotulo_modal_disponivel,
    criar_entrada_texto_modal,
    adicionar_entrada_texto_modal,
    adicionar_item_rotulo_modal,
    criar_seletor_modal,
    adicionar_radio_modal,
)
from .valores_atts import (
    normalizar_localidade_atts,
    normalizar_fator_atts,
    normalizar_fator_personalizado_atts,
    padrao_radio_modal_atts,
    separar_valores_personalizados_atts,
)
from .catalogo_atts import (
    opcoes_idiomas_atts,
    pontuar_voz_atts,
    vozes_atts_por_idioma,
    opcoes_vozes_atts_por_idioma,
    voz_atts_corresponde_idioma,
    primeira_voz_atts_por_idioma,
    catalogo_atts_pronto_para_idioma,
)
from .operacoes_painel import salvar_atualizacoes_modal_tts

if TYPE_CHECKING:
    from ..cog import TTSVoice


CACHE_CATALOGO_VOZES_ATTS: dict[str, object] = {"by_locale": {}, "last_error": ""}
MENSAGEM_ERRO_CARREGAMENTO_ATTS = "ATTS indisponível no momento. Tente novamente em instantes."


def chave_cache_catalogo_vozes_atts(locale: str = "") -> str:
    normalized = normalizar_localidade_atts(locale or "", "").strip()
    return normalized.casefold() if normalized else "all"


def buscar_catalogo_vozes_atts_sincrono(locale: str = "", *, limite: int = 500, timeout: float = 2.2, usar_cache: bool = True) -> list[dict[str, object]]:
    """Busca o catálogo de vozes do ATTS antes de abrir o modal.

    O modal do Discord não atualiza opções depois de aberto; por isso esta
    função precisa retornar as vozes do idioma salvo atual antes do send_modal.
    """
    now = time.monotonic()
    key = chave_cache_catalogo_vozes_atts(locale)
    by_locale = CACHE_CATALOGO_VOZES_ATTS.setdefault("by_locale", {})
    if isinstance(by_locale, dict) and usar_cache:
        cached = by_locale.get(key)
        if isinstance(cached, dict) and now - float(cached.get("at") or 0.0) <= 180.0:
            cached_voices = cached.get("voices")
            if isinstance(cached_voices, list) and cached_voices:
                return [v for v in cached_voices if isinstance(v, dict)]
    try:
        enabled = bool(getattr(config, "PHONE_WORKER_ENABLED", False))
        host = str(getattr(config, "PHONE_WORKER_HOST", "") or "").strip()
        token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
        if not enabled or not host or not token:
            CACHE_CATALOGO_VOZES_ATTS["last_error"] = "worker_unavailable"
            return []
        scheme = str(getattr(config, "PHONE_WORKER_SCHEME", "http") or "http").strip().lower() or "http"
        if scheme not in {"http", "https"}:
            scheme = "http"
        port = int(getattr(config, "PHONE_WORKER_PORT", 8766) or 8766)
        url = f"{scheme}://{host}:{port}/task"
        payload = {"task": "tts_android_voices", "locale": str(locale or ""), "limit": int(limite or 500)}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "CoreBot/ATTSModal",
        })
        with urllib.request.urlopen(req, timeout=max(0.2, float(timeout))) as response:
            raw = response.read(1024 * 1024)
        parsed = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        if not isinstance(parsed, dict) or not bool(parsed.get("ok", True)):
            CACHE_CATALOGO_VOZES_ATTS["last_error"] = str((parsed or {}).get("error") if isinstance(parsed, dict) else "invalid_response")[:180]
            return []
        voices = parsed.get("voices")
        if not isinstance(voices, list):
            CACHE_CATALOGO_VOZES_ATTS["last_error"] = "missing_voices"
            return []
        normalized = [v for v in voices if isinstance(v, dict) and str(v.get("name") or "").strip()]
        if isinstance(by_locale, dict) and normalized:
            by_locale[key] = {"at": now, "voices": normalized}
            CACHE_CATALOGO_VOZES_ATTS["last_error"] = ""
        elif not normalized:
            CACHE_CATALOGO_VOZES_ATTS["last_error"] = "empty_voices"
        return normalized
    except Exception as e:
        CACHE_CATALOGO_VOZES_ATTS["last_error"] = f"{type(e).__name__}: {encurtar(str(e), 160)}"
        print(f"[tts_modal] catálogo ATTS indisponível: {e!r}")
        return []


def pontuacao_voz_atts(voice: dict[str, object], language: str) -> int:
    return pontuar_voz_atts(voice, language)


def vozes_correspondentes_idioma_atts(catalog: list[dict[str, object]] | None, language: str) -> list[dict[str, object]]:
    return vozes_atts_por_idioma(catalog, language)


def opcoes_vozes_modal_atts(cog: "TTSVoice", *, language: str, current: str = "", catalog: list[dict[str, object]] | None = None) -> list[discord.SelectOption]:
    return opcoes_vozes_atts_por_idioma(
        cog,
        idioma=language,
        atual=current,
        catalogo=catalog,
        buscar_catalogo=buscar_catalogo_vozes_atts_sincrono,
    )


def voz_modal_atts_corresponde_idioma(voice: str, language: str) -> bool:
    return voz_atts_corresponde_idioma(
        voice,
        language,
        buscar_catalogo=buscar_catalogo_vozes_atts_sincrono,
    )


def primeira_voz_modal_atts_por_idioma(cog: "TTSVoice", language: str, current: str = "") -> str:
    # Mantém a chamada pela fachada legada para preservar pontos de patch.
    options = opcoes_vozes_modal_atts(cog, language=language, current=current)
    return primeira_voz_atts_por_idioma(options)


def idioma_atual_modal_atts(cog: "TTSVoice", panel_message: discord.Message | None, *, servidor: bool, id_usuario_alvo: int | None = None) -> str:
    user_id = int(id_usuario_alvo or 0)
    guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
    return normalizar_localidade_atts(valor_tts_atual(cog, guild_id, user_id, "android_language", "pt-BR", server=servidor), "pt-BR")


def catalogo_pronto_modal_atts(catalog: list[dict[str, object]] | None, language: str) -> bool:
    return catalogo_atts_pronto_para_idioma(catalog, language)


async def carregar_catalogo_vozes_atts_para_modal(language: str) -> list[dict[str, object]]:
    language = normalizar_localidade_atts(language, "pt-BR")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(buscar_catalogo_vozes_atts_sincrono, language, limite=500, timeout=2.2, usar_cache=True),
            timeout=2.7,
        )
    except Exception as e:
        CACHE_CATALOGO_VOZES_ATTS["last_error"] = f"{type(e).__name__}: {encurtar(str(e), 160)}"
        print(f"[tts_modal] catálogo ATTS indisponível antes do modal: {e!r}")
        return []


async def enviar_indisponibilidade_atts_minima(interaction: discord.Interaction) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(MENSAGEM_ERRO_CARREGAMENTO_ATTS, ephemeral=True)
        else:
            await interaction.response.send_message(MENSAGEM_ERRO_CARREGAMENTO_ATTS, ephemeral=True)
    except Exception:
        pass


async def enviar_modal_configuracao_atts(
    interaction: discord.Interaction,
    cog: "TTSVoice",
    panel_message: discord.Message | None,
    *,
    servidor: bool,
    id_usuario_alvo: int | None = None,
    nome_usuario_alvo: str | None = None,
    contexto: str = "atts",
) -> None:
    language = idioma_atual_modal_atts(cog, panel_message, servidor=servidor, id_usuario_alvo=id_usuario_alvo)
    catalog = await carregar_catalogo_vozes_atts_para_modal(language)
    if not catalogo_pronto_modal_atts(catalog, language):
        print(f"[tts_modal] ATTS indisponível em {contexto}: language={language} voices={len(catalog or [])} error={CACHE_CATALOGO_VOZES_ATTS.get('last_error')!r}")
        await enviar_indisponibilidade_atts_minima(interaction)
        return
    try:
        await interaction.response.send_modal(
            ModalConfiguracaoATTS(
                cog,
                panel_message,
                server=servidor,
                target_user_id=id_usuario_alvo,
                target_user_name=nome_usuario_alvo,
                voice_catalog=catalog,
            )
        )
    except Exception as e:
        print(f"[tts_modal] modal ATTS falhou em {contexto}: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)
        await enviar_indisponibilidade_atts_minima(interaction)


class ModalConfiguracaoATTS(discord.ui.Modal, title="Editar ATTS"):
    def __init__(
        self,
        cog: "TTSVoice",
        panel_message: discord.Message | None,
        *,
        server: bool,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
        force_text_fallback: bool = False,
        voice_catalog: list[dict[str, object]] | None = None,
        allow_text_fallback: bool = False,
    ):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.force_text_fallback = bool(force_text_fallback)
        self.allow_text_fallback = bool(allow_text_fallback)
        self.voice_catalog = list(voice_catalog or [])
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        self.current_language = normalizar_localidade_atts(valor_tts_atual(cog, guild_id, user_id, "android_language", "pt-BR", server=server), "pt-BR")
        self.current_voice = str(valor_tts_atual(cog, guild_id, user_id, "android_voice", "", server=server) or "").strip()
        self.current_rate = normalizar_fator_atts(valor_tts_atual(cog, guild_id, user_id, "android_rate", "1.0", server=server), "1.0") or "1.0"
        self.current_pitch = normalizar_fator_atts(valor_tts_atual(cog, guild_id, user_id, "android_pitch", "1.0", server=server), "1.0") or "1.0"

        guided_ok = False if self.force_text_fallback else self._build_guided_modal()
        if guided_ok:
            return

        # O ATTS depende do catálogo do worker para montar as opções.
        # Se o modal guiado não puder ser montado, não abrimos o formulário antigo
        # de campos livres para o usuário comum; mostramos a mensagem mínima no
        # chamador. O fallback textual fica só para chamadas legadas explícitas.
        if self.force_text_fallback or self.allow_text_fallback:
            self._build_text_fallback()
            return

        raise RuntimeError("atts_guided_modal_unavailable")

    def _build_guided_modal(self) -> bool:
        if not rotulo_modal_disponivel():
            return False
        try:
            catalog = list(self.voice_catalog or [])
            if not catalogo_pronto_modal_atts(catalog, self.current_language):
                return False
            language_select = criar_seletor_modal(
                "android_language",
                placeholder="Idioma ATTS",
                options=opcoes_com_valor_padrao(opcoes_idiomas_atts(self.current_language, catalog), self.current_language),
            )
            voice_select = criar_seletor_modal(
                "android_voice",
                placeholder="Voz ATTS",
                options=opcoes_vozes_modal_atts(self.cog, language=self.current_language, current=self.current_voice, catalog=catalog),
            )
            ok = adicionar_item_rotulo_modal(
                self,
                "language",
                text="Idioma ATTS",
                description="",
                component=language_select,
            )
            ok = ok and adicionar_item_rotulo_modal(
                self,
                "voice",
                text="Voz ATTS",
                description="",
                component=voice_select,
            )
            rate_presets = {"0.75", "1.0", "1.25", "1.5"}
            pitch_presets = {"0.8", "1.0", "1.2", "1.4"}
            ok = ok and adicionar_radio_modal(
                self,
                "rate",
                text="Velocidade ATTS",
                description="",
                current=padrao_radio_modal_atts(self.current_rate, rate_presets),
                options=[
                    ("Mais lenta", "0.75", ""),
                    ("Normal", "1.0", ""),
                    ("Mais rápida", "1.25", ""),
                    ("Bem mais rápida", "1.5", ""),
                    ("Custom", "custom", "Usa o valor custom abaixo"),
                ],
            )
            ok = ok and adicionar_radio_modal(
                self,
                "pitch",
                text="Tom ATTS",
                description="",
                current=padrao_radio_modal_atts(self.current_pitch, pitch_presets),
                options=[
                    ("Mais grave", "0.8", ""),
                    ("Normal", "1.0", ""),
                    ("Mais agudo", "1.2", ""),
                    ("Bem mais agudo", "1.4", ""),
                    ("Custom", "custom", "Usa o valor custom abaixo"),
                ],
            )
            custom_values = criar_entrada_texto_modal(
                label=None,
                placeholder="Velocidade / tom — ex.: 1.0 / 1.0",
                current=f"{self.current_rate or '1.0'} / {self.current_pitch or '1.0'}",
                max_length=32,
                required=False,
            )
            ok = ok and adicionar_item_rotulo_modal(
                self,
                "custom_values",
                text="Valores custom",
                description="Use quando escolher Custom em velocidade ou tom.",
                component=custom_values,
            )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] ATTS guiado falhou: {e!r}")
            traceback.print_exception(type(e), e, e.__traceback__)
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        adicionar_entrada_texto_modal(
            self,
            "language",
            label="Idioma ATTS",
            placeholder="Ex.: pt-BR, en-US, es-ES",
            current=self.current_language or "pt-BR",
            max_length=16,
        )
        adicionar_entrada_texto_modal(
            self,
            "voice",
            label="Voz ATTS",
            placeholder="Vazio/auto usa a voz padrão do Android",
            current=self.current_voice or "",
            max_length=96,
            required=False,
        )
        adicionar_entrada_texto_modal(
            self,
            "rate",
            label="Velocidade ATTS",
            placeholder="1.0 normal, 0.8 lenta, 1.25 rápida",
            current=self.current_rate or "1.0",
            max_length=8,
        )
        adicionar_entrada_texto_modal(
            self,
            "pitch",
            label="Tom ATTS",
            placeholder="1.0 normal, 0.8 grave, 1.2 agudo",
            current=self.current_pitch or "1.0",
            max_length=8,
        )

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        details: list[str] = []

        language = normalizar_localidade_atts(valor_unico_componente(getattr(self, "language", None), self.current_language), "pt-BR")
        current_language = normalizar_localidade_atts(self.current_language, "pt-BR")
        if language != current_language:
            updates["android_language"] = language
            details.append(f"• Idioma: `{language}`")

        raw_voice = valor_unico_componente(getattr(self, "voice", None), self.current_voice).strip()
        raw_lower = raw_voice.casefold()
        if raw_lower in {"", "auto", "automatica", "automática", "rapida", "rápida", "automatica rapida", "automática rápida"}:
            voice = ""
            voice_label = "Automática rápida"
        elif raw_lower in {"default", "padrao", "padrão", "voz padrao", "voz padrão"}:
            voice = "default"
            voice_label = "Padrão do Android"
        else:
            voice = raw_voice[:96]
            voice_label = voice

        # Se o usuário trocou o idioma no mesmo modal, o Discord ainda mostra
        # as vozes do idioma anterior até o próximo modal. Evitamos salvar uma
        # voz incompatível e voltamos para automática rápida.
        if voice and voice != "default" and not voz_modal_atts_corresponde_idioma(voice, language):
            voice = ""
            voice_label = "Automática rápida"

        current_voice = str(self.current_voice or "").strip()
        current_norm = "" if current_voice.casefold() in {"", "auto", "automatica", "automática"} else current_voice
        if voice != current_norm:
            updates["android_voice"] = voice
            details.append(f"• Voz: `{voice_label}`")

        rate_choice = valor_unico_componente(getattr(self, "rate", None), self.current_rate)
        pitch_choice = valor_unico_componente(getattr(self, "pitch", None), self.current_pitch)
        custom_rate_raw, custom_pitch_raw = separar_valores_personalizados_atts(
            valor_unico_componente(getattr(self, "custom_values", None), f"{self.current_rate or '1.0'} / {self.current_pitch or '1.0'}"),
            taxa_padrao=self.current_rate or "1.0",
            tom_padrao=self.current_pitch or "1.0",
        )

        if str(rate_choice or "").strip().casefold() == "custom":
            rate = normalizar_fator_personalizado_atts(custom_rate_raw)
            if rate is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Velocidade inválida", "Use um número entre 0.5 e 2.0.", ok=False), ephemeral=True)
                return
        else:
            rate = normalizar_fator_atts(rate_choice, "1.0")
            if rate is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Velocidade inválida", "Use uma opção de velocidade do ATTS.", ok=False), ephemeral=True)
                return
        current_rate = normalizar_fator_atts(self.current_rate, "1.0") or "1"
        if rate != current_rate:
            updates["android_rate"] = rate
            details.append(f"• Velocidade: `{rate}x`")

        if str(pitch_choice or "").strip().casefold() == "custom":
            pitch = normalizar_fator_personalizado_atts(custom_pitch_raw)
            if pitch is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Tom inválido", "Use um número entre 0.5 e 2.0.", ok=False), ephemeral=True)
                return
        else:
            pitch = normalizar_fator_atts(pitch_choice, "1.0")
            if pitch is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Tom inválido", "Use uma opção de tom do ATTS.", ok=False), ephemeral=True)
                return
        current_pitch = normalizar_fator_atts(self.current_pitch, "1.0") or "1"
        if pitch != current_pitch:
            updates["android_pitch"] = pitch
            details.append(f"• Tom: `{pitch}x`")


        await salvar_atualizacoes_modal_tts(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            success_title="ATTS atualizado",
            success_description="\n".join(details) if details else "Nada mudou.",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
