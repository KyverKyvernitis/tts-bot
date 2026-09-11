import contextlib
import re
import weakref
import unicodedata
from urllib.parse import urlparse
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .common import _shorten, validate_mode
from .prefix import validate_prefix_values
from .utils.embed import build_settings_panel_text_from_embed
from .interface.componentes import (
    adicionar_radio_modal as _add_modal_radio,
    criar_seletor_modal as _make_modal_select,
    adicionar_item_rotulo_modal as _add_modal_label_item,
    adicionar_entrada_texto_modal as _add_modal_text_input,
    criar_entrada_texto_modal as _make_modal_text_input,
    rotulo_modal_disponivel as _modal_label_available,
    opcoes_com_valor_padrao as _with_default_option,
    valor_unico_componente as _single_component_value,
    valor_tts_atual as _current_tts_value,
    valores_selecionados as _select_values,
    cargos_selecionados as _selected_roles,
    primeiro_cargo_selecionado as _first_selected_role,
    valor_item as _item_value,
    componentes_experimentais_modal_ativos as _experimental_modal_components_enabled,
    tentar_adicionar_grupo_radio as _maybe_add_radio_group,
    tentar_adicionar_grupo_checkbox as _maybe_add_checkbox_group,
    criar_seletor_opcional as _make_optional_select,
    valores_padrao_seletor_cargo as _role_select_default_values,
    criar_seletor_cargo_modal as _make_modal_role_select,
    valores_radio_correspondem as _radio_value_matches,
    criar_radio_modal as _make_modal_radio,
    criar_grupo_checkbox_modal as _make_modal_checkbox_group,
)


from .interface.catalogos_de_vozes import (
    idioma_edge_da_voz,
    voz_edge_corresponde_idioma,
    opcoes_idiomas_edge,
    opcoes_vozes_edge_por_idioma,
    primeira_voz_edge_por_idioma,
    principais_opcoes_vozes_edge,
    principais_opcoes_idiomas_gtts,
)
from .interface.valores_atts import (
    normalizar_localidade_atts,
    normalizar_fator_atts,
    normalizar_fator_personalizado_atts,
    padrao_radio_modal_atts,
    separar_valores_personalizados_atts,
)
from .interface.catalogo_atts import (
    localidade_corresponde_idioma_atts,
    localidade_da_voz_atts,
    opcoes_idiomas_atts,
    pontuar_voz_atts,
    vozes_atts_por_idioma,
    opcoes_vozes_atts_por_idioma,
    voz_atts_corresponde_idioma,
    primeira_voz_atts_por_idioma,
    catalogo_atts_pronto_para_idioma,
)
from .interface.modais_simples import (
    ModalCodigoIdioma as LanguageCodeModal,
    VisaoAjudaIdioma as LanguageHelpView,
    ModalPrefixoBot as BotPrefixModal,
    ModalPrefixoATTS as ATTSPrefixModal,
    ModalPrefixoTeto as TetoPrefixModal,
    ModalPrefixoGTTS as GTTSPrefixModal,
    ModalPrefixoEdge as EdgePrefixModal,
    SeletorCargoIgnorado as IgnoredRoleSelect,
    ModalApelidoFalado as SpokenNameModal,
)
from .interface.modais_vozes_online import (
    ModalConfiguracaoEdge as EdgeSettingsModal,
    ModalConfiguracaoGTTS as GTTSSettingsModal,
)
from .interface.modais_atts import (
    CACHE_CATALOGO_VOZES_ATTS as _ATTS_VOICE_CATALOG_CACHE,
    MENSAGEM_ERRO_CARREGAMENTO_ATTS as ATTS_LOAD_ERROR_MESSAGE,
    chave_cache_catalogo_vozes_atts,
    buscar_catalogo_vozes_atts_sincrono,
    idioma_atual_modal_atts,
    carregar_catalogo_vozes_atts_para_modal,
    enviar_indisponibilidade_atts_minima,
    enviar_modal_configuracao_atts,
    ModalConfiguracaoATTS,
)
from .interface.modais_servidor import (
    ModalPrefixosServidor as ServerPrefixesModal,
    ModalRegrasServidorTTS as TTSServerRulesModal,
)


