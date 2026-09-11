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
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "visao_acoes_modo.py"


class TTSInterfaceAcoesModoEstruturaTests(unittest.TestCase):
    def test_implementacao_canonicamente_em_portugues_foi_extraida(self):
        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        modulo_tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        classes_ui = {n.name for n in ui_tree.body if isinstance(n, ast.ClassDef)}
        classes_modulo = {n.name for n in modulo_tree.body if isinstance(n, ast.ClassDef)}
        self.assertIn("VisaoAcoesModoTTS", classes_modulo)
        self.assertIn("TTSModeActionsView", classes_ui)

        fachada = next(n for n in ui_tree.body if isinstance(n, ast.ClassDef) and n.name == "TTSModeActionsView")
        self.assertEqual([base.id for base in fachada.bases if isinstance(base, ast.Name)], ["VisaoAcoesModoTTS"])

    def test_assinaturas_canonica_e_legada_sao_preservadas(self):
        modulo_tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        classe = next(n for n in modulo_tree.body if isinstance(n, ast.ClassDef) and n.name == "VisaoAcoesModoTTS")
        init = next(n for n in classe.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        self.assertEqual([a.arg for a in init.args.args], ["self", "cog", "owner_id", "guild_id"])
        self.assertEqual(
            [a.arg for a in init.args.kwonlyargs],
            ["modo", "servidor", "mensagem_painel_origem", "id_usuario_alvo", "nome_usuario_alvo"],
        )

        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        fachada = next(n for n in ui_tree.body if isinstance(n, ast.ClassDef) and n.name == "TTSModeActionsView")
        legacy_init = next(n for n in fachada.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        self.assertEqual([a.arg for a in legacy_init.args.args], ["self", "cog", "owner_id", "guild_id"])
        self.assertEqual(
            [a.arg for a in legacy_init.args.kwonlyargs],
            ["mode", "server", "source_panel_message", "target_user_id", "target_user_name"],
        )

    def test_fachada_preserva_pontos_de_patch_historicos(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        fachada = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TTSModeActionsView")
        nomes = {n.id for n in ast.walk(fachada) if isinstance(n, ast.Name)}
        for esperado in (
            "TTSModeActionSelect",
            "VoiceRegionSelect",
            "_SimpleSelectView",
            "TTSReadingQuickView",
            "LanguageHelpView",
        ):
            self.assertIn(esperado, nomes)

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


class _ButtonStyle:
    secondary = object()


class _Button:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.callback = None


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
        self.children = []

    def clear_items(self):
        self.children.clear()

    def add_item(self, item):
        self.children.append(item)


class _ModeActionSelect:
    ultimo = None

    def __init__(self, mode):
        self.mode = mode
        type(self).ultimo = self


class _VoiceRegionSelect:
    ultimo = None

    def __init__(self, cog, *, server):
        self.cog = cog
        self.server = server
        type(self).ultimo = self


class _SimpleView:
    ultimo = None

    def __init__(self, cog, owner_id, guild_id, title, description, select, *, source_panel_message=None, target_user_id=None, target_user_name=None):
        self.args = (cog, owner_id, guild_id, title, description, select)
        self.source_panel_message = source_panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.send = AsyncMock()
        type(self).ultimo = self


class _ReadingView:
    ultimo = None

    def __init__(self, cog, owner_id, guild_id, *, server, source_panel_message, target_user_id=None, target_user_name=None):
        self.args = (cog, owner_id, guild_id)
        self.server = server
        self.source_panel_message = source_panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.send = AsyncMock()
        type(self).ultimo = self


class _LanguageHelpView:
    ultimo = None

    def __init__(self, cog, owner_id, guild_id, *, server=False, source_panel_message=None, target_user_id=None, target_user_name=None, timeout=180):
        self.args = (cog, owner_id, guild_id)
        self.server = server
        self.source_panel_message = source_panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.timeout = timeout
        type(self).ultimo = self


def _carregar_modulo():
    discord = types.ModuleType("discord")
    discord.Message = type("Message", (), {})
    discord.Interaction = type("Interaction", (), {})
    discord.Embed = _Embed
    discord.Color = _Color
    discord.ButtonStyle = _ButtonStyle
    discord.ui = types.SimpleNamespace(Button=_Button, Select=type("Select", (), {}))

    controles = types.ModuleType("cogs.tts.interface.controles_paineis")
    controles.SeletorAcaoModoTTS = _ModeActionSelect

    modais = types.ModuleType("cogs.tts.interface.modais_simples")
    modais.VisaoAjudaIdioma = _LanguageHelpView

    seletores = types.ModuleType("cogs.tts.interface.seletores_basicos")
    seletores.SeletorRegiaoVoz = _VoiceRegionSelect

    auxiliares = types.ModuleType("cogs.tts.interface.visoes_auxiliares")
    auxiliares.VisaoLeituraRapidaTTS = _ReadingView

    base = types.ModuleType("cogs.tts.interface.visoes_base")
    base.VisaoBaseTTS = _BaseView
    base.VisaoSelecaoSimples = _SimpleView

    nome = "cogs.tts.interface._visao_acoes_modo_teste"
    spec = importlib.util.spec_from_file_location(nome, MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "discord": discord,
            "cogs.tts.interface.controles_paineis": controles,
            "cogs.tts.interface.modais_simples": modais,
            "cogs.tts.interface.seletores_basicos": seletores,
            "cogs.tts.interface.visoes_auxiliares": auxiliares,
            "cogs.tts.interface.visoes_base": base,
            nome: module,
        },
    ):
        spec.loader.exec_module(module)
    return module


class TTSInterfaceAcoesModoComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _ModeActionSelect.ultimo = None
        _VoiceRegionSelect.ultimo = None
        _SimpleView.ultimo = None
        _ReadingView.ultimo = None
        _LanguageHelpView.ultimo = None

    def _view(self, modulo, *, mode="edge", server=False, owner=0, target=30, target_name="Alvo"):
        cog = types.SimpleNamespace()
        return modulo.VisaoAcoesModoTTS(
            cog,
            owner,
            20,
            modo=mode,
            servidor=server,
            mensagem_painel_origem="orig",
            id_usuario_alvo=target,
            nome_usuario_alvo=target_name,
        )

    def test_construtor_preserva_atributos_legados_e_monta_controles(self):
        modulo = _carregar_modulo()
        view = self._view(modulo, mode="gtts", server=True, owner=15)
        self.assertEqual((view.modo, view.mode), ("gtts", "gtts"))
        self.assertEqual((view.servidor, view.server), (True, True))
        self.assertEqual((view.mensagem_painel_origem, view.source_panel_message), ("orig", "orig"))
        self.assertEqual(view.panel_kind, "server")
        self.assertEqual(len(view.children), 2)
        self.assertEqual(_ModeActionSelect.ultimo.mode, "gtts")

    def test_titulo_prefixo_permanece_equivalente_para_todos_os_modos(self):
        modulo = _carregar_modulo()
        casos = {
            "atts": ("%", "ATTS"),
            "android_native": ("%", "ATTS"),
            "edge": (",", "Edge"),
            "gtts": (".", "gTTS"),
            "desconhecido": (".", "gTTS"),
        }
        for modo, (prefixo, titulo) in casos.items():
            with self.subTest(modo=modo):
                view = self._view(modulo, mode=modo)
                self.assertEqual(view._prefixo_modo(), prefixo)
                atual_titulo, descricao = view._titulo_descricao_modo()
                self.assertEqual(atual_titulo, titulo)
                self.assertIn(f"`{prefixo}texto`", descricao)

    async def test_abrir_voz_edge_preserva_owner_alvo_e_mensagem_origem(self):
        modulo = _carregar_modulo()
        view = self._view(modulo, owner=0)
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=99))
        await view._abrir_voz_edge(interaction)

        criada = _SimpleView.ultimo
        self.assertEqual(criada.args[1:3], (99, 20))
        self.assertIs(criada.args[5], _VoiceRegionSelect.ultimo)
        self.assertFalse(_VoiceRegionSelect.ultimo.server)
        self.assertEqual(criada.source_panel_message, "orig")
        self.assertEqual(criada.target_user_id, 30)
        self.assertEqual(criada.target_user_name, "Alvo")
        criada.send.assert_awaited_once_with(interaction)

    async def test_abrir_leitura_edge_preserva_contexto(self):
        modulo = _carregar_modulo()
        view = self._view(modulo, server=True, owner=15)
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=99))
        await view._abrir_leitura_edge(interaction)

        criada = _ReadingView.ultimo
        self.assertEqual(criada.args[1:3], (15, 20))
        self.assertTrue(criada.server)
        self.assertEqual(criada.source_panel_message, "orig")
        self.assertEqual(criada.target_user_id, 30)
        self.assertEqual(criada.target_user_name, "Alvo")
        criada.send.assert_awaited_once_with(interaction)

    async def test_abrir_idioma_gtts_preserva_contexto_e_ephemeral(self):
        modulo = _carregar_modulo()
        view = self._view(modulo, owner=0)
        response = types.SimpleNamespace(send_message=AsyncMock())
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=99), response=response)
        await view._abrir_idioma_gtts(interaction)

        criada = _LanguageHelpView.ultimo
        self.assertEqual(criada.args[1:3], (99, 20))
        self.assertEqual(criada.source_panel_message, "orig")
        self.assertEqual(criada.target_user_id, 30)
        self.assertEqual(criada.target_user_name, "Alvo")
        kwargs = response.send_message.await_args.kwargs
        self.assertIs(kwargs["view"], criada)
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(kwargs["embed"].kwargs["title"], "Idioma gTTS")

    async def test_voltar_ao_painel_usuario_preserva_alvo_e_payload(self):
        modulo = _carregar_modulo()
        painel = types.SimpleNamespace(message=None)
        cog = types.SimpleNamespace(
            _member_panel_name=MagicMock(return_value="Fallback"),
            _build_settings_embed=AsyncMock(return_value="embed"),
            _build_panel_view=MagicMock(return_value=painel),
            _prepare_panel_payload=MagicMock(return_value=("conteudo", "embed-edit", "view-edit")),
        )
        view = modulo.VisaoAcoesModoTTS(
            cog,
            0,
            20,
            modo="edge",
            servidor=False,
            mensagem_painel_origem="orig",
            id_usuario_alvo=30,
            nome_usuario_alvo="Alvo",
        )
        response = types.SimpleNamespace(edit_message=AsyncMock())
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=20),
            user=types.SimpleNamespace(id=99),
            response=response,
            message="mensagem-interacao",
        )

        await view._voltar_painel_principal(interaction)

        cog._build_settings_embed.assert_awaited_once_with(
            20,
            30,
            server=False,
            panel_kind="user",
            target_user_name="Alvo",
            viewer_user_id=99,
        )
        cog._build_panel_view.assert_called_once_with(
            99,
            20,
            server=False,
            target_user_id=30,
            target_user_name="Alvo",
        )
        response.edit_message.assert_awaited_once_with(content="conteudo", embed="embed-edit", view="view-edit")
        self.assertEqual(painel.message, "mensagem-interacao")

    async def test_enviar_preserva_caminhos_response_e_followup(self):
        modulo = _carregar_modulo()
        cog = types.SimpleNamespace(_make_embed=MagicMock(return_value="embed"))
        for concluida in (False, True):
            with self.subTest(response_ja_concluida=concluida):
                view = modulo.VisaoAcoesModoTTS(
                    cog,
                    10,
                    20,
                    modo="edge",
                    servidor=False,
                    mensagem_painel_origem=None,
                )
                response = types.SimpleNamespace(is_done=MagicMock(return_value=concluida), send_message=AsyncMock())
                followup = types.SimpleNamespace(send=AsyncMock())
                interaction = types.SimpleNamespace(response=response, followup=followup)
                await view.enviar(interaction)
                if concluida:
                    followup.send.assert_awaited_once_with(embed="embed", view=view, ephemeral=True, wait=True)
                    response.send_message.assert_not_awaited()
                else:
                    response.send_message.assert_awaited_once_with(embed="embed", view=view, ephemeral=True)
                    followup.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
