from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
COG_PATH = ROOT / "cogs" / "tts" / "cog.py"
APELIDOS_PATH = ROOT / "cogs" / "tts" / "configuracao" / "apelidos.py"
STATUS_PATH = ROOT / "cogs" / "tts" / "interface" / "status_tts.py"


def _carregar_modulo(caminho: Path, nome: str):
    spec = importlib.util.spec_from_file_location(nome, caminho)
    modulo = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(modulo)
    return modulo


def _carregar_status(*, build_status_embed=None):
    cogs = types.ModuleType("cogs")
    cogs.__path__ = []
    tts = types.ModuleType("cogs.tts")
    tts.__path__ = []
    interface = types.ModuleType("cogs.tts.interface")
    interface.__path__ = []
    utils = types.ModuleType("cogs.tts.utils")
    utils.__path__ = []
    embed = types.ModuleType("cogs.tts.utils.embed")
    embed.build_status_embed = build_status_embed or MagicMock(return_value="EMBED")
    embed.spoken_name_status_text = lambda *, active_name, active_source, custom_name="": (
        f"{active_name}|{active_source}|{custom_name}",
        active_source,
    )
    embed.status_badge = lambda value, *, on="Ativo", off="Inativo": f"{on if value else off}"
    embed.status_engine_label = lambda engine: f"motor:{engine}"
    embed.status_source_badge = lambda source: f"origem:{source}"
    embed.status_voice_channel_text = lambda guild, target_user_id: f"canal:{target_user_id}"

    nome = "cogs.tts.interface._status_tts_teste"
    spec = importlib.util.spec_from_file_location(nome, STATUS_PATH)
    modulo = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "cogs": cogs,
            "cogs.tts": tts,
            "cogs.tts.interface": interface,
            "cogs.tts.utils": utils,
            "cogs.tts.utils.embed": embed,
            nome: modulo,
        },
    ):
        spec.loader.exec_module(modulo)
    return modulo, embed