from .interface.visoes_base import (
    DURACAO_EXPIRACAO_PAINEL_TTS as TTS_PANEL_EXPIRE_AFTER_SECONDS,
    DURACAO_DESPACHO_PAINEL_TTS as TTS_PANEL_DISPATCH_TIMEOUT_SECONDS,
    EMOJI_PAINEL_TTS_EXPIRADO as TTS_EXPIRED_EMOJI,
    dica_comando_painel_expirado as _fallback_panel_command_hint,
    mensagem_painel_expirado as _fallback_expired_panel_message,
    VisaoBaseTTS as _BaseTTSView,
    VisaoSelecaoSimples as _SimpleSelectView,
)
from .interface.seletores_basicos import (
    SeletorModo as ModeSelect,
    SeletorIdioma as LanguageSelect,
    SeletorVelocidade as SpeedSelect,
    SeletorTom as PitchSelect,
    SeletorRegiaoVoz as VoiceRegionSelect,
    SeletorVoz as VoiceSelect,
    SeletorToggle as ToggleSelect,
)
from .interface.controles_paineis import (
    BotaoLancadorPublicoTTS,
    SeletorAlvoPrefixo,
    SeletorPainelPrincipalTTS,
    SeletorAcaoModoTTS,
)
from .interface.visoes_auxiliares import (
    VisaoConfiguracaoCargoIgnorado as IgnoreRoleConfigView,
    VisaoLeituraRapidaTTS as TTSReadingQuickView,
    VisaoStatusTTS as TTSStatusView,
    VisaoPainelToggleTTS as TTSTogglePanelView,
)
from .interface.visoes_layout import (
    VisaoLayoutBaseTTS as _BaseTTSLayoutView,
)
from .interface.visao_acoes_avancadas import (
    VisaoAcoesAvancadasTTS as TTSAdvancedActionsView,
)
from .interface.visao_acoes_modo import (
    VisaoAcoesModoTTS,
)
from .interface.visao_lancador_publico import (
    VisaoLancadorPublicoTTS,
)
from .interface.visao_painel_principal import (
    VisaoPainelPrincipalTTS,
)
from .interface.operacoes_painel import (
    DESCRICAO_LANCADOR_TTS as TTS_LAUNCHER_DESCRIPTION,
    salvar_atualizacoes_modal_tts as _save_tts_modal_updates,
    enviar_modal_configuracao_com_fallback as _send_settings_modal_with_fallback,
    reiniciar_selecao_lancador_publico as _reset_public_launcher_select,
)


# Fachadas de compatibilidade: os nomes privados antigos seguem disponíveis
# para consumidores e testes legados, mas a implementação vive em interface/.
def _edge_language_from_voice(voice: str, default: str = "pt-BR") -> str:
    return idioma_edge_da_voz(voice, default)


def _edge_voice_matches_language(voice: str, language: str) -> bool:
    return voz_edge_corresponde_idioma(voice, language)


def _edge_language_options(cog: "TTSVoice", current: str = "") -> list[discord.SelectOption]:
    return opcoes_idiomas_edge(cog, current)


def _edge_voice_options_for_language(cog: "TTSVoice", *, language: str, current: str = "") -> list[discord.SelectOption]:
    return opcoes_vozes_edge_por_idioma(cog, idioma=language, atual=current)


def _pick_first_edge_voice_for_language(cog: "TTSVoice", language: str, current: str = "") -> str:
    return primeira_voz_edge_por_idioma(cog, language, current)


def _top_edge_voice_options(cog: "TTSVoice", current: str = "") -> list[discord.SelectOption]:
    return principais_opcoes_vozes_edge(cog, current)


def _top_gtts_language_options(cog: "TTSVoice", current: str = "") -> list[discord.SelectOption]:
    return principais_opcoes_idiomas_gtts(cog, current)


def _normalize_atts_locale(value: object, default: str = "pt-BR") -> str:
    return normalizar_localidade_atts(value, default)


