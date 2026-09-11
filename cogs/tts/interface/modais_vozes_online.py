from __future__ import annotations

import discord

import config
from ..utils.embed import human_language_name, human_pitch, human_rate, human_voice_name
from .catalogos_de_vozes import (
    idioma_edge_da_voz,
    opcoes_idiomas_edge,
    opcoes_vozes_edge_por_idioma,
    primeira_voz_edge_por_idioma,
    principais_opcoes_idiomas_gtts,
    voz_edge_corresponde_idioma,
)
from .componentes import (
    adicionar_entrada_texto_modal,
    adicionar_item_rotulo_modal,
    adicionar_radio_modal,
    criar_entrada_texto_modal,
    criar_seletor_modal,
    opcoes_com_valor_padrao,
    rotulo_modal_disponivel,
    valor_item,
    valor_tts_atual,
    valor_unico_componente,
)
from .operacoes_painel import salvar_atualizacoes_modal_tts


class ModalConfiguracaoEdge(discord.ui.Modal, title="Editar Edge"):
    def __init__(
        self,
        cog: "TTSVoice",
        panel_message: discord.Message | None,
        *,
        server: bool,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
        force_text_fallback: bool = False,
    ):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.force_text_fallback = bool(force_text_fallback)
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        self.current_voice = valor_tts_atual(
            cog,
            guild_id,
            user_id,
            "voice",
            str(getattr(config, "EDGE_TTS_VOICE", "pt-BR-FranciscaNeural") or "pt-BR-FranciscaNeural"),
            server=server,
        )
        self.current_language = idioma_edge_da_voz(self.current_voice)
        self.current_rate = valor_tts_atual(cog, guild_id, user_id, "rate", "+0%", server=server)
        self.current_pitch = valor_tts_atual(cog, guild_id, user_id, "pitch", "+0Hz", server=server)
        if self.force_text_fallback or not self._build_guided_modal():
            self._build_text_fallback()

    def _build_guided_modal(self) -> bool:
        if not rotulo_modal_disponivel():
            return False
        try:
            language_options = opcoes_idiomas_edge(self.cog, self.current_language)
            voice_options = opcoes_vozes_edge_por_idioma(
                self.cog,
                idioma=self.current_language,
                atual=self.current_voice,
            )
            if not voice_options:
                return False
            language_select = criar_seletor_modal(
                "edge_language",
                placeholder="Idioma Edge",
                options=language_options,
            )
            voice_select = criar_seletor_modal(
                "edge_voice",
                placeholder="Escolha a voz Edge",
                options=voice_options,
            )
            ok = adicionar_item_rotulo_modal(
                self,
                "language",
                text="Idioma Edge",
                description="",
                component=language_select,
            )
            ok = ok and adicionar_item_rotulo_modal(
                self,
                "voice",
                text="Voz Edge",
                description="",
                component=voice_select,
            )
            ok = ok and adicionar_radio_modal(
                self,
                "rate",
                text="Velocidade Edge",
                description="",
                current=self.current_rate,
                options=[
                    ("Bem mais lenta", "-50%", ""),
                    ("Mais lenta", "-25%", ""),
                    ("Normal", "+0%", ""),
                    ("Mais rápida", "+25%", ""),
                    ("Bem mais rápida", "+50%", ""),
                ],
            )
            ok = ok and adicionar_radio_modal(
                self,
                "pitch",
                text="Tom Edge",
                description="",
                current=self.current_pitch,
                options=[
                    ("Bem mais grave", "-50Hz", ""),
                    ("Mais grave", "-25Hz", ""),
                    ("Normal", "+0Hz", ""),
                    ("Mais agudo", "+25Hz", ""),
                    ("Bem mais agudo", "+50Hz", ""),
                ],
            )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] Edge guiado falhou: {e!r}")
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        adicionar_entrada_texto_modal(
            self,
            "language",
            label="Idioma Edge",
            placeholder="Ex.: pt-BR, en-US, es-ES",
            current=self.current_language,
            max_length=16,
        )
        adicionar_entrada_texto_modal(
            self,
            "voice",
            label="Voz Edge",
            placeholder="Voz usada com ,texto. Ex.: pt-BR-FranciscaNeural",
            current=self.current_voice,
            max_length=80,
        )
        adicionar_entrada_texto_modal(
            self,
            "rate",
            label="Velocidade Edge",
            placeholder="Use +0% normal, -25% lenta ou +25% rápida",
            current=self.current_rate,
            max_length=8,
        )
        adicionar_entrada_texto_modal(
            self,
            "pitch",
            label="Tom Edge",
            placeholder="Use +0Hz normal, -25Hz grave ou +25Hz agudo",
            current=self.current_pitch,
            max_length=8,
        )

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        details: list[str] = []

        language = valor_unico_componente(getattr(self, "language", None), self.current_language)
        selected_language = str(language or self.current_language or "pt-BR").strip() or "pt-BR"
        voice = valor_unico_componente(getattr(self, "voice", None), self.current_voice)
        selected_voice = str(voice or self.current_voice or "").strip()
        adjusted_voice = ""
        if selected_language and selected_voice and not voz_edge_corresponde_idioma(selected_voice, selected_language):
            adjusted_voice = primeira_voz_edge_por_idioma(self.cog, selected_language, self.current_voice)
            if not adjusted_voice:
                await interaction.response.send_message(
                    embed=self.cog._make_embed("Idioma indisponível", "Não encontrei uma voz Edge disponível para esse idioma.", ok=False),
                    ephemeral=True,
                )
                return
            selected_voice = adjusted_voice
        if selected_voice and str(selected_voice) != str(self.current_voice):
            if selected_voice not in self.cog.edge_voice_names and selected_voice not in self.cog.edge_voice_cache:
                await interaction.response.send_message(
                    embed=self.cog._make_embed("Voz inválida", "Essa voz Edge não foi encontrada.", ok=False),
                    ephemeral=True,
                )
                return
            updates["voice"] = selected_voice
            if selected_language != self.current_language and adjusted_voice:
                details.append(f"Idioma · {human_language_name(selected_language)}")
                details.append(f"Voz · {human_voice_name(selected_voice)}")
            else:
                details.append(f"Voz · {human_voice_name(selected_voice)}")
        elif selected_language != self.current_language:
            picked_voice = primeira_voz_edge_por_idioma(self.cog, selected_language, self.current_voice)
            if not picked_voice:
                await interaction.response.send_message(
                    embed=self.cog._make_embed("Idioma indisponível", "Não encontrei uma voz Edge disponível para esse idioma.", ok=False),
                    ephemeral=True,
                )
                return
            if picked_voice != self.current_voice:
                updates["voice"] = picked_voice
                details.append(f"Idioma · {human_language_name(selected_language)}")
                details.append(f"Voz · {human_voice_name(picked_voice)}")

        rate = valor_unico_componente(getattr(self, "rate", None), self.current_rate)
        if rate:
            normalized = self.cog._normalize_rate_value(rate)
            if normalized is None:
                await interaction.response.send_message(
                    embed=self.cog._make_embed("Velocidade inválida", "Use opções como `+0%`, `-25%` ou `+25%`.", ok=False),
                    ephemeral=True,
                )
                return
            current_rate = self.cog._normalize_rate_value(self.current_rate) or self.current_rate
            if str(normalized) != str(current_rate):
                updates["rate"] = normalized
                details.append(f"Velocidade · {human_rate(normalized)}")

        pitch = valor_unico_componente(getattr(self, "pitch", None), self.current_pitch)
        if pitch:
            normalized = self.cog._normalize_pitch_value(pitch)
            if normalized is None:
                await interaction.response.send_message(
                    embed=self.cog._make_embed("Tom inválido", "Use opções como `+0Hz`, `-25Hz` ou `+25Hz`.", ok=False),
                    ephemeral=True,
                )
                return
            current_pitch = self.cog._normalize_pitch_value(self.current_pitch) or self.current_pitch
            if str(normalized) != str(current_pitch):
                updates["pitch"] = normalized
                details.append(f"Tom · {human_pitch(normalized)}")

        await salvar_atualizacoes_modal_tts(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            success_title="Edge atualizado",
            success_description=" · ".join(details) if details else "Nada mudou",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class ModalConfiguracaoGTTS(discord.ui.Modal, title="Editar gTTS"):
    def __init__(
        self,
        cog: "TTSVoice",
        panel_message: discord.Message | None,
        *,
        server: bool,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
        force_text_fallback: bool = False,
    ):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.force_text_fallback = bool(force_text_fallback)
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        self.current_language = valor_tts_atual(cog, guild_id, user_id, "language", "pt-br", server=server)
        if self.force_text_fallback or not self._build_guided_modal():
            self._build_text_fallback()

    def _build_guided_modal(self) -> bool:
        if not rotulo_modal_disponivel():
            return False
        try:
            language_select = criar_seletor_modal(
                "gtts_language",
                placeholder="Idioma gTTS",
                options=opcoes_com_valor_padrao(principais_opcoes_idiomas_gtts(self.cog, self.current_language), self.current_language),
            )
            ok = adicionar_item_rotulo_modal(
                self,
                "language",
                text="Idioma gTTS",
                description="",
                component=language_select,
            )
            manual_input = criar_entrada_texto_modal(
                label=None,
                placeholder="Opcional: pt-br, en, es, ja",
                current="",
                max_length=10,
                required=False,
            )
            ok = ok and adicionar_item_rotulo_modal(
                self,
                "manual_language",
                text="Outro idioma",
                description="Opcional. Substitui a seleção acima.",
                component=manual_input,
            )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] gTTS guiado falhou: {e!r}")
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        adicionar_entrada_texto_modal(
            self,
            "language",
            label="Idioma gTTS",
            placeholder="Ex.: pt-br, en, es, fr, ja",
            current=self.current_language,
            max_length=10,
        )

    async def on_submit(self, interaction: discord.Interaction):
        selected = valor_unico_componente(getattr(self, "language", None), self.current_language)
        manual = valor_item(getattr(self, "manual_language", None))
        raw_value = manual or selected
        code, _language_name = self.cog._resolve_gtts_language_input(raw_value)
        if code is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Idioma inválido", "Use algo como `pt-br`, `en`, `es` ou `ja`.", ok=False),
                ephemeral=True,
            )
            return
        updates = {"language": code} if str(code) != str(self.current_language) else {}
        await salvar_atualizacoes_modal_tts(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            success_title="gTTS atualizado",
            success_description=f"Idioma · {human_language_name(code)}" if updates else "Nada mudou",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
