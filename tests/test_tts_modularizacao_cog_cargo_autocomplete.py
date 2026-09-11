from __future__ import annotations

import ast
import importlib.util
import inspect
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]
COG_PATH = ROOT / "cogs" / "tts" / "cog.py"
CARGO_PATH = ROOT / "cogs" / "tts" / "configuracao" / "cargo_ignorado.py"
AUTOCOMPLETE_PATH = ROOT / "cogs" / "tts" / "configuracao" / "autocompletar.py"


def _carregar(caminho: Path, nome: str):
    spec = importlib.util.spec_from_file_location(nome, caminho)
    modulo = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(modulo)
    return modulo


class TTSModularizacaoCargoAutocompleteEstruturaTests(unittest.TestCase):
    def test_dois_cortes_canonicos_em_portugues_foram_criados(self):
        cargo = ast.parse(CARGO_PATH.read_text(encoding="utf-8"))
        autocomplete = ast.parse(AUTOCOMPLETE_PATH.read_text(encoding="utf-8"))
        funcoes_cargo = {
            node.name for node in cargo.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        funcoes_autocomplete = {
            node.name for node in autocomplete.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(
            {
                "obter_id_cargo_ignorado_tts",
                "cargo_ignorado_tts_ativo",
                "obter_cargo_ignorado_tts",
                "texto_cargo_ignorado_tts",
                "membro_tem_cargo_ignorado_tts",
                "sufixo_apelido_membro_tts",
            }.issubset(funcoes_cargo)
        )
        self.assertTrue(
            {
                "opcoes_autocomplete_vozes_edge",
                "opcoes_autocomplete_idiomas_gtts",
            }.issubset(funcoes_autocomplete)
        )

    def test_cog_preserva_metodos_legados_como_fachadas(self):
        texto = COG_PATH.read_text(encoding="utf-8")
        tree = ast.parse(texto)
        classe = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSVoice")
        metodos = {node.name: node for node in classe.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        esperados = {
            "_get_ignored_tts_role_id": "obter_id_cargo_ignorado_tts",
            "_ignored_tts_role_enabled": "cargo_ignorado_tts_ativo",
            "_get_ignored_tts_role": "obter_cargo_ignorado_tts",
            "_ignored_tts_role_text": "texto_cargo_ignorado_tts",
            "_member_has_ignored_tts_role": "membro_tem_cargo_ignorado_tts",
            "_spoken_name_suffix": "sufixo_apelido_membro_tts",
            "voice_autocomplete": "opcoes_autocomplete_vozes_edge",
            "language_autocomplete": "opcoes_autocomplete_idiomas_gtts",
        }
        for metodo, delegado in esperados.items():
            with self.subTest(metodo=metodo):
                self.assertIn(metodo, metodos)
                fonte = ast.get_source_segment(texto, metodos[metodo]) or ""
                self.assertIn(delegado, fonte)

    def test_assinaturas_legadas_principais_foram_preservadas(self):
        tree = ast.parse(COG_PATH.read_text(encoding="utf-8"))
        classe = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSVoice")
        metodos = {node.name: node for node in classe.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        casos = {
            "_get_ignored_tts_role_id": ["self", "guild_id", "guild_defaults"],
            "_ignored_tts_role_enabled": ["self", "guild_id", "guild_defaults"],
            "_get_ignored_tts_role": ["self", "guild", "guild_defaults"],
            "_ignored_tts_role_text": ["self", "guild_id", "guild_defaults"],
            "_member_has_ignored_tts_role": ["self", "member", "guild_defaults"],
            "_spoken_name_suffix": ["self", "member", "guild_defaults"],
            "voice_autocomplete": ["self", "interaction", "current"],
            "language_autocomplete": ["self", "interaction", "current"],
        }
        for nome, esperado in casos.items():
            with self.subTest(nome=nome):
                node = metodos[nome]
                parametros = [arg.arg for arg in node.args.args] + [arg.arg for arg in node.args.kwonlyargs]
                self.assertEqual(parametros, esperado)

    def test_modulos_novos_nao_importam_audio_worker_termux_apk_rede_ou_discord(self):
        for caminho in (CARGO_PATH, AUTOCOMPLETE_PATH):
            tree = ast.parse(caminho.read_text(encoding="utf-8"))
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
            joined = "\n".join(imports).lower()
            for proibido in (
                "audio",
                "worker",
                "termux",
                "android",
                "streaming",
                "aiohttp",
                "requests",
                "discord",
            ):
                self.assertNotIn(proibido, joined)


class TTSCargoIgnoradoComportamentoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _carregar(CARGO_PATH, "tts_cargo_ignorado_teste")

    def test_id_prioriza_defaults_e_preserva_fallbacks_do_banco(self):
        db = types.SimpleNamespace(
            get_ignored_tts_role_id=MagicMock(return_value=33),
            get_guild_tts_defaults=MagicMock(return_value={"ignored_tts_role_id": 44}),
        )
        self.assertEqual(self.mod.obter_id_cargo_ignorado_tts(db, 1, guild_defaults={"ignored_tts_role_id": 22}), 22)
        db.get_ignored_tts_role_id.assert_not_called()
        self.assertEqual(self.mod.obter_id_cargo_ignorado_tts(db, 1), 33)

        quebrado = types.SimpleNamespace(
            get_ignored_tts_role_id=lambda guild_id: (_ for _ in ()).throw(RuntimeError("x")),
            get_guild_tts_defaults=lambda guild_id: {"ignored_tts_role_id": 55},
        )
        self.assertEqual(self.mod.obter_id_cargo_ignorado_tts(quebrado, 1), 55)
        self.assertEqual(self.mod.obter_id_cargo_ignorado_tts(None, 1), 0)

    def test_flag_preserva_migracao_suave_e_prioridade_explicita(self):
        obter = MagicMock(return_value=99)
        self.assertFalse(
            self.mod.cargo_ignorado_tts_ativo(
                None,
                1,
                guild_defaults={"ignored_tts_role_id": 99, "ignored_tts_role_enabled": False},
                obter_id_cargo=obter,
            )
        )
        obter.assert_not_called()
        self.assertTrue(
            self.mod.cargo_ignorado_tts_ativo(
                None,
                1,
                guild_defaults={"ignored_tts_role_id": 99},
                obter_id_cargo=obter,
            )
        )

    def test_texto_cargo_preserva_estado_ativo_inativo_e_cargo_ausente(self):
        role = types.SimpleNamespace(mention="<@&7>")
        guild = types.SimpleNamespace(get_role=lambda role_id: role if role_id == 7 else None)
        bot = types.SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 1 else None)
        obter = MagicMock(return_value=7)
        self.assertEqual(
            self.mod.texto_cargo_ignorado_tts(
                bot,
                1,
                obter_id_cargo=obter,
                cargo_ativo=lambda *args, **kwargs: True,
            ),
            "<@&7> · ligado",
        )
        self.assertEqual(
            self.mod.texto_cargo_ignorado_tts(
                bot,
                1,
                obter_id_cargo=obter,
                cargo_ativo=lambda *args, **kwargs: False,
            ),
            "desligado · <@&7> salvo",
        )
        self.assertEqual(
            self.mod.texto_cargo_ignorado_tts(
                bot,
                1,
                obter_id_cargo=lambda *args, **kwargs: 0,
                cargo_ativo=lambda *args, **kwargs: False,
            ),
            "desligado",
        )

    def test_membro_e_sufixo_preservam_regras_de_censura(self):
        guild = types.SimpleNamespace(id=10)
        membro = types.SimpleNamespace(
            guild=guild,
            roles=[types.SimpleNamespace(id=7)],
            voice=types.SimpleNamespace(mute=True),
        )
        self.assertTrue(
            self.mod.membro_tem_cargo_ignorado_tts(
                membro,
                obter_id_cargo=lambda *args, **kwargs: 7,
                cargo_ativo=lambda *args, **kwargs: True,
            )
        )
        self.assertEqual(
            self.mod.sufixo_apelido_membro_tts(
                membro,
                membro_tem_cargo_ignorado=lambda *args, **kwargs: True,
            ),
            " [ultra-censurado]",
        )
        membro.voice.mute = False
        self.assertEqual(
            self.mod.sufixo_apelido_membro_tts(
                membro,
                membro_tem_cargo_ignorado=lambda *args, **kwargs: True,
            ),
            " [bot ignora]",
        )
        self.assertEqual(
            self.mod.sufixo_apelido_membro_tts(
                membro,
                membro_tem_cargo_ignorado=lambda *args, **kwargs: False,
            ),
            "",
        )


class TTSAutocompleteComportamentoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _carregar(AUTOCOMPLETE_PATH, "tts_autocompletar_teste")

    @staticmethod
    def _choice(nome: str, valor: str):
        return types.SimpleNamespace(name=nome, value=valor)

    def test_vozes_edge_priorizam_cache_filtram_pt_e_consulta(self):
        resultados = self.mod.opcoes_autocomplete_vozes_edge(
            "maria",
            vozes_cache=["pt-BR-Maria", "en-US-Maria", "pt-PT-Joana"],
            nomes_vozes={"pt-BR-Outra"},
            construir_escolha=self._choice,
        )
        self.assertEqual([(item.name, item.value) for item in resultados], [("pt-BR-Maria", "pt-BR-Maria")])

    def test_vozes_edge_sem_cache_usam_nomes_ordenados_e_limite_25(self):
        nomes = {f"pt-BR-Voz{i:02d}" for i in range(30)} | {"en-US-Ignore"}
        resultados = self.mod.opcoes_autocomplete_vozes_edge(
            "",
            vozes_cache=[],
            nomes_vozes=nomes,
            construir_escolha=self._choice,
        )
        self.assertEqual(len(resultados), 25)
        self.assertEqual(resultados[0].value, "pt-BR-Voz00")
        self.assertEqual(resultados[-1].value, "pt-BR-Voz24")

    def test_idiomas_gtts_buscam_por_codigo_ou_nome_e_preservam_label(self):
        idiomas = {"pt-br": "Português (Brasil)", "en": "English", "ja": "Japanese"}
        por_nome = self.mod.opcoes_autocomplete_idiomas_gtts(
            "português",
            idiomas=idiomas,
            construir_escolha=self._choice,
        )
        self.assertEqual(len(por_nome), 1)
        self.assertEqual(por_nome[0].value, "pt-br")
        self.assertEqual(por_nome[0].name, "pt-br — Português (Brasil)")

        por_codigo = self.mod.opcoes_autocomplete_idiomas_gtts(
            "ja",
            idiomas=idiomas,
            construir_escolha=self._choice,
        )
        self.assertEqual([(item.name, item.value) for item in por_codigo], [("ja — Japanese", "ja")])


if __name__ == "__main__":
    unittest.main()