def _normalize_atts_factor(value: object, default: str = "1.0") -> str | None:
    return normalizar_fator_atts(value, default)


def _normalize_atts_custom_factor(value: object) -> str | None:
    return normalizar_fator_personalizado_atts(value)


def _atts_modal_radio_default(current: str, presets: set[str]) -> str:
    return padrao_radio_modal_atts(current, presets)


def _parse_atts_custom_values(value: object, *, default_rate: str = "1.0", default_pitch: str = "1.0") -> tuple[str, str]:
    return separar_valores_personalizados_atts(
        value,
        taxa_padrao=default_rate,
        tom_padrao=default_pitch,
    )



def _atts_locale_matches(voice_locale: str, language: str) -> bool:
    return localidade_corresponde_idioma_atts(voice_locale, language)


def _atts_voice_locale_from_name(name: str) -> str:
    return localidade_da_voz_atts(name)


def _atts_common_language_options(current: str = "pt-BR", voices: list[dict[str, object]] | None = None) -> list[discord.SelectOption]:
    return opcoes_idiomas_atts(current, voices)


def _atts_voice_cache_key(locale: str = "") -> str:
    return chave_cache_catalogo_vozes_atts(locale)


def _fetch_atts_voice_catalog_sync(locale: str = "", *, limit: int = 500, timeout: float = 2.2, use_cache: bool = True) -> list[dict[str, object]]:
    return buscar_catalogo_vozes_atts_sincrono(
        locale,
        limite=limit,
        timeout=timeout,
        usar_cache=use_cache,
    )


def _atts_voice_score(voice: dict[str, object], language: str) -> int:
    return pontuar_voz_atts(voice, language)


def _atts_matching_voices_for_language(catalog: list[dict[str, object]] | None, language: str) -> list[dict[str, object]]:
    return vozes_atts_por_idioma(catalog, language)


def _atts_voice_options_for_language(cog: "TTSVoice", *, language: str, current: str = "", catalog: list[dict[str, object]] | None = None) -> list[discord.SelectOption]:
    return opcoes_vozes_atts_por_idioma(
        cog,
        idioma=language,
        atual=current,
        catalogo=catalog,
        buscar_catalogo=_fetch_atts_voice_catalog_sync,
    )


def _atts_voice_matches_language(voice: str, language: str) -> bool:
    return voz_atts_corresponde_idioma(
        voice,
        language,
        buscar_catalogo=_fetch_atts_voice_catalog_sync,
    )


def _pick_atts_voice_for_language(cog: "TTSVoice", language: str, current: str = "") -> str:
    # Mantém a chamada pela fachada legada para preservar pontos de patch.
    options = _atts_voice_options_for_language(cog, language=language, current=current)
    return primeira_voz_atts_por_idioma(options)


def _atts_modal_current_language(cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None) -> str:
    return idioma_atual_modal_atts(
        cog,
        panel_message,
        servidor=server,
        id_usuario_alvo=target_user_id,
    )


def _atts_catalog_ready_for_language(catalog: list[dict[str, object]] | None, language: str) -> bool:
    return catalogo_atts_pronto_para_idioma(catalog, language)


async def _load_atts_voice_catalog_for_modal(language: str) -> list[dict[str, object]]:
    return await carregar_catalogo_vozes_atts_para_modal(language)


async def _send_minimal_atts_unavailable(interaction: discord.Interaction) -> None:
    await enviar_indisponibilidade_atts_minima(interaction)


async def _send_atts_settings_modal(
    interaction: discord.Interaction,
    cog: "TTSVoice",
    panel_message: discord.Message | None,
    *,
    server: bool,
    target_user_id: int | None = None,
    target_user_name: str | None = None,
    context: str = "atts",
) -> None:
    await enviar_modal_configuracao_atts(
        interaction,
        cog,
        panel_message,
        servidor=server,
        id_usuario_alvo=target_user_id,
        nome_usuario_alvo=target_user_name,
        contexto=context,
    )


AndroidSettingsModal = ModalConfiguracaoATTS




