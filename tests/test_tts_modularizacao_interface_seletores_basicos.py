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
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "seletores_basicos.py"

CLASSES_CANONICAS = {
    "SeletorModo",
    "SeletorIdioma",
    "SeletorVelocidade",
    "SeletorTom",
    "SeletorRegiaoVoz",
    "SeletorVoz",
    "SeletorToggle",
}

ALIASES_LEGADOS = {
    "ModeSelect": "SeletorModo",
    "LanguageSelect": "SeletorIdioma",
    "SpeedSelect": "SeletorVelocidade",
    "PitchSelect": "SeletorTom",
    "VoiceRegionSelect": "SeletorRegiaoVoz",
    "VoiceSelect": "SeletorVoz",
    "ToggleSelect": "SeletorToggle",
}


class TTSInterfaceSeletoresBasicosEstruturaTests(unittest.TestCase):
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
            if not isinstance(node, ast.ImportFrom) or node.module != "interface.seletores_basicos":
                continue
            for alias in node.names:
                encontrados[str(alias.asname or alias.name)] = alias.name
        self.assertEqual(encontrados, ALIASES_LEGADOS)

    def test_modulo_novo_nao_importa_audio_worker_termux_apk_ou_streaming(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "streaming", "routing"):
            self.assertNotIn(proibido, joined)


class _SelectOption:
    def __init__(self, *, label, description=None, value=None, emoji=None, default=False):
        self.label = label
        self.description = description
        self.value = value
        self.emoji = emoji
        self.default = default


class _Select:
    def __init__(self, *, placeholder=None, min_values=None, max_values=None, options=None, **kwargs):
        self.placeholder = placeholder
        self.min_values = min_values
        self.max_values = max_values
        self.options = list(options or [])
        self.values = []
        self.view = None


class _VisaoSelecaoSimples:
    def __init__(
        self,
        cog,
        owner_id,
        guild_id,
        title,
        description,
        select,
        *,
        source_panel_message=None,
        target_user_id=None,
        target_user_name=None,
        **kwargs,
    ):
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.title = title
        self.description = description
        self.select = select
        self.source_panel_message = source_panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.message = None
        select.view = self
        select.guild_id = guild_id
        select.owner_id = owner_id
        select.target_user_id = target_user_id
        select.target_user_name = target_user_name


def _carregar_modulo():
    discord = types.ModuleType("discord")
    discord.Interaction = type("Interaction", (), {})
    discord.SelectOption = _SelectOption
    discord.ui = types.SimpleNamespace(Select=_Select)

    common = types.ModuleType("cogs.tts.common")
    common._shorten = lambda text, limit=100: text if len(text) <= limit else text[: limit - 1] + "…"

    visoes = types.ModuleType("cogs.tts.interface.visoes_base")
    visoes.VisaoSelecaoSimples = _VisaoSelecaoSimples

    cogs_pkg = types.ModuleType("cogs")
    cogs_pkg.__path__ = []
    tts_pkg = types.ModuleType("cogs.tts")
    tts_pkg.__path__ = []
    interface_pkg = types.ModuleType("cogs.tts.interface")
    interface_pkg.__path__ = []

    spec = importlib.util.spec_from_file_location("cogs.tts.interface.seletores_basicos", MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "discord": discord,
            "cogs": cogs_pkg,
            "cogs.tts": tts_pkg,
            "cogs.tts.interface": interface_pkg,
            "cogs.tts.common": common,
            "cogs.tts.interface.visoes_base": visoes,
        },
    ):
        spec.loader.exec_module(module)
    return module


class TTSInterfaceSeletoresBasicosComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def test_assinaturas_publicas_legadas_sao_preservaveis(self):
        modulo = _carregar_modulo()
        esperadas = {
            "SeletorModo": ["self", "cog", "server"],
            "SeletorIdioma": ["self", "cog", "server"],
            "SeletorVelocidade": ["self", "cog", "server"],
            "SeletorTom": ["self", "cog", "server"],
            "SeletorRegiaoVoz": ["self", "cog", "server"],
            "SeletorVoz": ["self", "cog", "server", "voices"],
            "SeletorToggle": ["self", "cog", "toggle_name"],
        }
        for nome, parametros in esperadas.items():
            with self.subTest(nome=nome):
                self.assertEqual(list(inspect.signature(getattr(modulo, nome).__init__).parameters), parametros)

    async def test_seletores_de_configuracao_preservam_delegacao_e_contexto(self):
        modulo = _carregar_modulo()
        painel = object()
        view = types.SimpleNamespace(source_panel_message=painel, target_user_id=77, target_user_name="Alvo")
        interaction = object()
        casos = [
            (modulo.SeletorModo, "_apply_mode_from_panel", "edge"),
            (modulo.SeletorIdioma, "_apply_language_from_panel", "pt"),
            (modulo.SeletorVelocidade, "_apply_speed_from_panel", "+25%"),
            (modulo.SeletorTom, "_apply_pitch_from_panel", "+25Hz"),
            (modulo.SeletorVoz, "_apply_voice_from_panel", "pt-BR-FranciscaNeural"),
        ]
        for classe, metodo, valor in casos:
            with self.subTest(classe=classe.__name__):
                aplicar = AsyncMock()
                cog = types.SimpleNamespace(gtts_languages={"pt": "Português"}, **{metodo: aplicar})
                kwargs = {"server": True}
                if classe is modulo.SeletorVoz:
                    kwargs["voices"] = [valor]
                seletor = classe(cog, **kwargs)
                seletor.view = view
                seletor.values = [valor]

                await seletor.callback(interaction)

                aplicar.assert_awaited_once_with(
                    interaction,
                    valor,
                    server=True,
                    source_panel_message=painel,
                    target_user_id=77,
                    target_user_name="Alvo",
                )

    def test_opcoes_de_modo_idioma_velocidade_e_tom_preservam_contrato(self):
        modulo = _carregar_modulo()
        cog = types.SimpleNamespace(gtts_languages={"pt": "Português", "en": "English"})

        modo = modulo.SeletorModo(cog, server=False)
        self.assertEqual([o.value for o in modo.options], ["android_native", "gtts", "edge"])
        self.assertEqual(modo.placeholder, "Escolha o modo de TTS")

        idioma = modulo.SeletorIdioma(cog, server=False)
        self.assertEqual([o.value for o in idioma.options], ["en", "pt"])
        self.assertEqual(idioma.placeholder, "Escolha um idioma do gtts")

        velocidade = modulo.SeletorVelocidade(cog, server=False)
        self.assertEqual([o.value for o in velocidade.options], ["-100%", "-75%", "-50%", "-25%", "+0%", "+25%", "+50%", "+75%", "+100%"])

        tom = modulo.SeletorTom(cog, server=False)
        self.assertEqual([o.value for o in tom.options], ["-100Hz", "-75Hz", "-50Hz", "-25Hz", "+0Hz", "+25Hz", "+50Hz", "+75Hz", "+100Hz"])

    async def test_toggle_preserva_rotas_de_announce_author_e_auto_leave(self):
        modulo = _carregar_modulo()
        painel = object()
        interaction = object()
        for nome, metodo, valor, esperado in [
            ("announce_author", "_apply_announce_author_from_panel", "true", True),
            ("auto_leave", "_apply_auto_leave_from_panel", "false", False),
        ]:
            with self.subTest(nome=nome):
                aplicar = AsyncMock()
                cog = types.SimpleNamespace(**{metodo: aplicar})
                seletor = modulo.SeletorToggle(cog, nome)
                seletor.view = types.SimpleNamespace(source_panel_message=painel)
                seletor.values = [valor]
                await seletor.callback(interaction)
                aplicar.assert_awaited_once_with(interaction, esperado, source_panel_message=painel)

    async def test_regiao_de_voz_preserva_fallback_e_abertura_da_lista(self):
        modulo = _carregar_modulo()
        response = types.SimpleNamespace(send_message=AsyncMock())
        interaction = types.SimpleNamespace(
            user=types.SimpleNamespace(id=10),
            guild=types.SimpleNamespace(id=20),
            response=response,
        )
        cog = types.SimpleNamespace(
            edge_voice_cache=["pt-BR-FranciscaNeural", "pt-BR-AntonioNeural", "pt-PT-DuarteNeural"],
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        seletor = modulo.SeletorRegiaoVoz(cog, server=True)
        seletor.view = types.SimpleNamespace(source_panel_message="painel", target_user_id=30, target_user_name="Alvo")
        seletor.values = ["pt-BR"]

        await seletor.callback(interaction)

        response.send_message.assert_awaited_once()
        kwargs = response.send_message.await_args.kwargs
        view = kwargs["view"]
        self.assertIsInstance(view, _VisaoSelecaoSimples)
        self.assertEqual(view.source_panel_message, "painel")
        self.assertEqual(view.target_user_id, 30)
        self.assertEqual([o.value for o in view.select.options], ["pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"])

        response.send_message.reset_mock()
        seletor.values = ["pt-AO"]
        await seletor.callback(interaction)
        response.send_message.assert_awaited_once()
        self.assertNotIn("view", response.send_message.await_args.kwargs)
        self.assertTrue(response.send_message.await_args.kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()
