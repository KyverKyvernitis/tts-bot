from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "visao_acoes_avancadas.py"


class TTSInterfaceAcoesAvancadasEstruturaTests(unittest.TestCase):
    def test_implementacao_canonicamente_em_portugues_foi_extraida(self):
        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        modulo_tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        classes_ui = {n.name for n in ui_tree.body if isinstance(n, ast.ClassDef)}
        classes_modulo = {n.name for n in modulo_tree.body if isinstance(n, ast.ClassDef)}
        self.assertIn("VisaoAcoesAvancadasTTS", classes_modulo)
        self.assertNotIn("TTSAdvancedActionsView", classes_ui)

    def test_ui_preserva_nome_legado_por_alias_de_importacao(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        encontrados = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module != "interface.visao_acoes_avancadas":
                continue
            for alias in node.names:
                encontrados[alias.asname or alias.name] = alias.name
        self.assertEqual(encontrados, {"TTSAdvancedActionsView": "VisaoAcoesAvancadasTTS"})

    def test_modulo_novo_nao_depende_de_audio_worker_termux_apk_ou_rede(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "streaming", "aiohttp", "requests"):
            self.assertNotIn(proibido, joined)

    def test_assinatura_canonicamente_preserva_contrato_do_construtor(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        classe = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VisaoAcoesAvancadasTTS")
        init = next(n for n in classe.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        posicionais = [a.arg for a in init.args.args]
        kwonly = [a.arg for a in init.args.kwonlyargs]
        self.assertEqual(posicionais, ["self", "cog", "owner_id", "guild_id"])
        self.assertEqual(kwonly, ["server", "source_panel_message", "target_user_id", "target_user_name"])


class _ButtonStyle:
    secondary = object()


class _Button:
    pass


class _Color:
    @staticmethod
    def blurple():
        return "blurple"


class _Embed:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _BaseView:
    def __init__(self, cog, owner_id, guild_id, *, timeout=180, target_user_id=None, target_user_name=None):
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.timeout = timeout
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.removidos = []

    def remove_item(self, item):
        self.removidos.append(item)


class _SimpleView:
    ultimo = None

    def __init__(self, cog, owner_id, guild_id, title, description, select, *, source_panel_message=None, target_user_id=None, target_user_name=None):
        self.args = (cog, owner_id, guild_id, title, description, select)
        self.source_panel_message = source_panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.send = AsyncMock()
        type(self).ultimo = self


class _LanguageHelpView:
    ultimo = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        type(self).ultimo = self


class _IgnoreRoleView:
    ultimo = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.send = AsyncMock()
        type(self).ultimo = self


class _ModeSelect:
    ultimo = None

    def __init__(self, cog, *, server):
        self.cog = cog
        self.server = server
        type(self).ultimo = self


class _SpokenNameModal:
    ultimo = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        type(self).ultimo = self


def _button_decorator(**kwargs):
    def decorator(func):
        func.__button_metadata__ = kwargs
        return func
    return decorator


def _carregar_modulo():
    discord = types.ModuleType("discord")
    discord.Message = type("Message", (), {})
    discord.Interaction = type("Interaction", (), {})
    discord.Embed = _Embed
    discord.Color = _Color
    discord.ButtonStyle = _ButtonStyle
    discord.ui = types.SimpleNamespace(button=_button_decorator, Button=_Button)

    visoes_base = types.ModuleType("cogs.tts.interface.visoes_base")
    visoes_base.VisaoBaseTTS = _BaseView
    visoes_base.VisaoSelecaoSimples = _SimpleView

    modais_simples = types.ModuleType("cogs.tts.interface.modais_simples")
    modais_simples.ModalApelidoFalado = _SpokenNameModal
    modais_simples.VisaoAjudaIdioma = _LanguageHelpView

    seletores = types.ModuleType("cogs.tts.interface.seletores_basicos")
    seletores.SeletorModo = _ModeSelect

    auxiliares = types.ModuleType("cogs.tts.interface.visoes_auxiliares")
    auxiliares.VisaoConfiguracaoCargoIgnorado = _IgnoreRoleView

    nome = "cogs.tts.interface._visao_acoes_avancadas_teste"
    spec = importlib.util.spec_from_file_location(nome, MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "discord": discord,
            "cogs.tts.interface.visoes_base": visoes_base,
            "cogs.tts.interface.modais_simples": modais_simples,
            "cogs.tts.interface.seletores_basicos": seletores,
            "cogs.tts.interface.visoes_auxiliares": auxiliares,
            nome: module,
        },
    ):
        spec.loader.exec_module(module)
    return module


class TTSInterfaceAcoesAvancadasComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _SimpleView.ultimo = None
        _LanguageHelpView.ultimo = None
        _IgnoreRoleView.ultimo = None
        _ModeSelect.ultimo = None
        _SpokenNameModal.ultimo = None

    def test_configuracao_remove_controles_corretos_por_escopo(self):
        modulo = _carregar_modulo()
        cog = types.SimpleNamespace()
        user_view = modulo.VisaoAcoesAvancadasTTS(cog, 10, 20, server=False, source_panel_message="msg")
        server_view = modulo.VisaoAcoesAvancadasTTS(cog, 10, 20, server=True, source_panel_message="msg")

        self.assertEqual(user_view.panel_kind, "user")
        self.assertEqual(server_view.panel_kind, "server")
        self.assertEqual(len(user_view.removidos), 2)
        self.assertEqual(len(server_view.removidos), 1)

    async def test_idioma_gtts_preserva_owner_contexto_e_resposta_ephemeral(self):
        modulo = _carregar_modulo()
        view = modulo.VisaoAcoesAvancadasTTS(types.SimpleNamespace(), 0, 20, server=False, source_panel_message="orig", target_user_id=30, target_user_name="Alvo")
        response = types.SimpleNamespace(send_message=AsyncMock())
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=99), response=response)

        await view.gtts_language_button(interaction, _Button())

        criada = _LanguageHelpView.ultimo
        self.assertEqual(criada.args[:3], (view.cog, 99, 20))
        self.assertEqual(criada.kwargs["source_panel_message"], "orig")
        self.assertEqual(criada.kwargs["target_user_id"], 30)
        self.assertEqual(criada.kwargs["target_user_name"], "Alvo")
        kwargs = response.send_message.await_args.kwargs
        self.assertIs(kwargs["view"], criada)
        self.assertTrue(kwargs["ephemeral"])

    async def test_cargo_ignorado_preserva_owner_e_mensagem_de_origem(self):
        modulo = _carregar_modulo()
        view = modulo.VisaoAcoesAvancadasTTS(types.SimpleNamespace(), 15, 20, server=True, source_panel_message="orig")
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=99))

        await view.ignored_role_button(interaction, _Button())

        criada = _IgnoreRoleView.ultimo
        self.assertEqual(criada.args[:3], (view.cog, 15, 20))
        self.assertEqual(criada.kwargs["source_panel_message"], "orig")
        criada.send.assert_awaited_once_with(interaction)

    async def test_modo_preserva_server_e_contexto_do_usuario_alvo(self):
        modulo = _carregar_modulo()
        view = modulo.VisaoAcoesAvancadasTTS(types.SimpleNamespace(), 0, 20, server=False, source_panel_message="orig", target_user_id=30, target_user_name="Alvo")
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=99))

        await view.mode_button(interaction, _Button())

        seletor = _ModeSelect.ultimo
        criada = _SimpleView.ultimo
        self.assertFalse(seletor.server)
        self.assertEqual(criada.args[1:3], (99, 20))
        self.assertIs(criada.args[5], seletor)
        self.assertEqual(criada.source_panel_message, "orig")
        self.assertEqual(criada.target_user_id, 30)
        self.assertEqual(criada.target_user_name, "Alvo")
        criada.send.assert_awaited_once_with(interaction)

    async def test_apelido_preserva_alvo_e_valor_atual(self):
        modulo = _carregar_modulo()
        cog = types.SimpleNamespace(_get_saved_spoken_name=MagicMock(return_value="Nome atual"))
        view = modulo.VisaoAcoesAvancadasTTS(cog, 0, 20, server=False, source_panel_message="orig", target_user_id=30, target_user_name="Alvo")
        response = types.SimpleNamespace(send_modal=AsyncMock())
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=99), response=response)

        await view.spoken_name_button(interaction, _Button())

        cog._get_saved_spoken_name.assert_called_once_with(20, 30)
        modal = _SpokenNameModal.ultimo
        self.assertEqual(modal.args, (cog, "orig"))
        self.assertEqual(modal.kwargs["target_user_id"], 30)
        self.assertEqual(modal.kwargs["target_user_name"], "Alvo")
        self.assertEqual(modal.kwargs["current_value"], "Nome atual")
        response.send_modal.assert_awaited_once_with(modal)


if __name__ == "__main__":
    unittest.main()