class TTSPublicLauncherButton(BotaoLancadorPublicoTTS):
    """Fachada legada do botão do launcher público."""

    def __init__(self, *, action: str, label: str, emoji: str | None = None):
        super().__init__(acao=action, rotulo=label, emoji=emoji)


class PrefixTargetSelect(SeletorAlvoPrefixo):
    """Fachada legada que preserva os pontos de patch dos modais de prefixo."""

    def __init__(self, cog: "TTSVoice"):
        super().__init__(
            cog,
            modal_bot=BotPrefixModal,
            modal_atts=ATTSPrefixModal,
            modal_teto=TetoPrefixModal,
            modal_gtts=GTTSPrefixModal,
            modal_edge=EdgePrefixModal,
        )


class TTSMainPanelSelect(SeletorPainelPrincipalTTS):
    """Fachada legada do seletor principal do painel."""

    def __init__(self, *, server: bool):
        super().__init__(servidor=server)


class TTSModeActionSelect(SeletorAcaoModoTTS):
    """Fachada legada que mantém `_send_atts_settings_modal` interceptável."""

    def __init__(self, mode: str):
        super().__init__(mode, abrir_modal_atts=_send_atts_settings_modal)


class TTSPublicLauncherView(VisaoLancadorPublicoTTS):
    """Fachada legada do launcher público."""

    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, timeout: float = 300):
        super().__init__(
            cog,
            owner_id,
            guild_id,
            duracao=timeout,
            classe_botao=TTSPublicLauncherButton,
            classe_modal_edge=EdgeSettingsModal,
            classe_modal_gtts=GTTSSettingsModal,
            classe_modal_apelido=SpokenNameModal,
            enviar_modal_fallback=_send_settings_modal_with_fallback,
            descricao_lancador=TTS_LAUNCHER_DESCRIPTION,
        )


class TTSModeActionsView(VisaoAcoesModoTTS):
    """Fachada legada da visão de ações específicas de cada modo."""

    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        mode: str,
        server: bool,
        source_panel_message: discord.Message | None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(
            cog,
            owner_id,
            guild_id,
            modo=mode,
            servidor=server,
            mensagem_painel_origem=source_panel_message,
            id_usuario_alvo=target_user_id,
            nome_usuario_alvo=target_user_name,
        )

    # As fábricas usam os símbolos legados deste módulo em tempo de execução.
    # Isso preserva pontos históricos de monkeypatch sem duplicar a lógica.
    def _criar_seletor_acao_modo(self):
        return TTSModeActionSelect(self.mode)

    def _criar_seletor_regiao_voz(self):
        return VoiceRegionSelect(self.cog, server=self.server)

    def _criar_visao_selecao_voz(self, interaction: discord.Interaction):
        return _SimpleSelectView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Voz Edge",
            "Muda a voz usada pelo modo Edge.",
            self._criar_seletor_regiao_voz(),
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )

    def _criar_visao_leitura(self, interaction: discord.Interaction):
        return TTSReadingQuickView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            server=self.server,
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )

    def _criar_visao_ajuda_idioma(self, interaction: discord.Interaction):
        return LanguageHelpView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            server=self.server,
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )







class TTSMainPanelView(VisaoPainelPrincipalTTS):
    """Fachada legada do painel principal pessoal/servidor."""

    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, server: bool = False, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__(
            cog,
            owner_id,
            guild_id,
            servidor=server,
            duracao=timeout,
            id_usuario_alvo=target_user_id,
            nome_usuario_alvo=target_user_name,
            abrir_modal_atts=_send_atts_settings_modal,
            enviar_modal_fallback=_send_settings_modal_with_fallback,
            classe_modal_edge=EdgeSettingsModal,
            classe_modal_gtts=GTTSSettingsModal,
            classe_modal_apelido=SpokenNameModal,
            classe_modal_prefixos=ServerPrefixesModal,
            classe_modal_regras=TTSServerRulesModal,
            classe_visao_selecao=_SimpleSelectView,
            classe_seletor_regiao_voz=VoiceRegionSelect,
            classe_visao_leitura=TTSReadingQuickView,
            renderizar_painel=build_settings_panel_text_from_embed,
        )


