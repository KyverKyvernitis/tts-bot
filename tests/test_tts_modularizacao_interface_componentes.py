from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
COMPONENTES_PATH = ROOT / "cogs" / "tts" / "interface" / "componentes.py"


NOMES_CANONICOS = {
    "valor_tts_atual",
    "valores_selecionados",
    "cargos_selecionados",
    "primeiro_cargo_selecionado",
    "valor_item",
    "componentes_experimentais_modal_ativos",
    "tentar_adicionar_grupo_radio",
    "tentar_adicionar_grupo_checkbox",
    "criar_seletor_opcional",
    "valor_unico_componente",
    "opcoes_com_valor_padrao",
    "rotulo_modal_disponivel",
    "criar_entrada_texto_modal",
    "adicionar_entrada_texto_modal",
    "adicionar_item_rotulo_modal",
    "criar_seletor_modal",
    "valores_padrao_seletor_cargo",
    "criar_seletor_cargo_modal",
    "valores_radio_correspondem",
    "criar_radio_modal",
    "adicionar_radio_modal",
    "criar_grupo_checkbox_modal",
}

ALIASES_LEGADOS = {
    "_current_tts_value": "valor_tts_atual",
    "_select_values": "valores_selecionados",
    "_selected_roles": "cargos_selecionados",
    "_first_selected_role": "primeiro_cargo_selecionado",
    "_item_value": "valor_item",
    "_experimental_modal_components_enabled": "componentes_experimentais_modal_ativos",
    "_maybe_add_radio_group": "tentar_adicionar_grupo_radio",
    "_maybe_add_checkbox_group": "tentar_adicionar_grupo_checkbox",
    "_make_optional_select": "criar_seletor_opcional",
    "_single_component_value": "valor_unico_componente",
    "_with_default_option": "opcoes_com_valor_padrao",
    "_modal_label_available": "rotulo_modal_disponivel",
    "_make_modal_text_input": "criar_entrada_texto_modal",
    "_add_modal_text_input": "adicionar_entrada_texto_modal",
    "_add_modal_label_item": "adicionar_item_rotulo_modal",
    "_make_modal_select": "criar_seletor_modal",
    "_role_select_default_values": "valores_padrao_seletor_cargo",
    "_make_modal_role_select": "criar_seletor_cargo_modal",
    "_radio_value_matches": "valores_radio_correspondem",
    "_make_modal_radio": "criar_radio_modal",
    "_add_modal_radio": "adicionar_radio_modal",
    "_make_modal_checkbox_group": "criar_grupo_checkbox_modal",
}


