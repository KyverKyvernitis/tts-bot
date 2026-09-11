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
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "visoes_layout.py"


class TTSInterfaceVisoesLayoutEstruturaTests(unittest.TestCase):
    def test_implementacao_canonicamente_em_portugues_foi_extraida(self):
        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        modulo_tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))

        classes_ui = {n.name for n in ui_tree.body if isinstance(n, ast.ClassDef)}
        classes_modulo = {n.name for n in modulo_tree.body if isinstance(n, ast.ClassDef)}

        self.assertIn("VisaoLayoutBaseTTS", classes_modulo)
        self.assertNotIn("_BaseTTSLayoutView", classes_ui)
        atribuicoes_ui = {
            alvo.id
            for no in ui_tree.body
            if isinstance(no, ast.Assign)
            for alvo in no.targets
            if isinstance(alvo, ast.Name)
        }
        self.assertNotIn("_TTS_LAYOUT_VIEW_CLS", atribuicoes_ui)

    def test_ui_preserva_nome_legado_por_alias_de_importacao(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        encontrados = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module != "interface.visoes_layout":
                continue
            for alias in node.names:
                encontrados[alias.asname or alias.name] = alias.name
        self.assertEqual(encontrados, {"_BaseTTSLayoutView": "VisaoLayoutBaseTTS"})

    def test_modulo_novo_nao_depende_de_audio_worker_termux_apk_ou_rede(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "streaming", "routing", "aiohttp", "requests"):
            self.assertNotIn(proibido, joined)


class _View:
    def __init__(self, *, timeout=None, **kwargs):
        self.timeout = timeout


class _LayoutView(_View):
    pass


class _AllowedMentions:
    MARCADOR = object()

    @staticmethod
    def none():
        return _AllowedMentions.MARCADOR


class _Interaction:
    pass


def _carregar_modulo(*, prefixo="_"):
    discord = types.ModuleType("discord")
    discord.Message = type("Message", (), {})
    discord.Interaction = _Interaction
    discord.AllowedMentions = _AllowedMentions
    discord.ui = types.SimpleNamespace(View=_View, LayoutView=_LayoutView)

    config = types.ModuleType("config")
    config.BOT_PREFIX = prefixo
    config.PREFIX = "?"

    # Evita reutilizar visoes_base carregado com outra implementação de discord.
    sys.modules.pop("cogs.tts.interface.visoes_base", None)
    nome = "cogs.tts.interface._visoes_layout_teste"
    spec = importlib.util.spec_from_file_location(nome, MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(sys.modules, {"discord": discord, "config": config, nome: module}):
        spec.loader.exec_module(module)
    return module


class TTSInterfaceVisoesLayoutComportamentoTests(unittest.IsolatedAsyncioTestCase):
    async def test_layout_preserva_classe_timeout_estado_e_owner_livre(self):
        modulo = _carregar_modulo()
        cog = types.SimpleNamespace(_make_embed=lambda *args, **kwargs: (args, kwargs))
        with patch.object(modulo.time, "monotonic", return_value=50.0):
            view = modulo.VisaoLayoutBaseTTS(
                cog,
                0,
                456,
                timeout=30,
                target_user_id=789,
                target_user_name="Pessoa",
            )

        self.assertIsInstance(view, _LayoutView)
        self.assertEqual(view.timeout, 86400.0)
        self.assertEqual(view.guild_id, 456)
        self.assertEqual(view.target_user_id, 789)
        self.assertEqual(view.target_user_name, "Pessoa")
        self.assertEqual(view.expires_at_monotonic, 80.0)

        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=999))
        with patch.object(view, "_is_expired", return_value=False):
            self.assertTrue(await view.interaction_check(interaction))

    async def test_launcher_rejeita_outro_usuario_com_dica_curta_e_mentions_bloqueadas(self):
        modulo = _carregar_modulo()
        cog = types.SimpleNamespace(
            _get_panel_prefix_hint=AsyncMock(return_value="`!tts`"),
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        view = modulo.VisaoLayoutBaseTTS(cog, 10, 20)
        view.panel_kind = "launcher"
        response = types.SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False)
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=99), response=response)

        with patch.object(view, "_is_expired", return_value=False):
            self.assertFalse(await view.interaction_check(interaction))

        cog._get_panel_prefix_hint.assert_awaited_once_with(20, "launcher")
        args, kwargs = response.send_message.await_args
        self.assertEqual(args[0], "Essa configuração não é sua, use o comando `!tts` para configurar a sua voz")
        self.assertTrue(kwargs["ephemeral"])
        self.assertIs(kwargs["allowed_mentions"], _AllowedMentions.MARCADOR)

    async def test_launcher_preserva_fallback_de_prefixo(self):
        modulo = _carregar_modulo(prefixo="$")
        cog = types.SimpleNamespace(
            _get_panel_prefix_hint=AsyncMock(side_effect=RuntimeError("falha")),
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        view = modulo.VisaoLayoutBaseTTS(cog, 10, 20)
        view.panel_kind = "launcher"
        response = types.SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False)
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=99), response=response)

        with patch.object(view, "_is_expired", return_value=False):
            self.assertFalse(await view.interaction_check(interaction))

        texto = response.send_message.await_args.args[0]
        self.assertIn("`$tts`", texto)

    async def test_expiracao_preserva_followup_quando_resposta_ja_foi_usada(self):
        modulo = _carregar_modulo()
        cog = types.SimpleNamespace(
            _build_expired_panel_message=AsyncMock(return_value="expirou"),
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        view = modulo.VisaoLayoutBaseTTS(cog, 10, 20)
        response = types.SimpleNamespace(send_message=AsyncMock(), is_done=lambda: True)
        followup = types.SimpleNamespace(send=AsyncMock())
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=10), response=response, followup=followup)

        with patch.object(view, "_is_expired", return_value=True):
            self.assertFalse(await view.interaction_check(interaction))

        followup.send.assert_awaited_once_with("expirou", ephemeral=True)
        response.send_message.assert_not_awaited()

    async def test_painel_comum_rejeita_outro_usuario_com_embed(self):
        modulo = _carregar_modulo()
        embed = object()
        cog = types.SimpleNamespace(_make_embed=lambda *args, **kwargs: embed)
        view = modulo.VisaoLayoutBaseTTS(cog, 10, 20)
        response = types.SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False)
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=99), response=response)

        with patch.object(view, "_is_expired", return_value=False):
            self.assertFalse(await view.interaction_check(interaction))

        response.send_message.assert_awaited_once_with(embed=embed, ephemeral=True)


if __name__ == "__main__":
    unittest.main()
