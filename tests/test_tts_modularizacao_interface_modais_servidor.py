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
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "modais_servidor.py"

CLASSES_CANONICAS = {"ModalPrefixosServidor", "ModalRegrasServidorTTS"}
ALIASES_LEGADOS = {
    "ServerPrefixesModal": "ModalPrefixosServidor",
    "TTSServerRulesModal": "ModalRegrasServidorTTS",
}


class TTSInterfaceModaisServidorEstruturaTests(unittest.TestCase):
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
            if not isinstance(node, ast.ImportFrom) or node.module != "interface.modais_servidor":
                continue
            for alias in node.names:
                encontrados[str(alias.asname or alias.name)] = alias.name
        self.assertEqual(encontrados, ALIASES_LEGADOS)

    def test_modulo_novo_nao_importa_audio_worker_termux_apk_streaming_ou_rede(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "streaming", "routing", "urllib", "http"):
            self.assertNotIn(proibido, joined)


class _ModalBase:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def __init__(self):
        self.children = []

    def add_item(self, item):
        self.children.append(item)

    def clear_items(self):
        self.children.clear()


class _Input:
    def __init__(self, *, value="", **kwargs):
        self.value = value
        self.default = value
        self.values = []
        self.kwargs = kwargs


class _Role:
    def __init__(self, role_id: int, name: str):
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"


class _Guild:
    def __init__(self, guild_id: int, roles=None):
        self.id = guild_id
        self.roles = list(roles or [])

    def get_role(self, role_id: int):
        return next((role for role in self.roles if role.id == role_id), None)


def _carregar_modulo(*, defaults=None, prefix_validator=None):
    discord = types.ModuleType("discord")
    discord.Interaction = type("Interaction", (), {})
    discord.Message = type("Message", (), {})
    discord.Role = _Role
    discord.Guild = _Guild
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: "none")
    discord.ui = types.SimpleNamespace(Modal=_ModalBase, TextInput=lambda **kwargs: _Input(**kwargs))

    config = types.ModuleType("config")
    config.PREFIX = "_"
    config.TTS_ATTS_PREFIX = "%"
    config.TTS_TETO_PREFIX = "'"
    config.TTS_PREFIX = "."
    config.EDGE_TTS_PREFIX = ","

    prefix = types.ModuleType("cogs.tts.prefix")
    prefix.validate_prefix_values = prefix_validator or (lambda **values: (True, ""))

    componentes = types.ModuleType("cogs.tts.interface.componentes")

    def adicionar_entrada_texto_modal(modal, nome, *, current="", **kwargs):
        item = _Input(value=current, **kwargs)
        setattr(modal, nome, item)
        modal.add_item(item)
        return item

    componentes.adicionar_entrada_texto_modal = adicionar_entrada_texto_modal
    componentes.adicionar_item_rotulo_modal = lambda *args, **kwargs: True
    componentes.criar_entrada_texto_modal = lambda *, current="", **kwargs: _Input(value=current, **kwargs)
    componentes.criar_grupo_checkbox_modal = lambda *args, **kwargs: None
    componentes.criar_seletor_cargo_modal = lambda *args, **kwargs: None
    componentes.primeiro_cargo_selecionado = lambda item: getattr(item, "selected_role", None)
    componentes.rotulo_modal_disponivel = lambda: False
    componentes.valor_item = lambda item, default="": str(getattr(item, "value", default) or default).strip()
    componentes.valores_selecionados = lambda item: list(getattr(item, "values", []) or [])

    salvar = AsyncMock()
    operacoes = types.ModuleType("cogs.tts.interface.operacoes_painel")
    operacoes.salvar_atualizacoes_modal_tts = salvar

    cogs_pkg = types.ModuleType("cogs")
    cogs_pkg.__path__ = []
    tts_pkg = types.ModuleType("cogs.tts")
    tts_pkg.__path__ = []
    interface_pkg = types.ModuleType("cogs.tts.interface")
    interface_pkg.__path__ = []

    spec = importlib.util.spec_from_file_location("cogs.tts.interface.modais_servidor", MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "discord": discord,
            "config": config,
            "cogs": cogs_pkg,
            "cogs.tts": tts_pkg,
            "cogs.tts.interface": interface_pkg,
            "cogs.tts.prefix": prefix,
            "cogs.tts.interface.componentes": componentes,
            "cogs.tts.interface.operacoes_painel": operacoes,
        },
    ):
        spec.loader.exec_module(module)

    class _DB:
        def get_guild_tts_defaults(self, guild_id):
            return dict(defaults or {})

    cog = types.SimpleNamespace(
        _get_db=lambda: _DB(),
        _make_embed=lambda *args, **kwargs: (args, kwargs),
    )
    return module, salvar, cog


class TTSInterfaceModaisServidorComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def test_assinaturas_preservam_contrato_legado(self):
        modulo, _, _ = _carregar_modulo()
        self.assertEqual(
            list(inspect.signature(modulo.ModalPrefixosServidor.__init__).parameters),
            ["self", "cog", "panel_message"],
        )
        self.assertEqual(
            list(inspect.signature(modulo.ModalRegrasServidorTTS.__init__).parameters),
            ["self", "cog", "panel_message", "force_text_fallback"],
        )

    async def test_prefixos_preservam_defaults_e_migram_colisao_historica(self):
        defaults = {
            "bot_prefix": "_",
            "atts_prefix": "%",
            "teto_prefix": "'",
            "edge_prefix": ",",
            "gtts_prefix": ",",
        }
        modulo, salvar, cog = _carregar_modulo(defaults=defaults)
        guild = _Guild(77)
        modal = modulo.ModalPrefixosServidor(cog, types.SimpleNamespace(guild=guild))

        self.assertEqual(modal.bot_prefix.default, "_")
        self.assertEqual(modal.atts_prefix.default, "%")
        self.assertEqual(modal.teto_prefix.default, "'")
        self.assertEqual(modal.edge_prefix.default, ",")
        self.assertEqual(modal.gtts_prefix.default, ".")
        salvar.assert_not_awaited()

    async def test_prefixos_validos_salvam_chave_legada_tts_prefix(self):
        modulo, salvar, cog = _carregar_modulo()
        modal = modulo.ModalPrefixosServidor(cog, types.SimpleNamespace(guild=_Guild(7)))
        modal.bot_prefix.value = "!"
        modal.atts_prefix.value = "%"
        modal.teto_prefix.value = "'"
        modal.gtts_prefix.value = "."
        modal.edge_prefix.value = ","
        interaction = types.SimpleNamespace(response=types.SimpleNamespace(send_message=AsyncMock()))

        await modal.on_submit(interaction)

        salvar.assert_awaited_once()
        kwargs = salvar.await_args.kwargs
        self.assertTrue(kwargs["server"])
        self.assertEqual(kwargs["updates"]["bot_prefix"], "!")
        self.assertEqual(kwargs["updates"]["gtts_prefix"], ".")
        self.assertEqual(kwargs["updates"]["tts_prefix"], ".")
        self.assertEqual(kwargs["success_title"], "Prefixos atualizados")

    async def test_prefixos_invalidos_respondem_sem_salvar(self):
        modulo, salvar, cog = _carregar_modulo(prefix_validator=lambda **values: (False, "duplicado"))
        modal = modulo.ModalPrefixosServidor(cog, types.SimpleNamespace(guild=_Guild(7)))
        response = types.SimpleNamespace(send_message=AsyncMock())
        interaction = types.SimpleNamespace(response=response)

        await modal.on_submit(interaction)

        response.send_message.assert_awaited_once()
        salvar.assert_not_awaited()

    async def test_regras_fallback_preserva_migracao_de_cargo_existente(self):
        role = _Role(123, "Silencioso")
        modulo, salvar, cog = _carregar_modulo(defaults={"ignored_tts_role_id": 123})
        modal = modulo.ModalRegrasServidorTTS(cog, types.SimpleNamespace(guild=_Guild(8, [role])), force_text_fallback=True)

        self.assertEqual(modal.current_ignored_role_id, 123)
        self.assertTrue(modal.current_ignored_role_enabled)
        self.assertEqual(modal.current_ignored_role_name, "Silencioso")
        self.assertEqual(modal.ignored_role.value, "123")
        self.assertEqual(modal.ignored_role_enabled.value, "sim")
        salvar.assert_not_awaited()

    async def test_regras_troca_de_cargo_liga_regra_e_salva(self):
        role = _Role(456, "Novo")
        modulo, salvar, cog = _carregar_modulo(defaults={"ignored_tts_role_id": 123, "ignored_tts_role_enabled": False})
        modal = modulo.ModalRegrasServidorTTS(cog, types.SimpleNamespace(guild=_Guild(8, [role])), force_text_fallback=True)
        modal.ignored_role.value = "Novo"
        interaction = types.SimpleNamespace(
            guild=_Guild(8, [role]),
            response=types.SimpleNamespace(send_message=AsyncMock()),
        )

        await modal.on_submit(interaction)

        salvar.assert_awaited_once()
        updates = salvar.await_args.kwargs["updates"]
        self.assertEqual(updates["ignored_tts_role_id"], 456)
        self.assertTrue(updates["ignored_tts_role_enabled"])

    async def test_regras_cargo_inexistente_responde_sem_salvar(self):
        modulo, salvar, cog = _carregar_modulo()
        modal = modulo.ModalRegrasServidorTTS(cog, types.SimpleNamespace(guild=_Guild(8)), force_text_fallback=True)
        modal.ignored_role.value = "NaoExiste"
        response = types.SimpleNamespace(send_message=AsyncMock())
        interaction = types.SimpleNamespace(guild=_Guild(8), response=response)

        await modal.on_submit(interaction)

        response.send_message.assert_awaited_once()
        salvar.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
