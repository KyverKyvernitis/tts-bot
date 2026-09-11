from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
CATALOGO_ATTS_PATH = ROOT / "cogs" / "tts" / "interface" / "catalogo_atts.py"


FUNCOES_CANONICAS = {
    "localidade_corresponde_idioma_atts",
    "localidade_da_voz_atts",
    "opcoes_idiomas_atts",
    "pontuar_voz_atts",
    "vozes_atts_por_idioma",
    "opcoes_vozes_atts_por_idioma",
    "voz_atts_corresponde_idioma",
    "primeira_voz_atts_por_idioma",
    "catalogo_atts_pronto_para_idioma",
}

FACHADAS_LEGADAS = {
    "_atts_locale_matches": ["voice_locale", "language"],
    "_atts_voice_locale_from_name": ["name"],
    "_atts_common_language_options": ["current", "voices"],
    "_atts_voice_score": ["voice", "language"],
    "_atts_matching_voices_for_language": ["catalog", "language"],
    "_atts_voice_options_for_language": ["cog", "language", "current", "catalog"],
    "_atts_voice_matches_language": ["voice", "language"],
    "_pick_atts_voice_for_language": ["cog", "language", "current"],
    "_atts_catalog_ready_for_language": ["catalog", "language"],
}


class SelectOption:
    def __init__(self, *, label, description=None, value=None, default=False):
        self.label = label
        self.description = description
        self.value = value
        self.default = default


def _normalizar_localidade(valor: object, padrao: str = "pt-BR") -> str:
    bruto = str(valor or padrao or "").strip().replace("_", "-")
    if not bruto:
        return str(padrao or "").strip()
    partes = [p for p in bruto.split("-") if p]
    if len(partes) == 1:
        return partes[0].lower()
    return f"{partes[0].lower()}-{partes[1].upper()}"


def _opcoes_com_padrao(options, current):
    current = str(current or "").strip()
    fixed = []
    seen = set()
    matched = False
    for option in options or []:
        value = str(getattr(option, "value", "") or "").strip()
        if not value or value in seen:
            continue
        option.default = bool(current and value == current)
        matched = matched or option.default
        fixed.append(option)
        seen.add(value)
        if len(fixed) >= 25:
            break
    if current and not matched:
        fixed.insert(0, SelectOption(label=current[:100], description="Valor atual", value=current, default=True))
    return fixed[:25]


def _carregar_catalogo_atts():
    discord = types.ModuleType("discord")
    discord.SelectOption = SelectOption

    common = types.ModuleType("cogs.tts.common")
    common._shorten = lambda value, limit: str(value)[:limit]

    componentes = types.ModuleType("cogs.tts.interface.componentes")
    componentes.opcoes_com_valor_padrao = _opcoes_com_padrao

    valores = types.ModuleType("cogs.tts.interface.valores_atts")
    valores.normalizar_localidade_atts = _normalizar_localidade

    spec = importlib.util.spec_from_file_location(
        "cogs.tts.interface.catalogo_atts",
        CATALOGO_ATTS_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "discord": discord,
            "cogs.tts.common": common,
            "cogs.tts.interface.componentes": componentes,
            "cogs.tts.interface.valores_atts": valores,
        },
    ):
        spec.loader.exec_module(module)
    return module


