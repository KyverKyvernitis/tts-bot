from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
CATALOGOS_PATH = ROOT / "cogs" / "tts" / "interface" / "catalogos_de_vozes.py"
VALORES_ATTS_PATH = ROOT / "cogs" / "tts" / "interface" / "valores_atts.py"


CATALOGOS_CANONICOS = {
    "idioma_edge_da_voz",
    "voz_edge_corresponde_idioma",
    "opcoes_idiomas_edge",
    "opcoes_vozes_edge_por_idioma",
    "primeira_voz_edge_por_idioma",
    "principais_opcoes_vozes_edge",
    "principais_opcoes_idiomas_gtts",
}

VALORES_ATTS_CANONICOS = {
    "normalizar_localidade_atts",
    "normalizar_fator_atts",
    "normalizar_fator_personalizado_atts",
    "padrao_radio_modal_atts",
    "separar_valores_personalizados_atts",
}

FACHADAS_LEGADAS = {
    "_edge_language_from_voice": ["voice", "default"],
    "_edge_voice_matches_language": ["voice", "language"],
    "_edge_language_options": ["cog", "current"],
    "_edge_voice_options_for_language": ["cog", "language", "current"],
    "_pick_first_edge_voice_for_language": ["cog", "language", "current"],
    "_top_edge_voice_options": ["cog", "current"],
    "_top_gtts_language_options": ["cog", "current"],
    "_normalize_atts_locale": ["value", "default"],
    "_normalize_atts_factor": ["value", "default"],
    "_normalize_atts_custom_factor": ["value"],
    "_atts_modal_radio_default": ["current", "presets"],
    "_parse_atts_custom_values": ["value", "default_rate", "default_pitch"],
}


class TTSInterfaceCatalogosEstruturaTests(unittest.TestCase):
    def test_implementacoes_reais_estao_em_modulos_com_nomes_em_portugues(self):
        catalog_tree = ast.parse(CATALOGOS_PATH.read_text(encoding="utf-8"))
        valores_tree = ast.parse(VALORES_ATTS_PATH.read_text(encoding="utf-8"))
        catalog_defs = {node.name for node in catalog_tree.body if isinstance(node, ast.FunctionDef)}
        valores_defs = {node.name for node in valores_tree.body if isinstance(node, ast.FunctionDef)}
        self.assertTrue(CATALOGOS_CANONICOS <= catalog_defs)
        self.assertTrue(VALORES_ATTS_CANONICOS <= valores_defs)

    def test_ui_mantem_fachadas_com_assinaturas_legadas(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        for nome, parametros in FACHADAS_LEGADAS.items():
            self.assertIn(nome, defs)
            node = defs[nome]
            encontrados = [arg.arg for arg in node.args.args + node.args.kwonlyargs]
            self.assertEqual(encontrados, parametros, nome)
            self.assertLessEqual(len(node.body), 1 if nome != "_parse_atts_custom_values" else 1, nome)

    def test_modulos_novos_nao_importam_worker_termux_apk_ou_audio(self):
        for path in (CATALOGOS_PATH, VALORES_ATTS_PATH):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
            joined = "\n".join(imports).lower()
            for proibido in ("worker", "termux", "android", "audio", "streaming", "routing"):
                self.assertNotIn(proibido, joined)


class TTSInterfaceCatalogosComportamentoTests(unittest.TestCase):
    @staticmethod
    def _carregar_catalogos():
        class SelectOption:
            def __init__(self, *, label, description=None, value=None, default=False):
                self.label = label
                self.description = description
                self.value = value
                self.default = default

        discord = types.ModuleType("discord")
        discord.SelectOption = SelectOption
        common = types.ModuleType("cogs.tts.common")
        common._shorten = lambda value, limit: str(value)[:limit]

        spec = importlib.util.spec_from_file_location(
            "cogs.tts.interface.catalogos_de_vozes",
            CATALOGOS_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        with patch.dict(sys.modules, {"discord": discord, "cogs.tts.common": common}):
            spec.loader.exec_module(module)
        return module

    @staticmethod
    def _carregar_valores_atts():
        spec = importlib.util.spec_from_file_location("_tts_valores_atts_teste", VALORES_ATTS_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_edge_lista_apenas_vozes_reais_do_catalogo_carregado(self):
        modulo = self._carregar_catalogos()
        cog = types.SimpleNamespace(
            edge_voice_cache=["pt-BR-FranciscaNeural", "en-US-JennyNeural"],
            edge_voice_names={"pt-BR-AntonioNeural"},
        )
        opcoes = modulo.opcoes_vozes_edge_por_idioma(
            cog,
            idioma="pt-BR",
            atual="pt-BR-FranciscaNeural",
        )
        valores = [op.value for op in opcoes]
        self.assertEqual(valores, ["pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"])
        self.assertTrue(opcoes[0].default)
        self.assertNotIn("en-US-JennyNeural", valores)

    def test_edge_idiomas_e_gtts_preservam_defaults(self):
        modulo = self._carregar_catalogos()
        cog = types.SimpleNamespace(
            edge_voice_cache=["en-US-JennyNeural", "pt-BR-FranciscaNeural"],
            edge_voice_names=set(),
            gtts_languages={"pt-br": "Portuguese", "en": "English"},
        )
        edge = modulo.opcoes_idiomas_edge(cog, "en-US")
        self.assertEqual([op.value for op in edge], ["en-US", "pt-BR"])
        self.assertTrue(edge[0].default)
        gtts = modulo.principais_opcoes_idiomas_gtts(cog, "en")
        self.assertEqual(gtts[0].value, "en")
        self.assertTrue(gtts[0].default)

    def test_normalizacao_atts_preserva_limites_e_customizacao(self):
        modulo = self._carregar_valores_atts()
        self.assertEqual(modulo.normalizar_localidade_atts("PT_br"), "pt-BR")
        self.assertEqual(modulo.normalizar_fator_atts("2,8x"), "2")
        self.assertEqual(modulo.normalizar_fator_atts("0.1"), "0.5")
        self.assertIsNone(modulo.normalizar_fator_personalizado_atts("2.1"))
        self.assertEqual(modulo.normalizar_fator_personalizado_atts("1,25x"), "1.25")
        self.assertEqual(modulo.padrao_radio_modal_atts("1.25", {"0.5", "1", "1.5"}), "custom")
        self.assertEqual(
            modulo.separar_valores_personalizados_atts("1.2/0.8"),
            ("1.2", "0.8"),
        )


if __name__ == "__main__":
    unittest.main()