class TTSModularizacaoCogEstruturaTests(unittest.TestCase):
    def test_dois_cortes_canonicos_em_portugues_foram_criados(self):
        apelidos = ast.parse(APELIDOS_PATH.read_text(encoding="utf-8"))
        status = ast.parse(STATUS_PATH.read_text(encoding="utf-8"))
        funcoes_apelidos = {
            node.name for node in apelidos.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        funcoes_status = {
            node.name for node in status.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(
            {
                "obter_apelido_falado_salvo",
                "validar_entrada_apelido_falado",
                "resolver_apelido_falado",
            }.issubset(funcoes_apelidos)
        )
        self.assertTrue(
            {
                "origem_configuracao_status",
                "texto_booleano_status",
                "distintivo_status",
                "distintivo_origem_status",
                "rotulo_motor_status",
                "texto_canal_voz_status",
                "texto_apelido_status",
                "construir_embed_status_tts",
            }.issubset(funcoes_status)
        )

    def test_cog_preserva_os_metodos_legados_como_fachadas(self):
        text = COG_PATH.read_text(encoding="utf-8")
        tree = ast.parse(text)
        classe = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TTSVoice")
        metodos = {node.name: node for node in classe.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        esperados = {
            "_get_saved_spoken_name": "obter_apelido_falado_salvo",
            "_validate_spoken_name_input": "validar_entrada_apelido_falado",
            "_resolve_spoken_name": "resolver_apelido_falado",
            "_setting_origin_label": "origem_configuracao_status",
            "_status_bool": "texto_booleano_status",
            "_status_badge": "distintivo_status",
            "_status_source_badge": "distintivo_origem_status",
            "_status_engine_label": "rotulo_motor_status",
            "_status_voice_channel_text": "texto_canal_voz_status",
            "_spoken_name_status_text": "texto_apelido_status",
            "_build_status_embed": "construir_embed_status_tts",
        }
        for metodo, delegado in esperados.items():
            with self.subTest(metodo=metodo):
                self.assertIn(metodo, metodos)
                fonte = ast.get_source_segment(text, metodos[metodo]) or ""
                self.assertIn(delegado, fonte)

    def test_modulos_novos_nao_importam_audio_worker_termux_apk_ou_rede(self):
        for caminho in (APELIDOS_PATH, STATUS_PATH):
            tree = ast.parse(caminho.read_text(encoding="utf-8"))
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(node.module or "")
            joined = "\n".join(imports).lower()
            for proibido in ("audio", "worker", "termux", "android", "streaming", "aiohttp", "requests"):
                self.assertNotIn(proibido, joined)


class TTSApelidosComportamentoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _carregar_modulo(APELIDOS_PATH, "tts_apelidos_teste")

    def test_leitura_do_apelido_salvo_e_tolerante_a_falhas(self):
        db = types.SimpleNamespace(get_user_tts=lambda guild_id, user_id: {"speaker_name": "  Nome   Falado "})
        normalizar = lambda value: " ".join(str(value).split())
        self.assertEqual(
            self.mod.obter_apelido_falado_salvo(db, 10, 20, normalizar_espacos=normalizar),
            "Nome Falado",
        )
        quebrado = types.SimpleNamespace(get_user_tts=lambda *args: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertEqual(self.mod.obter_apelido_falado_salvo(quebrado, 10, 20, normalizar_espacos=normalizar), "")
        self.assertEqual(self.mod.obter_apelido_falado_salvo(db, None, 20, normalizar_espacos=normalizar), "")

    def test_validacao_preserva_vazio_invalido_normalizacao_e_limite(self):
        normalizar = lambda value: " ".join(str(value).split())
        pronunciavel = lambda value: bool(value) and "!" not in value
        nome_falado = lambda value: str(value).upper()
        validar = self.mod.validar_entrada_apelido_falado
        self.assertEqual(
            validar("   ", normalizar_espacos=normalizar, parece_pronunciavel=pronunciavel, normalizar_nome_falado=nome_falado),
            ("", None),
        )
        valor, erro = validar("nome!", normalizar_espacos=normalizar, parece_pronunciavel=pronunciavel, normalizar_nome_falado=nome_falado)
        self.assertIsNone(valor)
        self.assertIn("caracteres", erro or "")
        valor, erro = validar("a" * 40, normalizar_espacos=normalizar, parece_pronunciavel=pronunciavel, normalizar_nome_falado=nome_falado)
        self.assertEqual(valor, "A" * 32)
        self.assertIsNone(erro)

    def test_resolucao_respeita_prioridade_personalizado_display_username_fallback(self):
        resolver = self.mod.resolver_apelido_falado
        normalizar = lambda value: " ".join(str(value).split())
        pronunciavel = lambda value: bool(value) and not str(value).startswith("?")
        nome_falado = lambda value: str(value).strip().lower()
        membro = types.SimpleNamespace(id=7, display_name="Display", name="Username")

        self.assertEqual(
            resolver(
                membro,
                guild_id=1,
                obter_apelido_salvo=lambda *_: "Personalizado",
                normalizar_espacos=normalizar,
                parece_pronunciavel=pronunciavel,
                normalizar_nome_falado=nome_falado,
            ),
            ("personalizado", "personalizado"),
        )
        self.assertEqual(
            resolver(
                membro,
                guild_id=1,
                obter_apelido_salvo=lambda *_: "",
                normalizar_espacos=normalizar,
                parece_pronunciavel=pronunciavel,
                normalizar_nome_falado=nome_falado,
            ),
            ("display", "apelido do servidor"),
        )
        membro.display_name = "?"
        self.assertEqual(
            resolver(
                membro,
                guild_id=1,
                obter_apelido_salvo=lambda *_: "",
                normalizar_espacos=normalizar,
                parece_pronunciavel=pronunciavel,
                normalizar_nome_falado=nome_falado,
            ),
            ("username", "nome de usuário"),
        )
        membro.name = "?"
        self.assertEqual(
            resolver(
                membro,
                guild_id=1,
                obter_apelido_salvo=lambda *_: "",
                normalizar_espacos=normalizar,
                parece_pronunciavel=pronunciavel,
                normalizar_nome_falado=nome_falado,
            ),
            ("usuário", "padrão"),
        )

    def test_resolucao_preserva_preparacao_historica_sem_propagar_erro(self):
        chamado = MagicMock(side_effect=RuntimeError("db"))
        membro = types.SimpleNamespace(id=8, display_name="Nome", name="User")
        resultado = self.mod.resolver_apelido_falado(
            membro,
            guild_id=2,
            obter_apelido_salvo=lambda *_: "",
            normalizar_espacos=lambda value: str(value).strip(),
            parece_pronunciavel=lambda value: bool(value),
            normalizar_nome_falado=lambda value: str(value),
            preparar_contexto_servidor=chamado,
        )
        chamado.assert_called_once_with(membro)
        self.assertEqual(resultado, ("Nome", "apelido do servidor"))


class TTSStatusComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def test_helpers_de_status_preservam_semantica(self):
        mod, _ = _carregar_status()
        self.assertEqual(mod.origem_configuracao_status({"voice": "x"}, "voice"), "Usuário")
        self.assertEqual(mod.origem_configuracao_status({}, "voice"), "Servidor")
        self.assertEqual(mod.texto_booleano_status(True), "Ativado")
        self.assertEqual(mod.texto_booleano_status(False), "Desativado")
        self.assertEqual(mod.distintivo_status(True, ligado="ON", desligado="OFF"), "ON")
        self.assertEqual(mod.distintivo_origem_status("Usuário"), "origem:Usuário")
        self.assertEqual(mod.rotulo_motor_status("edge"), "motor:edge")
        self.assertEqual(mod.texto_canal_voz_status(object(), 44), "canal:44")

    def test_texto_apelido_status_delega_resolucao_e_customizacao(self):
        mod, _ = _carregar_status()
        resolver = MagicMock(return_value=("Ana", "personalizado"))
        resultado = mod.texto_apelido_status(
            9,
            object(),
            resolvido={"speaker_name": "  Ana  "},
            resolver_apelido=resolver,
            normalizar_espacos=lambda value: " ".join(str(value).split()),
        )
        self.assertEqual(resultado, ("Ana|personalizado|Ana", "personalizado"))
        resolver.assert_called_once()

    async def test_construcao_do_embed_preserva_estado_conexao_fila_e_contexto(self):
        build = MagicMock(return_value="EMBED-FINAL")
        mod, _ = _carregar_status(build_status_embed=build)

        membro = types.SimpleNamespace(id=33)
        guild = types.SimpleNamespace(get_member=lambda user_id: membro if user_id == 33 else None)
        canal = types.SimpleNamespace(mention="#voz", name="voz")
        cliente = object()
        fila = types.SimpleNamespace(qsize=lambda: 2)
        estado = types.SimpleNamespace(queue=fila)
        db = types.SimpleNamespace(
            get_user_tts=MagicMock(return_value={"voice": "x"}),
            resolve_tts=MagicMock(return_value={"engine": "edge", "speaker_name": "Ana"}),
        )

        async def talvez_aguardar(valor):
            return valor

        contexto = types.SimpleNamespace(
            _get_db=lambda: db,
            _maybe_await=talvez_aguardar,
            bot=types.SimpleNamespace(get_guild=lambda guild_id: guild),
            guild_states={77: estado},
            _get_voice_client_for_guild=lambda g: cliente,
            _voice_client_is_connected=lambda vc: True,
            _voice_client_is_playing_or_paused=lambda vc: False,
            _voice_client_channel=lambda vc: canal,
            _status_voice_channel_text=lambda g, uid: "#usuario",
            _member_panel_name=lambda m: "@Ana",
            _spoken_name_status_text=lambda gid, m, resolved=None: ("`Ana` (personalizado)", "personalizado"),
        )

        resultado = await mod.construir_embed_status_tts(
            contexto,
            77,
            33,
            viewer_user_id=12,
            target_user_name=None,
            public=True,
        )
        self.assertEqual(resultado, "EMBED-FINAL")
        kwargs = build.call_args.kwargs
        self.assertIs(kwargs["member"], membro)
        self.assertEqual(kwargs["target_name"], "@Ana")
        self.assertEqual(kwargs["queue_size"], 2)
        self.assertTrue(kwargs["is_connected"])
        self.assertFalse(kwargs["is_playing"])
        self.assertEqual(kwargs["user_channel"], "#usuario")
        self.assertEqual(kwargs["bot_channel"], "#voz")
        self.assertEqual(kwargs["spoken_name_text"], "`Ana` (personalizado)")
        self.assertTrue(kwargs["public"])

    async def test_construcao_do_status_sem_banco_ou_guild_preserva_fallbacks(self):
        build = MagicMock(return_value="SEM-CONTEXTO")
        mod, _ = _carregar_status(build_status_embed=build)

        async def talvez_aguardar(valor):
            return valor

        contexto = types.SimpleNamespace(
            _get_db=lambda: None,
            _maybe_await=talvez_aguardar,
            bot=types.SimpleNamespace(get_guild=lambda guild_id: None),
            guild_states={},
            _get_voice_client_for_guild=lambda g: None,
            _voice_client_is_connected=lambda vc: False,
            _voice_client_is_playing_or_paused=lambda vc: False,
            _voice_client_channel=lambda vc: None,
            _status_voice_channel_text=lambda g, uid: "Não disponível",
            _member_panel_name=lambda m: "@usuário",
            _spoken_name_status_text=lambda gid, m, resolved=None: ("`usuário` (padrão)", "padrão"),
        )
        resultado = await mod.construir_embed_status_tts(contexto, 1, 2)
        self.assertEqual(resultado, "SEM-CONTEXTO")
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["queue_size"], 0)
        self.assertEqual(kwargs["bot_channel"], "Desconectado")
        self.assertEqual(kwargs["user_settings"], {})
        self.assertEqual(kwargs["resolved"], {})


if __name__ == "__main__":
    unittest.main()