class TTSInterfaceCatalogoATTSEstruturaTests(unittest.TestCase):
    def test_funcoes_reais_estao_no_modulo_em_portugues(self):
        tree = ast.parse(CATALOGO_ATTS_PATH.read_text(encoding="utf-8"))
        defs = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertTrue(FUNCOES_CANONICAS <= defs)

    def test_ui_preserva_assinaturas_das_fachadas_antigas(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        for nome, parametros in FACHADAS_LEGADAS.items():
            self.assertIn(nome, defs)
            node = defs[nome]
            encontrados = [arg.arg for arg in node.args.args + node.args.kwonlyargs]
            self.assertEqual(encontrados, parametros, nome)

    def test_modulo_de_catalogo_nao_acessa_rede_config_worker_audio_ou_android(self):
        tree = ast.parse(CATALOGO_ATTS_PATH.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("config", "urllib", "worker", "termux", "android", "audio", "streaming", "routing"):
            self.assertNotIn(proibido, joined)

    def test_pick_legado_continua_passando_pela_fachada_patchavel_de_opcoes(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        pick = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_pick_atts_voice_for_language"
        )
        nomes = {node.id for node in ast.walk(pick) if isinstance(node, ast.Name)}
        self.assertIn("_atts_voice_options_for_language", nomes)


class TTSInterfaceCatalogoATTSComportamentoTests(unittest.TestCase):
    def test_localidade_exata_familia_e_nome_da_voz(self):
        modulo = _carregar_catalogo_atts()
        self.assertTrue(modulo.localidade_corresponde_idioma_atts("pt_BR", "pt-BR"))
        self.assertTrue(modulo.localidade_corresponde_idioma_atts("pt-PT", "pt-BR"))
        self.assertFalse(modulo.localidade_corresponde_idioma_atts("en-US", "pt-BR"))
        self.assertEqual(modulo.localidade_da_voz_atts("pt_BR-local-voz"), "pt-BR")
        self.assertEqual(modulo.localidade_da_voz_atts("en_1network_voice"), "en")

    def test_opcoes_de_idioma_incluem_localidades_descobertas_sem_duplicar(self):
        modulo = _carregar_catalogo_atts()
        opcoes = modulo.opcoes_idiomas_atts(
            "pt_BR",
            [
                {"name": "nl-NL-local", "locale": "nl_NL"},
                {"name": "nl-NL-outra", "locale": "nl-NL"},
            ],
        )
        valores = [item.value for item in opcoes]
        self.assertEqual(valores[0], "pt-BR")
        self.assertIn("nl-NL", valores)
        self.assertEqual(valores.count("nl-NL"), 1)
        self.assertTrue(opcoes[0].default)
        self.assertLessEqual(len(opcoes), 25)

    def test_vozes_sao_filtradas_e_ordenadas_pela_mesma_pontuacao(self):
        modulo = _carregar_catalogo_atts()
        catalogo = [
            {"name": "pt-BR-network", "locale": "pt-BR", "network_required": True, "quality": 500, "latency": 100},
            {"name": "pt-BR-local", "locale": "pt-BR", "network_required": False, "quality": 100, "latency": 100},
            {"name": "en-US-local", "locale": "en-US", "network_required": False, "quality": 1000, "latency": 0},
        ]
        vozes = modulo.vozes_atts_por_idioma(catalogo, "pt-BR")
        self.assertEqual([item["name"] for item in vozes], ["pt-BR-local", "pt-BR-network"])
        self.assertGreater(
            modulo.pontuar_voz_atts(catalogo[1], "pt-BR"),
            modulo.pontuar_voz_atts(catalogo[0], "pt-BR"),
        )

    def test_opcoes_preservam_auto_default_e_voz_atual(self):
        modulo = _carregar_catalogo_atts()
        buscar = Mock(return_value=[{"name": "pt-BR-local", "locale": "pt-BR", "network_required": False}])
        opcoes = modulo.opcoes_vozes_atts_por_idioma(
            object(),
            idioma="pt-BR",
            atual="pt-BR-atual",
            buscar_catalogo=buscar,
        )
        buscar.assert_called_once_with("pt-BR")
        valores = [item.value for item in opcoes]
        self.assertIn("auto", valores)
        self.assertIn("default", valores)
        self.assertIn("pt-BR-atual", valores)
        self.assertEqual(sum(bool(item.default) for item in opcoes), 1)
        self.assertTrue(next(item for item in opcoes if item.value == "pt-BR-atual").default)

    def test_catalogo_fornecido_nao_dispara_busca_e_estado_pronto_reusa_filtro(self):
        modulo = _carregar_catalogo_atts()
        buscar = Mock(side_effect=AssertionError("não deveria buscar"))
        catalogo = [{"name": "pt-BR-local", "locale": "pt-BR", "network_required": False}]
        opcoes = modulo.opcoes_vozes_atts_por_idioma(
            object(),
            idioma="pt-BR",
            catalogo=catalogo,
            buscar_catalogo=buscar,
        )
        self.assertIn("pt-BR-local", [item.value for item in opcoes])
        buscar.assert_not_called()
        self.assertTrue(modulo.catalogo_atts_pronto_para_idioma(catalogo, "pt-BR"))
        self.assertFalse(modulo.catalogo_atts_pronto_para_idioma(catalogo, "ja-JP"))

    def test_vozes_especiais_nao_consultam_catalogo_e_voz_nomeada_consulta(self):
        modulo = _carregar_catalogo_atts()
        buscar = Mock(return_value=[{"name": "pt-BR-local", "locale": "pt-BR"}])
        self.assertTrue(modulo.voz_atts_corresponde_idioma("auto", "pt-BR", buscar_catalogo=buscar))
        self.assertTrue(modulo.voz_atts_corresponde_idioma("default", "pt-BR", buscar_catalogo=buscar))
        buscar.assert_not_called()
        self.assertTrue(modulo.voz_atts_corresponde_idioma("pt-BR-local", "pt-BR", buscar_catalogo=buscar))
        buscar.assert_called_once_with("pt-BR")
        self.assertEqual(
            modulo.primeira_voz_atts_por_idioma([
                SelectOption(label="Auto", value="auto"),
                SelectOption(label="Padrão", value="default"),
                SelectOption(label="Voz", value="pt-BR-local"),
            ]),
            "pt-BR-local",
        )


if __name__ == "__main__":
    unittest.main()
