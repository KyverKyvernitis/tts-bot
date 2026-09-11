from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "controles_paineis.py"

CLASSES_CANONICAS = {
    "BotaoLancadorPublicoTTS",
    "SeletorAlvoPrefixo",
    "SeletorPainelPrincipalTTS",
    "SeletorAcaoModoTTS",
}

FACHADAS_LEGADAS = {
    "TTSPublicLauncherButton": "BotaoLancadorPublicoTTS",
    "PrefixTargetSelect": "SeletorAlvoPrefixo",
    "TTSMainPanelSelect": "SeletorPainelPrincipalTTS",
    "TTSModeActionSelect": "SeletorAcaoModoTTS",
}


class TTSInterfaceControlesPaineisEstruturaTests(unittest.TestCase):
    def test_implementacoes_canonicas_ficam_no_modulo_em_portugues(self):
        modulo_tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        classes_modulo = {node.name for node in modulo_tree.body if isinstance(node, ast.ClassDef)}
        self.assertTrue(CLASSES_CANONICAS <= classes_modulo)

    def test_ui_mantem_apenas_fachadas_legadas_finas(self):
        ui_text = UI_PATH.read_text(encoding="utf-8")
        ui_tree = ast.parse(ui_text)
        classes = {node.name: node for node in ui_tree.body if isinstance(node, ast.ClassDef)}
        for legado, canonico in FACHADAS_LEGADAS.items():
            with self.subTest(legado=legado):
                self.assertIn(legado, classes)
                node = classes[legado]
                self.assertEqual(len(node.bases), 1)
                self.assertIsInstance(node.bases[0], ast.Name)
                self.assertEqual(node.bases[0].id, canonico)
                source = ast.get_source_segment(ui_text, node) or ""
                self.assertLessEqual(len(source.splitlines()), 18)

    def test_fachadas_preservam_assinaturas_legadas_e_pontos_de_patch(self):
        ui_text = UI_PATH.read_text(encoding="utf-8")
        ui_tree = ast.parse(ui_text)
        esperadas = {
            "TTSPublicLauncherButton": ["self", "action", "label", "emoji"],
            "PrefixTargetSelect": ["self", "cog"],
            "TTSMainPanelSelect": ["self", "server"],
            "TTSModeActionSelect": ["self", "mode"],
        }
        classes = {node.name: node for node in ui_tree.body if isinstance(node, ast.ClassDef)}
        for nome, parametros in esperadas.items():
            with self.subTest(nome=nome):
                init = next(
                    node for node in classes[nome].body
                    if isinstance(node, ast.FunctionDef) and node.name == "__init__"
                )
                encontrados = [arg.arg for arg in (init.args.posonlyargs + init.args.args + init.args.kwonlyargs)]
                self.assertEqual(encontrados, parametros)
        self.assertIn("modal_bot=BotPrefixModal", ui_text)
        self.assertIn("abrir_modal_atts=_send_atts_settings_modal", ui_text)

    def test_modulo_novo_nao_importa_audio_worker_termux_apk_streaming_ou_routing(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "streaming", "routing"):
            self.assertNotIn(proibido, joined)


class _SelectOption:
    def __init__(self, *, label, description=None, value=None, emoji=None, default=False):
        self.label = label
        self.description = description
        self.value = value
        self.emoji = emoji
        self.default = default


class _ButtonStyle:
    secondary = "secondary"


class _Button:
    def __init__(self, *, label=None, emoji=None, style=None, **kwargs):
        self.label = label
        self.emoji = emoji
        self.style = style
        self.view = None


class _Select:
    def __init__(self, *, placeholder=None, min_values=None, max_values=None, options=None, **kwargs):
        self.placeholder = placeholder
        self.min_values = min_values
        self.max_values = max_values
        self.options = list(options or [])
        self.values = []
        self.view = None


class _ModalBase:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _ModalBot(_ModalBase):
    pass


class _ModalATTS(_ModalBase):
    pass


class _ModalTeto(_ModalBase):
    pass


class _ModalGTTS(_ModalBase):
    pass


class _ModalEdge(_ModalBase):
    pass


def _carregar_modulo():
    discord = types.ModuleType("discord")
    discord.Interaction = type("Interaction", (), {})
    discord.Message = type("Message", (), {})
    discord.SelectOption = _SelectOption
    discord.ButtonStyle = _ButtonStyle
    discord.ui = types.SimpleNamespace(Button=_Button, Select=_Select, Modal=_ModalBase)

    modais_atts = types.ModuleType("cogs.tts.interface.modais_atts")
    modais_atts.enviar_modal_configuracao_atts = AsyncMock()

    modais_simples = types.ModuleType("cogs.tts.interface.modais_simples")
    modais_simples.ModalPrefixoBot = _ModalBot
    modais_simples.ModalPrefixoATTS = _ModalATTS
    modais_simples.ModalPrefixoTeto = _ModalTeto
    modais_simples.ModalPrefixoGTTS = _ModalGTTS
    modais_simples.ModalPrefixoEdge = _ModalEdge

    cogs_pkg = types.ModuleType("cogs")
    cogs_pkg.__path__ = []
    tts_pkg = types.ModuleType("cogs.tts")
    tts_pkg.__path__ = []
    interface_pkg = types.ModuleType("cogs.tts.interface")
    interface_pkg.__path__ = []

    spec = importlib.util.spec_from_file_location("cogs.tts.interface.controles_paineis", MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "discord": discord,
            "cogs": cogs_pkg,
            "cogs.tts": tts_pkg,
            "cogs.tts.interface": interface_pkg,
            "cogs.tts.interface.modais_atts": modais_atts,
            "cogs.tts.interface.modais_simples": modais_simples,
        },
    ):
        spec.loader.exec_module(module)
    return module


class TTSInterfaceControlesPaineisComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def test_assinaturas_canonicas_usam_nomes_em_portugues(self):
        modulo = _carregar_modulo()
        esperadas = {
            "BotaoLancadorPublicoTTS": ["self", "acao", "rotulo", "emoji"],
            "SeletorPainelPrincipalTTS": ["self", "servidor"],
            "SeletorAcaoModoTTS": ["self", "modo", "abrir_modal_atts"],
        }
        for nome, parametros in esperadas.items():
            with self.subTest(nome=nome):
                self.assertEqual(list(inspect.signature(getattr(modulo, nome).__init__).parameters), parametros)

    async def test_botao_lancador_preserva_erro_e_delegacao(self):
        modulo = _carregar_modulo()
        botao = modulo.BotaoLancadorPublicoTTS(acao="edge", rotulo="Configurar")
        response = types.SimpleNamespace(send_message=AsyncMock())
        interaction = types.SimpleNamespace(response=response, guild=object())

        await botao.callback(interaction)
        response.send_message.assert_awaited_once_with("Esse painel não está disponível agora.", ephemeral=True)

        response.send_message.reset_mock()
        abrir = AsyncMock()
        botao.view = types.SimpleNamespace(_open_action=abrir)
        await botao.callback(interaction)
        abrir.assert_awaited_once_with(interaction, "edge")
        response.send_message.assert_not_awaited()

    async def test_seletor_prefixo_preserva_modal_e_contexto_do_painel(self):
        modulo = _carregar_modulo()
        response = types.SimpleNamespace(send_modal=AsyncMock())
        interaction = types.SimpleNamespace(
            response=response,
            user=types.SimpleNamespace(id=10),
            guild=types.SimpleNamespace(id=20),
            message="mensagem-interacao",
        )
        painel = types.SimpleNamespace(source_panel_message="painel-origem", owner_id=30, guild_id=40)
        casos = {
            "bot": _ModalBot,
            "atts": _ModalATTS,
            "teto": _ModalTeto,
            "edge": _ModalEdge,
            "gtts": _ModalGTTS,
        }
        for valor, classe_modal in casos.items():
            with self.subTest(valor=valor):
                response.send_modal.reset_mock()
                seletor = modulo.SeletorAlvoPrefixo(object())
                seletor.view = painel
                seletor.values = [valor]
                await seletor.callback(interaction)
                modal = response.send_modal.await_args.args[0]
                self.assertIsInstance(modal, classe_modal)
                self.assertEqual(modal.args[1:], ("painel-origem", 30, 40))

    async def test_seletor_principal_preserva_opcoes_e_rotas(self):
        modulo = _carregar_modulo()
        servidor = modulo.SeletorPainelPrincipalTTS(servidor=True)
        self.assertEqual([o.value for o in servidor.options], ["prefixes", "atts", "edge", "gtts", "rules"])
        pessoal = modulo.SeletorPainelPrincipalTTS(servidor=False)
        self.assertEqual([o.value for o in pessoal.options], ["atts", "edge", "gtts", "spoken_name"])

        rotas = {
            "atts": "_open_atts_panel",
            "edge": "_open_edge_panel",
            "gtts": "_open_gtts_panel",
            "spoken_name": "_open_spoken_name_modal",
            "prefixes": "_open_prefixes_panel",
            "rules": "_open_rules_panel",
        }
        for valor, metodo in rotas.items():
            with self.subTest(valor=valor):
                chamadas = {nome: AsyncMock() for nome in rotas.values()}
                seletor = modulo.SeletorPainelPrincipalTTS(servidor=valor in {"prefixes", "rules"})
                seletor.view = types.SimpleNamespace(**chamadas)
                seletor.values = [valor]
                await seletor.callback(object())
                chamadas[metodo].assert_awaited_once()

    async def test_seletor_acao_modo_preserva_modal_atts_e_rotas(self):
        modulo = _carregar_modulo()
        abrir_atts = AsyncMock()
        seletor = modulo.SeletorAcaoModoTTS("atts", abrir_modal_atts=abrir_atts)
        painel = types.SimpleNamespace(
            cog="cog",
            source_panel_message="painel",
            server=True,
            target_user_id=77,
            target_user_name="Alvo",
            _open_edge_voice=AsyncMock(),
            _open_edge_reading=AsyncMock(),
            _open_gtts_language=AsyncMock(),
        )
        seletor.view = painel
        seletor.values = ["atts_settings"]
        interaction = object()
        await seletor.callback(interaction)
        abrir_atts.assert_awaited_once_with(
            interaction,
            "cog",
            "painel",
            server=True,
            target_user_id=77,
            target_user_name="Alvo",
            context="mode-atts",
        )

        for valor, metodo in [
            ("edge_voice", "_open_edge_voice"),
            ("edge_reading", "_open_edge_reading"),
            ("gtts_language", "_open_gtts_language"),
        ]:
            with self.subTest(valor=valor):
                seletor = modulo.SeletorAcaoModoTTS("edge", abrir_modal_atts=AsyncMock())
                seletor.view = painel
                seletor.values = [valor]
                alvo = getattr(painel, metodo)
                alvo.reset_mock()
                await seletor.callback(interaction)
                alvo.assert_awaited_once_with(interaction)


if __name__ == "__main__":
    unittest.main()
