from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "visoes_auxiliares.py"

CLASSES_CANONICAS = {
    "VisaoConfiguracaoCargoIgnorado",
    "VisaoLeituraRapidaTTS",
    "VisaoStatusTTS",
    "VisaoPainelToggleTTS",
}

ALIASES_LEGADOS = {
    "IgnoreRoleConfigView": "VisaoConfiguracaoCargoIgnorado",
    "TTSReadingQuickView": "VisaoLeituraRapidaTTS",
    "TTSStatusView": "VisaoStatusTTS",
    "TTSTogglePanelView": "VisaoPainelToggleTTS",
}


class TTSInterfaceVisoesAuxiliaresEstruturaTests(unittest.TestCase):
    def test_implementacoes_foram_extraidas_com_nomes_em_portugues(self):
        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        modulo_tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        classes_ui = {node.name for node in ui_tree.body if isinstance(node, ast.ClassDef)}
        classes_modulo = {node.name for node in modulo_tree.body if isinstance(node, ast.ClassDef)}

        self.assertTrue(CLASSES_CANONICAS <= classes_modulo)
        self.assertTrue(set(ALIASES_LEGADOS).isdisjoint(classes_ui))

    def test_ui_preserva_nomes_legados_por_alias_de_importacao(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        encontrados: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module != "interface.visoes_auxiliares":
                continue
            for alias in node.names:
                encontrados[str(alias.asname or alias.name)] = alias.name
        self.assertEqual(encontrados, ALIASES_LEGADOS)

    def test_modulo_novo_nao_importa_audio_worker_termux_apk_streaming_ou_rede(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "streaming", "routing", "urllib", "http"):
            self.assertNotIn(proibido, joined)


class _ButtonStyle:
    danger = "danger"
    secondary = "secondary"


class _Button:
    pass


class _ViewBase:
    def __init__(self, cog, owner_id, guild_id, *, timeout=180, target_user_id=None, target_user_name=None):
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.timeout = timeout
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.children = []
        self.message = None
        self._finished = False
        self._stopped = False

    def add_item(self, item):
        self.children.append(item)
        item.view = self

    def is_finished(self):
        return self._finished

    def stop(self):
        self._stopped = True

    async def on_timeout(self):
        self._finished = True


class _SelectStub:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.view = None
        self.source_panel_message = None
        self.target_user_id = None
        self.target_user_name = None


class _SimpleView:
    instancias = []

    def __init__(self, cog, owner_id, guild_id, title, description, select, **kwargs):
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.title = title
        self.description = description
        self.select = select
        self.kwargs = kwargs
        self.send = AsyncMock()
        type(self).instancias.append(self)


def _button(**metadata):
    def decorator(func):
        func.__button_metadata__ = metadata
        return func
    return decorator


def _carregar_modulo():
    _SimpleView.instancias.clear()
    discord = types.ModuleType("discord")
    discord.Interaction = type("Interaction", (), {})
    discord.Message = type("Message", (), {})
    discord.NotFound = type("NotFound", (Exception,), {})
    discord.ButtonStyle = _ButtonStyle
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: "none")
    discord.ui = types.SimpleNamespace(button=_button, Button=_Button)

    modais = types.ModuleType("cogs.tts.interface.modais_simples")
    modais.SeletorCargoIgnorado = type("SeletorCargoIgnorado", (_SelectStub,), {})

    seletores = types.ModuleType("cogs.tts.interface.seletores_basicos")
    seletores.SeletorVelocidade = type("SeletorVelocidade", (_SelectStub,), {})
    seletores.SeletorTom = type("SeletorTom", (_SelectStub,), {})
    seletores.SeletorToggle = type("SeletorToggle", (_SelectStub,), {})

    bases = types.ModuleType("cogs.tts.interface.visoes_base")
    bases.VisaoBaseTTS = _ViewBase
    bases.VisaoSelecaoSimples = _SimpleView

    cogs_pkg = types.ModuleType("cogs")
    cogs_pkg.__path__ = []
    tts_pkg = types.ModuleType("cogs.tts")
    tts_pkg.__path__ = []
    interface_pkg = types.ModuleType("cogs.tts.interface")
    interface_pkg.__path__ = []

    spec = importlib.util.spec_from_file_location("cogs.tts.interface.visoes_auxiliares", MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "discord": discord,
            "cogs": cogs_pkg,
            "cogs.tts": tts_pkg,
            "cogs.tts.interface": interface_pkg,
            "cogs.tts.interface.modais_simples": modais,
            "cogs.tts.interface.seletores_basicos": seletores,
            "cogs.tts.interface.visoes_base": bases,
        },
    ):
        spec.loader.exec_module(module)
    return module


class TTSInterfaceVisoesAuxiliaresComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def test_assinaturas_das_visoes_preservam_contrato(self):
        modulo = _carregar_modulo()
        esperadas = {
            "VisaoConfiguracaoCargoIgnorado": ["self", "cog", "owner_id", "guild_id", "timeout", "source_panel_message"],
            "VisaoLeituraRapidaTTS": ["self", "cog", "owner_id", "guild_id", "server", "source_panel_message", "target_user_id", "target_user_name"],
            "VisaoStatusTTS": ["self", "cog", "owner_id", "guild_id", "timeout", "target_user_id", "target_user_name"],
            "VisaoPainelToggleTTS": ["self", "cog", "owner_id", "guild_id", "timeout"],
        }
        for nome, parametros in esperadas.items():
            with self.subTest(nome=nome):
                self.assertEqual(list(inspect.signature(getattr(modulo, nome).__init__).parameters), parametros)

    async def test_cargo_ignorado_preserva_seletor_envio_e_desativacao(self):
        modulo = _carregar_modulo()
        remover = AsyncMock()
        cog = types.SimpleNamespace(
            _make_embed=lambda *args, **kwargs: (args, kwargs),
            _remove_ignored_tts_role_from_panel=remover,
        )
        origem = object()
        view = modulo.VisaoConfiguracaoCargoIgnorado(cog, 10, 20, source_panel_message=origem)
        self.assertEqual(view.panel_kind, "server")
        self.assertEqual(len(view.children), 1)

        response = types.SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
        original = object()
        interaction = types.SimpleNamespace(message=object(), response=response, original_response=AsyncMock(return_value=original))
        await view.send(interaction)
        response.send_message.assert_awaited_once()
        self.assertIs(view.message, original)
        self.assertIs(view.source_panel_message, origem)

        await view.remove_role_button(interaction, _Button())
        remover.assert_awaited_once_with(interaction, source_panel_message=origem)

    async def test_leitura_rapida_preserva_seletores_contexto_e_envio(self):
        modulo = _carregar_modulo()
        cog = types.SimpleNamespace(_make_embed=lambda *args, **kwargs: (args, kwargs))
        origem = object()
        view = modulo.VisaoLeituraRapidaTTS(
            cog,
            1,
            2,
            server=True,
            source_panel_message=origem,
            target_user_id=3,
            target_user_name="Alvo",
        )
        self.assertEqual(len(view.children), 2)
        for item in view.children:
            with self.subTest(tipo=type(item).__name__):
                self.assertTrue(item.kwargs["server"])
                self.assertIs(item.source_panel_message, origem)
                self.assertEqual(item.target_user_id, 3)
                self.assertEqual(item.target_user_name, "Alvo")

        response = types.SimpleNamespace(is_done=lambda: True)
        followup = types.SimpleNamespace(send=AsyncMock(return_value=object()))
        interaction = types.SimpleNamespace(response=response, followup=followup)
        await view.send(interaction)
        followup.send.assert_awaited_once()
        self.assertTrue(followup.send.await_args.kwargs["ephemeral"])

    async def test_status_preserva_registro_refresh_e_timeout(self):
        modulo = _carregar_modulo()
        registrar = Mock()
        remover = Mock()
        embed = object()
        guild = types.SimpleNamespace(get_member=lambda user_id: types.SimpleNamespace(id=user_id))
        cog = types.SimpleNamespace(
            _register_status_view=registrar,
            _unregister_status_view=remover,
            bot=types.SimpleNamespace(get_guild=lambda guild_id: guild),
            _member_panel_name=lambda member: "Pessoa",
            _build_status_embed=AsyncMock(return_value=embed),
        )
        view = modulo.VisaoStatusTTS(cog, 11, 22, target_user_id=33)
        message = types.SimpleNamespace(edit=AsyncMock())
        view.attach_message(message)
        registrar.assert_called_once_with(view)

        await view.refresh_from_config_change()
        cog._build_status_embed.assert_awaited_once_with(22, 33, viewer_user_id=11, target_user_name="Pessoa", public=False)
        message.edit.assert_awaited_once_with(embed=embed, view=view)

        await view.on_timeout()
        remover.assert_called_once_with(view)
        self.assertTrue(view._finished)

    async def test_status_reset_preserva_validacoes_e_fluxo_de_sucesso(self):
        modulo = _carregar_modulo()
        reset = AsyncMock()
        build = AsyncMock(return_value="status")
        db = types.SimpleNamespace(reset_user_tts=lambda *args: None)
        cog = types.SimpleNamespace(
            _get_db=lambda: db,
            _make_embed=lambda *args, **kwargs: (args, kwargs),
            _member_panel_name=lambda user: "Pessoa",
            _reset_user_tts_and_refresh=reset,
            _build_status_embed=build,
        )
        view = modulo.VisaoStatusTTS(cog, 10, 20, target_user_id=30, target_user_name="Alvo")
        interaction = types.SimpleNamespace(
            guild=types.SimpleNamespace(id=20),
            user=types.SimpleNamespace(id=10),
            response=types.SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            followup=types.SimpleNamespace(send=AsyncMock()),
        )
        await view.reset_button(interaction, _Button())
        reset.assert_awaited_once_with(20, 30)
        build.assert_awaited_once_with(20, 30, viewer_user_id=10, target_user_name="Alvo", public=False)
        interaction.response.edit_message.assert_awaited_once_with(embed="status", view=view)
        interaction.followup.send.assert_awaited_once()

        # Fora de servidor deve parar antes de tocar no banco/reset.
        reset.reset_mock()
        interaction.guild = None
        await view.reset_button(interaction, _Button())
        interaction.response.send_message.assert_awaited_once()
        reset.assert_not_awaited()

    async def test_painel_toggle_preserva_owner_dinamico_e_abertura_auto_leave(self):
        modulo = _carregar_modulo()
        cog = object()
        view = modulo.VisaoPainelToggleTTS(cog, 0, 99)
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=55))
        self.assertEqual(view._target_owner(interaction), 55)
        self.assertEqual(view.panel_kind, "toggle")

        await view.auto_leave_button(interaction, _Button())
        self.assertEqual(len(_SimpleView.instancias), 1)
        criada = _SimpleView.instancias[0]
        self.assertEqual(criada.owner_id, 55)
        self.assertEqual(criada.guild_id, 99)
        self.assertEqual(criada.title, "Auto leave")
        self.assertEqual(criada.select.args, (cog, "auto_leave"))
        criada.send.assert_awaited_once_with(interaction)


if __name__ == "__main__":
    unittest.main()