class TTSInterfaceComponentesEstruturaTests(unittest.TestCase):
    def test_helpers_foram_extraidos_da_ui_para_modulo_em_portugues(self):
        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        componentes_tree = ast.parse(COMPONENTES_PATH.read_text(encoding="utf-8"))

        ui_defs = {
            node.name
            for node in ui_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        componentes_defs = {
            node.name
            for node in componentes_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertTrue(NOMES_CANONICOS <= componentes_defs)
        self.assertTrue(set(ALIASES_LEGADOS).isdisjoint(ui_defs))

    def test_ui_preserva_aliases_legados_sem_duplicar_implementacao(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        encontrados: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module != "interface.componentes":
                continue
            for alias in node.names:
                encontrados[str(alias.asname or alias.name)] = alias.name

        self.assertEqual(encontrados, ALIASES_LEGADOS)

    def test_novo_modulo_nao_depende_de_audio_worker_termux_ou_apk(self):
        tree = ast.parse(COMPONENTES_PATH.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "streaming", "routing"):
            self.assertNotIn(proibido, joined)


class TTSInterfaceComponentesComportamentoTests(unittest.TestCase):
    @staticmethod
    def _carregar_modulo():
        class SelectOption:
            def __init__(self, *, label, description=None, value=None, default=False):
                self.label = label
                self.description = description
                self.value = value
                self.default = default

        class Role:
            def __init__(self, role_id=0):
                self.id = role_id

        class TextInput:
            def __init__(self, **kwargs):
                self.kwargs = dict(kwargs)
                self.default = None
                self.value = None

        class Select:
            def __init__(self, **kwargs):
                self.kwargs = dict(kwargs)
                self.values = []

        discord = types.ModuleType("discord")
        discord.Role = Role
        discord.SelectOption = SelectOption
        discord.ui = types.SimpleNamespace(
            TextInput=TextInput,
            Select=Select,
        )

        config = types.ModuleType("config")
        config.TTS_EXPERIMENTAL_MODAL_COMPONENTS = False

        spec = importlib.util.spec_from_file_location("_tts_componentes_teste", COMPONENTES_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        with patch.dict(sys.modules, {"discord": discord, "config": config}):
            spec.loader.exec_module(module)
        return module, discord

    def test_valores_e_cargos_preservam_normalizacao_existente(self):
        modulo, discord = self._carregar_modulo()
        cargo = discord.Role(42)
        outro = types.SimpleNamespace(id=77)
        item = types.SimpleNamespace(values=["  edge  ", cargo, outro, None, ""])

        self.assertEqual(modulo.valores_selecionados(item), ["edge", "42", "77"])
        self.assertEqual(modulo.cargos_selecionados(item), [cargo])
        self.assertIs(modulo.primeiro_cargo_selecionado(item), cargo)
        self.assertEqual(modulo.valor_unico_componente(item), "edge")

    def test_valor_tts_atual_preserva_resolucao_de_usuario_servidor_e_fallback(self):
        modulo, _ = self._carregar_modulo()

        class DB:
            def get_guild_tts_defaults(self, guild_id):
                return {"voice": f"server-{guild_id}"}

            def resolve_tts(self, guild_id, user_id):
                return {"voice": f"user-{guild_id}-{user_id}"}

        cog = types.SimpleNamespace(_get_db=lambda: DB())
        self.assertEqual(modulo.valor_tts_atual(cog, 10, 20, "voice"), "user-10-20")
        self.assertEqual(modulo.valor_tts_atual(cog, 10, 20, "voice", server=True), "server-10")
        self.assertEqual(modulo.valor_tts_atual(cog, 10, 20, "missing", "padrao"), "padrao")

    def test_opcoes_e_radio_preservam_regras_de_default(self):
        modulo, discord = self._carregar_modulo()
        opcoes = [
            discord.SelectOption(label="A", value="a"),
            discord.SelectOption(label="A duplicado", value="a"),
            discord.SelectOption(label="B", value="b"),
        ]
        saida = modulo.opcoes_com_valor_padrao(opcoes, "b")
        self.assertEqual([op.value for op in saida], ["a", "b"])
        self.assertFalse(saida[0].default)
        self.assertTrue(saida[1].default)
        self.assertTrue(modulo.valores_radio_correspondem("+25Hz", "25hz"))
        self.assertTrue(modulo.valores_radio_correspondem("+0%", "0"))
        self.assertFalse(modulo.valores_radio_correspondem("25", "50"))

    def test_text_input_mantem_label_opcional_e_limite_do_valor_atual(self):
        modulo, _ = self._carregar_modulo()
        sem_label = modulo.criar_entrada_texto_modal(
            label=None,
            placeholder="Teste",
            current="abcdefgh",
            max_length=4,
            required=False,
        )
        self.assertNotIn("label", sem_label.kwargs)
        self.assertEqual(sem_label.default, "abcd")

        com_label = modulo.criar_entrada_texto_modal(
            label="Nome",
            placeholder="Teste",
            current="abc",
            max_length=10,
            required=True,
        )
        self.assertEqual(com_label.kwargs["label"], "Nome")
        self.assertTrue(com_label.kwargs["required"])
        self.assertEqual(com_label.default, "abc")


if __name__ == "__main__":
    unittest.main()
