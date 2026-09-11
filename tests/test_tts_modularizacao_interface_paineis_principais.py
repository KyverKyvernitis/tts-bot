from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
LANCADOR_PATH = ROOT / "cogs" / "tts" / "interface" / "visao_lancador_publico.py"
PAINEL_PATH = ROOT / "cogs" / "tts" / "interface" / "visao_painel_principal.py"


class TTSInterfacePaineisPrincipaisEstruturaTests(unittest.TestCase):
    def test_duas_implementacoes_canonicas_foram_extraidas_em_portugues(self):
        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        lancador_tree = ast.parse(LANCADOR_PATH.read_text(encoding="utf-8"))
        painel_tree = ast.parse(PAINEL_PATH.read_text(encoding="utf-8"))

        self.assertIn("VisaoLancadorPublicoTTS", {n.name for n in lancador_tree.body if isinstance(n, ast.ClassDef)})
        self.assertIn("VisaoPainelPrincipalTTS", {n.name for n in painel_tree.body if isinstance(n, ast.ClassDef)})

        classes_ui = {n.name: n for n in ui_tree.body if isinstance(n, ast.ClassDef)}
        self.assertEqual([b.id for b in classes_ui["TTSPublicLauncherView"].bases if isinstance(b, ast.Name)], ["VisaoLancadorPublicoTTS"])
        self.assertEqual([b.id for b in classes_ui["TTSMainPanelView"].bases if isinstance(b, ast.Name)], ["VisaoPainelPrincipalTTS"])

    def test_assinaturas_legadas_externas_continuam_exatas(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

        launcher_init = next(n for n in classes["TTSPublicLauncherView"].body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        self.assertEqual([a.arg for a in launcher_init.args.args], ["self", "cog", "owner_id", "guild_id"])
        self.assertEqual([a.arg for a in launcher_init.args.kwonlyargs], ["timeout"])

        panel_init = next(n for n in classes["TTSMainPanelView"].body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        self.assertEqual([a.arg for a in panel_init.args.args], ["self", "cog", "owner_id", "guild_id"])
        self.assertEqual(
            [a.arg for a in panel_init.args.kwonlyargs],
            ["server", "timeout", "target_user_id", "target_user_name"],
        )

    def test_fachadas_injetam_os_pontos_de_patch_historicos(self):
        text = UI_PATH.read_text(encoding="utf-8")
        tree = ast.parse(text)
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        launcher = ast.get_source_segment(text, classes["TTSPublicLauncherView"]) or ""
        painel = ast.get_source_segment(text, classes["TTSMainPanelView"]) or ""

        for esperado in (
            "TTSPublicLauncherButton",
            "EdgeSettingsModal",
            "GTTSSettingsModal",
            "SpokenNameModal",
            "_send_settings_modal_with_fallback",
        ):
            self.assertIn(esperado, launcher)
        for esperado in (
            "_send_atts_settings_modal",
            "_send_settings_modal_with_fallback",
            "EdgeSettingsModal",
            "GTTSSettingsModal",
            "SpokenNameModal",
            "ServerPrefixesModal",
            "TTSServerRulesModal",
            "_SimpleSelectView",
            "VoiceRegionSelect",
            "TTSReadingQuickView",
            "build_settings_panel_text_from_embed",
        ):
            self.assertIn(esperado, painel)

    def test_modulos_novos_nao_importam_audio_worker_termux_apk_ou_rede(self):
        for caminho in (LANCADOR_PATH, PAINEL_PATH):
            with self.subTest(caminho=caminho.name):
                tree = ast.parse(caminho.read_text(encoding="utf-8"))
                imports: list[str] = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        imports.append(node.module or "")
                joined = "\n".join(imports).lower()
                for proibido in ("audio", "worker", "termux", "android", "streaming", "aiohttp", "requests"):
                    self.assertNotIn(proibido, joined)

    def test_ui_ficou_apenas_com_fachadas_das_duas_views_grandes(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        self.assertLessEqual(classes["TTSPublicLauncherView"].end_lineno - classes["TTSPublicLauncherView"].lineno + 1, 20)
        self.assertLessEqual(classes["TTSMainPanelView"].end_lineno - classes["TTSMainPanelView"].lineno + 1, 30)


class _ButtonStyle:
    secondary = object()


class _Button:
    def __init__(self, *, label=None, emoji=None, style=None, **kwargs):
        self.label = label
        self.emoji = emoji
        self.style = style
        self.callback = None
        self.view = None


class _Modal:
    pass


class _BaseLayout:
    def __init__(self, cog, owner_id, guild_id, *, timeout=180, target_user_id=None, target_user_name=None):
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.timeout = timeout
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.children = []
        self.message = None

    def clear_items(self):
        self.children.clear()

    def add_item(self, item):
        item.view = self
        self.children.append(item)


class _LauncherButton(_Button):
    def __init__(self, *, acao, rotulo, emoji=None):
        super().__init__(label=rotulo, emoji=emoji, style=_ButtonStyle.secondary)
        self.acao = acao
        self.action = acao


class _SimpleSelectView:
    ultimo = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.send = AsyncMock()
        type(self).ultimo = self


class _VoiceRegionSelect:
    def __init__(self, cog, *, server):
        self.cog = cog
        self.server = server


class _ReadingView:
    ultimo = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.send = AsyncMock()
        type(self).ultimo = self


class _ModalCapture:
    ultimo = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        type(self).ultimo = self


class _Color:
    @staticmethod
    def blurple():
        return "blurple"


def _discord_stub():
    discord = types.ModuleType("discord")
    discord.Interaction = type("Interaction", (), {})
    discord.Message = type("Message", (), {})
    discord.Embed = type("Embed", (), {})
    discord.Color = _Color
    discord.ButtonStyle = _ButtonStyle
    discord.ui = types.SimpleNamespace(Button=_Button, Modal=_Modal, Select=type("Select", (), {}))
    return discord


def _carregar_lancador():
    discord = _discord_stub()
    config = types.ModuleType("config")
    config.EDGE_TTS_VOICE = "pt-BR-FranciscaNeural"

    embed = types.ModuleType("cogs.tts.utils.embed")
    embed.human_language_name = lambda value: {"en": "Inglês", "pt-br": "Português"}.get(str(value), str(value))
    embed.human_voice_name = lambda value: f"humana:{value}"

    controles = types.ModuleType("cogs.tts.interface.controles_paineis")
    controles.BotaoLancadorPublicoTTS = _LauncherButton
    simples = types.ModuleType("cogs.tts.interface.modais_simples")
    simples.ModalApelidoFalado = _ModalCapture
    online = types.ModuleType("cogs.tts.interface.modais_vozes_online")
    online.ModalConfiguracaoEdge = _ModalCapture
    online.ModalConfiguracaoGTTS = _ModalCapture
    operacoes = types.ModuleType("cogs.tts.interface.operacoes_painel")
    operacoes.DESCRICAO_LANCADOR_TTS = "Descrição"
    operacoes.enviar_modal_configuracao_com_fallback = AsyncMock()
    layout = types.ModuleType("cogs.tts.interface.visoes_layout")
    layout.VisaoLayoutBaseTTS = _BaseLayout

    nome = "cogs.tts.interface._visao_lancador_publico_teste"
    spec = importlib.util.spec_from_file_location(nome, LANCADOR_PATH)
    modulo = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(sys.modules, {
        "discord": discord,
        "config": config,
        "cogs.tts.utils.embed": embed,
        "cogs.tts.interface.controles_paineis": controles,
        "cogs.tts.interface.modais_simples": simples,
        "cogs.tts.interface.modais_vozes_online": online,
        "cogs.tts.interface.operacoes_painel": operacoes,
        "cogs.tts.interface.visoes_layout": layout,
        nome: modulo,
    }):
        spec.loader.exec_module(modulo)
    return modulo


def _carregar_painel():
    discord = _discord_stub()
    embed = types.ModuleType("cogs.tts.utils.embed")
    embed.build_settings_panel_text_from_embed = lambda panel, *, server: f"render:{server}:{panel}"
    atts = types.ModuleType("cogs.tts.interface.modais_atts")
    atts.enviar_modal_configuracao_atts = AsyncMock()
    servidor = types.ModuleType("cogs.tts.interface.modais_servidor")
    servidor.ModalPrefixosServidor = _ModalCapture
    servidor.ModalRegrasServidorTTS = _ModalCapture
    simples = types.ModuleType("cogs.tts.interface.modais_simples")
    simples.ModalApelidoFalado = _ModalCapture
    online = types.ModuleType("cogs.tts.interface.modais_vozes_online")
    online.ModalConfiguracaoEdge = _ModalCapture
    online.ModalConfiguracaoGTTS = _ModalCapture
    operacoes = types.ModuleType("cogs.tts.interface.operacoes_painel")
    operacoes.enviar_modal_configuracao_com_fallback = AsyncMock()
    seletores = types.ModuleType("cogs.tts.interface.seletores_basicos")
    seletores.SeletorRegiaoVoz = _VoiceRegionSelect
    auxiliares = types.ModuleType("cogs.tts.interface.visoes_auxiliares")
    auxiliares.VisaoLeituraRapidaTTS = _ReadingView
    base = types.ModuleType("cogs.tts.interface.visoes_base")
    base.VisaoSelecaoSimples = _SimpleSelectView
    layout = types.ModuleType("cogs.tts.interface.visoes_layout")
    layout.VisaoLayoutBaseTTS = _BaseLayout

    nome = "cogs.tts.interface._visao_painel_principal_teste"
    spec = importlib.util.spec_from_file_location(nome, PAINEL_PATH)
    modulo = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(sys.modules, {
        "discord": discord,
        "cogs.tts.utils.embed": embed,
        "cogs.tts.interface.modais_atts": atts,
        "cogs.tts.interface.modais_servidor": servidor,
        "cogs.tts.interface.modais_simples": simples,
        "cogs.tts.interface.modais_vozes_online": online,
        "cogs.tts.interface.operacoes_painel": operacoes,
        "cogs.tts.interface.seletores_basicos": seletores,
        "cogs.tts.interface.visoes_auxiliares": auxiliares,
        "cogs.tts.interface.visoes_base": base,
        "cogs.tts.interface.visoes_layout": layout,
        nome: modulo,
    }):
        spec.loader.exec_module(modulo)
    return modulo


class TTSInterfaceLancadorComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _ModalCapture.ultimo = None

    def _cog(self, *, defaults=None, user=None):
        db = types.SimpleNamespace(
            get_guild_tts_defaults=lambda guild_id: defaults or {},
            get_user_tts=lambda guild_id, user_id: user or {},
        )
        return types.SimpleNamespace(
            _get_db=lambda: db,
            _normalize_rate_value=lambda value: value,
            _normalize_pitch_value=lambda value: value,
            _member_panel_name=lambda member: f"nome-{member.id}",
            _get_saved_spoken_name=lambda guild_id, user_id: "apelido",
            _make_embed=lambda *a, **k: (a, k),
        )

    def test_resumo_preserva_diferencas_reais_de_edge_e_gtts(self):
        modulo = _carregar_lancador()
        view = modulo.VisaoLancadorPublicoTTS(
            self._cog(
                defaults={"voice": "padrao", "rate": "+0%", "pitch": "+0Hz", "language": "pt-br"},
                user={"voice": "outra", "rate": "+10%", "pitch": "+5Hz", "language": "en"},
            ),
            10,
            20,
        )
        self.assertEqual(view._resumo_edge(), "Voz: `humana:outra` · Velocidade: `+10%` · Tom: `+5Hz`")
        self.assertEqual(view._resumo_gtts(), "Idioma: `Inglês`")
        self.assertEqual([b.label for b in view.children], ["Configurar Edge", "Configurar gTTS"])

    async def test_acao_edge_preserva_contexto_e_fallback_textual(self):
        modulo = _carregar_lancador()
        enviar = AsyncMock()
        view = modulo.VisaoLancadorPublicoTTS(self._cog(), 10, 20, enviar_modal_fallback=enviar)
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=20),
            user=types.SimpleNamespace(id=30),
            message="painel",
            response=types.SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()),
        )
        await view._abrir_acao(interaction, "edge")
        enviar.assert_awaited_once()
        args = enviar.await_args.args
        self.assertIs(args[0], interaction)
        normal = args[1]()
        fallback = args[2]()
        self.assertEqual(normal.args[1], "painel")
        self.assertEqual(normal.kwargs["target_user_id"], 30)
        self.assertNotIn("force_text_fallback", normal.kwargs)
        self.assertTrue(fallback.kwargs["force_text_fallback"])
        self.assertEqual(enviar.await_args.kwargs["context"], "public-edge")

    async def test_acao_apelido_preserva_usuario_alvo(self):
        modulo = _carregar_lancador()
        view = modulo.VisaoLancadorPublicoTTS(
            self._cog(defaults={"announce_author": True}), 10, 20
        )
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=20),
            user=types.SimpleNamespace(id=30),
            message="painel",
            response=types.SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()),
        )
        await view._abrir_acao(interaction, "spoken_name")
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertEqual(modal.kwargs["target_user_id"], 30)
        self.assertEqual(modal.kwargs["target_user_name"], "nome-30")
        self.assertEqual(modal.kwargs["current_value"], "apelido")


class TTSInterfacePainelPrincipalComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _ModalCapture.ultimo = None
        _SimpleSelectView.ultimo = None
        _ReadingView.ultimo = None

    def _cog(self, *, announce=False):
        db = types.SimpleNamespace(get_guild_tts_defaults=lambda guild_id: {"announce_author": announce})
        return types.SimpleNamespace(
            _get_db=lambda: db,
            _member_panel_name=lambda member: f"nome-{member.id}",
            _get_saved_spoken_name=lambda guild_id, user_id: "apelido",
            _make_embed=lambda *a, **k: (a, k),
        )

    def test_botoes_servidor_e_pessoal_permanecem_equivalentes(self):
        modulo = _carregar_painel()
        servidor = modulo.VisaoPainelPrincipalTTS(self._cog(), 10, 20, servidor=True)
        self.assertEqual([b.label for b in servidor.children], ["Configurar prefixos", "Configurar Edge", "Configurar gTTS", "Configurar regras"])
        pessoal = modulo.VisaoPainelPrincipalTTS(self._cog(announce=True), 10, 20, servidor=False)
        self.assertEqual([b.label for b in pessoal.children], ["Configurar Edge", "Configurar gTTS", "Alterar apelido"])

    async def test_atts_preserva_usuario_alvo_automatico_no_painel_pessoal(self):
        modulo = _carregar_painel()
        abrir_atts = AsyncMock()
        view = modulo.VisaoPainelPrincipalTTS(self._cog(), 0, 20, servidor=False, abrir_modal_atts=abrir_atts)
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=20),
            user=types.SimpleNamespace(id=30),
            message="painel",
            response=types.SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()),
        )
        await view._abrir_painel_modo(interaction, "atts")
        abrir_atts.assert_awaited_once_with(
            interaction,
            view.cog,
            "painel",
            server=False,
            target_user_id=30,
            target_user_name="nome-30",
            context="panel-atts",
        )

    async def test_prefixos_e_regras_continuam_restritos_ao_servidor(self):
        modulo = _carregar_painel()
        pessoal = modulo.VisaoPainelPrincipalTTS(self._cog(), 10, 20, servidor=False)
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=20),
            user=types.SimpleNamespace(id=30),
            message="painel",
            response=types.SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()),
        )
        await pessoal._abrir_painel_prefixos(interaction)
        interaction.response.send_message.assert_awaited_once()
        interaction.response.send_modal.assert_not_awaited()

        enviar = AsyncMock()
        servidor = modulo.VisaoPainelPrincipalTTS(self._cog(), 10, 20, servidor=True, enviar_modal_fallback=enviar)
        await servidor._abrir_painel_regras(interaction)
        enviar.assert_awaited_once()
        self.assertEqual(enviar.await_args.kwargs["context"], "server-rules")


if __name__ == "__main__":
    unittest.main()
