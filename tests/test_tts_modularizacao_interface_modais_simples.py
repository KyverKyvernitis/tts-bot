from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
MODAIS_PATH = ROOT / "cogs" / "tts" / "interface" / "modais_simples.py"


CLASSES_CANONICAS = {
    "ModalCodigoIdioma",
    "VisaoAjudaIdioma",
    "ModalPrefixoBot",
    "ModalPrefixoATTS",
    "ModalPrefixoTeto",
    "ModalPrefixoGTTS",
    "ModalPrefixoEdge",
    "SeletorCargoIgnorado",
    "ModalApelidoFalado",
}

ALIASES_LEGADOS = {
    "LanguageCodeModal": "ModalCodigoIdioma",
    "LanguageHelpView": "VisaoAjudaIdioma",
    "BotPrefixModal": "ModalPrefixoBot",
    "ATTSPrefixModal": "ModalPrefixoATTS",
    "TetoPrefixModal": "ModalPrefixoTeto",
    "GTTSPrefixModal": "ModalPrefixoGTTS",
    "EdgePrefixModal": "ModalPrefixoEdge",
    "IgnoredRoleSelect": "SeletorCargoIgnorado",
    "SpokenNameModal": "ModalApelidoFalado",
}


class TTSInterfaceModaisSimplesEstruturaTests(unittest.TestCase):
    def test_componentes_reais_foram_extraidos_com_nomes_em_portugues(self):
        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        modais_tree = ast.parse(MODAIS_PATH.read_text(encoding="utf-8"))

        ui_classes = {node.name for node in ui_tree.body if isinstance(node, ast.ClassDef)}
        modais_classes = {node.name for node in modais_tree.body if isinstance(node, ast.ClassDef)}

        self.assertTrue(CLASSES_CANONICAS <= modais_classes)
        self.assertTrue(set(ALIASES_LEGADOS).isdisjoint(ui_classes))

    def test_ui_preserva_todos_os_nomes_legados_como_fachadas(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        encontrados: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module != "interface.modais_simples":
                continue
            for alias in node.names:
                encontrados[str(alias.asname or alias.name)] = alias.name

        self.assertEqual(encontrados, ALIASES_LEGADOS)

    def test_modulo_novo_nao_depende_de_audio_worker_termux_apk_ou_config(self):
        tree = ast.parse(MODAIS_PATH.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "streaming", "routing", "config"):
            self.assertNotIn(proibido, joined)


class _TextInput:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        self.default = None
        self.value = ""

    def __str__(self):
        return str(self.value)


class _Modal:
    title = None

    def __init_subclass__(cls, *, title=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.title = title

    def __init__(self, *args, **kwargs):
        pass


class _View:
    def __init__(self, *, timeout=None, **kwargs):
        self.timeout = timeout


class _RoleSelect:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        self.values = []
        self.view = None


class _Role:
    def __init__(self, role_id=0):
        self.id = role_id


class _Embed:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)


class _Color:
    @staticmethod
    def red():
        return "red"


def _button(**decorator_kwargs):
    def decorator(func):
        func.__discord_button_kwargs__ = dict(decorator_kwargs)
        return func
    return decorator


def _carregar_modulo():
    discord = types.ModuleType("discord")
    discord.Message = type("Message", (), {})
    discord.Interaction = type("Interaction", (), {})
    discord.Role = _Role
    discord.Embed = _Embed
    discord.Color = _Color
    discord.ButtonStyle = types.SimpleNamespace(secondary=2)
    discord.ui = types.SimpleNamespace(
        Modal=_Modal,
        View=_View,
        RoleSelect=_RoleSelect,
        TextInput=_TextInput,
        Button=type("Button", (), {}),
        button=_button,
    )

    spec = importlib.util.spec_from_file_location("_tts_modais_simples_teste", MODAIS_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(sys.modules, {"discord": discord}):
        spec.loader.exec_module(module)
    return module, discord


class TTSInterfaceModaisSimplesComportamentoTests(unittest.IsolatedAsyncioTestCase):
    async def test_modal_de_idioma_preserva_delegacao_e_argumentos(self):
        modulo, _ = _carregar_modulo()
        cog = types.SimpleNamespace(_apply_language_from_panel=AsyncMock())
        panel = object()
        interaction = object()
        modal = modulo.ModalCodigoIdioma(
            cog,
            panel,
            server=True,
            target_user_id=123,
            target_user_name="Core",
        )
        modal.language_code.value = " pt-br "

        await modal.on_submit(interaction)

        cog._apply_language_from_panel.assert_awaited_once_with(
            interaction,
            "pt-br",
            server=True,
            source_panel_message=panel,
            target_user_id=123,
            target_user_name="Core",
        )

    async def test_modais_de_prefixo_preservam_tipo_e_valor_enviado(self):
        modulo, _ = _carregar_modulo()
        casos = [
            (modulo.ModalPrefixoBot, "bot", "_"),
            (modulo.ModalPrefixoATTS, "atts", "%"),
            (modulo.ModalPrefixoTeto, "teto", "'"),
            (modulo.ModalPrefixoGTTS, "gtts", "."),
            (modulo.ModalPrefixoEdge, "edge", ","),
        ]
        for classe, tipo, valor in casos:
            with self.subTest(tipo=tipo):
                aplicar = AsyncMock()
                cog = types.SimpleNamespace(_apply_server_prefix_from_modal=aplicar)
                panel = object()
                interaction = object()
                modal = classe(cog, panel, 10, 20)
                modal.new_prefix.value = valor

                await modal.on_submit(interaction)

                aplicar.assert_awaited_once_with(
                    interaction,
                    prefix_kind=tipo,
                    prefix=valor,
                    panel_message=panel,
                )

    async def test_modal_apelido_preserva_limite_default_e_delegacao(self):
        modulo, _ = _carregar_modulo()
        aplicar = AsyncMock()
        cog = types.SimpleNamespace(_apply_spoken_name_from_modal=aplicar)
        panel = object()
        interaction = object()
        modal = modulo.ModalApelidoFalado(
            cog,
            panel,
            target_user_id=5,
            target_user_name="Pessoa",
            current_value="x" * 50,
        )
        self.assertEqual(modal.spoken_name.default, "x" * 32)
        modal.spoken_name.value = "Maria"

        await modal.on_submit(interaction)

        aplicar.assert_awaited_once_with(
            interaction,
            "Maria",
            panel_message=panel,
            target_user_id=5,
            target_user_name="Pessoa",
        )

    async def test_seletor_cargo_ignorado_preserva_validacao_e_delegacao(self):
        modulo, discord = _carregar_modulo()
        aplicar = AsyncMock()
        cog = types.SimpleNamespace(
            _apply_ignored_tts_role_from_panel=aplicar,
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        selector = modulo.SeletorCargoIgnorado(cog)
        panel = object()
        selector.view = types.SimpleNamespace(source_panel_message=panel)
        role = discord.Role(42)
        selector.values = [role]
        interaction = types.SimpleNamespace(response=types.SimpleNamespace(send_message=AsyncMock()))

        await selector.callback(interaction)

        aplicar.assert_awaited_once_with(interaction, role, source_panel_message=panel)
        interaction.response.send_message.assert_not_awaited()

        aplicar.reset_mock()
        selector.values = [object()]
        await selector.callback(interaction)
        aplicar.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()

    async def test_visao_ajuda_idioma_preserva_bloqueio_e_abertura_do_modal(self):
        modulo, _ = _carregar_modulo()
        cog = types.SimpleNamespace(
            gtts_languages={"pt": "Português"},
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        panel = object()
        view = modulo.VisaoAjudaIdioma(
            cog,
            owner_id=100,
            guild_id=200,
            server=False,
            source_panel_message=panel,
            target_user_id=300,
            target_user_name="Alvo",
        )
        response = types.SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock())
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=101), response=response)

        self.assertFalse(await view.interaction_check(interaction))
        response.send_message.assert_awaited_once()

        response.send_message.reset_mock()
        interaction.user.id = 100
        self.assertTrue(await view.interaction_check(interaction))
        response.send_message.assert_not_awaited()

        await view.select_button(interaction, object())
        response.send_modal.assert_awaited_once()
        modal = response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, modulo.ModalCodigoIdioma)
        self.assertIs(modal.panel_message, panel)
        self.assertEqual(modal.target_user_id, 300)
        self.assertEqual(modal.target_user_name, "Alvo")


if __name__ == "__main__":
    unittest.main()
