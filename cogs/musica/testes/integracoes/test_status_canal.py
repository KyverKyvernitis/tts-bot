from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cogs.musica.integracoes.status_canal import VoiceStatusController, instalar_ponte_gateway_status_canal


class FakeState:
    def __init__(self, track=None):
        self.current = track
        self.current_status = "playing"
        self.last_voice_channel_id = 22
        self.voice_status_channel_id = None
        self.voice_status_had_original = False
        self.voice_status_original_known = False
        self.voice_status_original = ""
        self.voice_status_owned = False
        self.voice_status_last_bot = ""
        self.voice_status_update_task = None
        self.voice_status_last_update_at = 0.0
        self.voice_status_last_track_key = ""
        self.voice_status_last_applied_key = ""
        self.voice_status_last_sync_request_key = ""
        self.voice_status_last_sync_request_at = 0.0
        self.voice_status_last_restore_key = ""
        self.voice_status_last_restore_at = 0.0
        self.voice_status_force_task = None
        self.voice_status_generation = 0
        self.voice_status_external_override = False
        self.voice_status_external_status = ""
        self.voice_status_gateway_status_known = False
        self.voice_status_gateway_status = ""
        self.voice_status_gateway_event_at = 0.0
        self.voice_status_expected_status = ""
        self.voice_status_expected_until = 0.0
        self.voice_status_last_write_at = 0.0
        self.voice_status_retry_count = 0
        self.voice_status_pause_position_seconds = -1.0
        self.voice_status_lock = asyncio.Lock()


class FakeChannel:
    def __init__(self, channel_id=22):
        self.id = channel_id


class FakeGuild:
    def __init__(self, channel):
        self.id = 11
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel if int(channel_id) == self._channel.id else None


class FakeBot:
    def __init__(self, guild, channel):
        self.guild = guild
        self.channel = channel

    def get_guild(self, guild_id):
        return self.guild if int(guild_id) == self.guild.id else None

    def get_channel(self, channel_id):
        return self.channel if int(channel_id) == self.channel.id else None


class FakeRouter:
    def __init__(self, *, fetch_results=None, idle="", set_results=None):
        self.channel = FakeChannel()
        self.guild = FakeGuild(self.channel)
        self.bot = FakeBot(self.guild, self.channel)
        self.track = SimpleNamespace(title="A")
        self.state = FakeState(self.track)
        self.fetch_results = list(fetch_results or [(False, "")])
        self.idle = idle
        self.set_calls = []
        self.set_results = list(set_results or [])
        self.saved = None
        self.clears = 0
        self._states = {self.guild.id: self.state}
        self._voice_status_update_interval_seconds = 60.0
        self._voice_status_watchdog_interval_seconds = 45.0
        self._voice_status_reassert_seconds = 240.0
        self._voice_status_write_retries = 3
        self._voice_status_retry_base_seconds = 0.001
        self._voice_status_gateway_ack_seconds = 8.0
        self._voice_status_last_http_status = 0
        self._voice_status_last_retry_after_seconds = 0.0

    def get_state(self, _guild_id):
        return self.state

    def _voice_status_settings_from_doc(self, _guild_id):
        return {"enabled": True, "template": "{title}", "idle": self.idle}

    def _bot_can_set_voice_status(self, _guild, _channel):
        return True

    def _voice_status_track_key(self, track):
        return str(getattr(track, "title", ""))

    def _voice_status_track_is_current(self, state, track, track_key):
        return state.current is track or self._voice_status_track_key(state.current) == track_key

    def render_voice_status(self, _guild_id, track, *, template=None):
        return str(getattr(track, "title", ""))

    def _trim_voice_status(self, value):
        return " ".join(str(value or "").split())[:500]

    async def _fetch_voice_channel_status(self, _channel):
        if self.fetch_results:
            value = self.fetch_results.pop(0)
            if callable(value):
                return await value()
            return value
        return False, ""

    async def _set_voice_channel_status(self, channel, status, *, reason=""):
        self.set_calls.append((channel.id, status, reason))
        self._voice_status_last_http_status = 0
        self._voice_status_last_retry_after_seconds = 0.0
        if self.set_results:
            result = self.set_results.pop(0)
            if isinstance(result, tuple):
                ok, http_status, retry_after = result
                self._voice_status_last_http_status = int(http_status or 0)
                self._voice_status_last_retry_after_seconds = float(retry_after or 0.0)
                return bool(ok)
            return bool(result)
        return True

    async def _save_voice_status_record(self, _guild_id, record):
        self.saved = dict(record)
        return True

    async def _clear_voice_status_record(self, _guild_id):
        self.clears += 1
        self.saved = None

    def _load_voice_status_record_into_state(self, _guild_id, state):
        if not self.saved:
            return None
        record = dict(self.saved)
        state.voice_status_channel_id = record.get("channel_id")
        state.voice_status_had_original = bool(record.get("had_original_status"))
        state.voice_status_original_known = bool(record.get("original_known_status"))
        state.voice_status_original = str(record.get("original_status") or "")
        state.voice_status_owned = bool(record.get("owned_by_bot"))
        state.voice_status_last_bot = str(record.get("last_bot_status") or "")
        state.voice_status_last_track_key = str(record.get("last_track_key") or "")
        return record


@pytest.mark.asyncio
async def test_restore_limpa_status_mesmo_quando_get_nao_consegue_ler() -> None:
    router = FakeRouter(fetch_results=[(False, ""), (False, "")], idle="")
    controller = VoiceStatusController(router)

    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)
    assert [call[1] for call in router.set_calls] == ["A"]
    assert router.state.voice_status_owned is True

    await controller.restore(router.guild, router.state, reason="queue_finished")

    assert [call[1] for call in router.set_calls] == ["A", ""]
    assert router.state.voice_status_channel_id is None
    assert router.clears == 1


@pytest.mark.asyncio
async def test_restore_respeita_override_externo_conhecido() -> None:
    router = FakeRouter(fetch_results=[(True, "original"), (True, "staff mudou")])
    controller = VoiceStatusController(router)

    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)
    await controller.restore(router.guild, router.state, reason="manual_stop")

    assert [call[1] for call in router.set_calls] == ["A"]
    assert router.clears == 1


@pytest.mark.asyncio
async def test_write_antigo_e_descartado_quando_geracao_muda_durante_fetch() -> None:
    release = asyncio.Event()

    async def delayed_fetch():
        await release.wait()
        return False, ""

    router = FakeRouter(fetch_results=[delayed_fetch])
    controller = VoiceStatusController(router)
    task = asyncio.create_task(
        controller.apply(
            router.guild,
            router.channel,
            router.state,
            router.track,
            force=True,
            generation=0,
        )
    )
    await asyncio.sleep(0)
    controller._bump_generation(router.state, reason="new_track")
    release.set()
    await task

    assert router.set_calls == []


@pytest.mark.asyncio
async def test_retry_nao_repete_put_quando_status_ja_foi_aplicado() -> None:
    router = FakeRouter(fetch_results=[(False, ""), (True, "A")])
    controller = VoiceStatusController(router)

    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)
    await controller.apply(router.guild, router.channel, router.state, router.track, force=False)

    assert [call[1] for call in router.set_calls] == ["A"]


def test_set_voice_status_do_roteador_limpa_com_null() -> None:
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[4]
    source = (raiz / "cogs" / "musica" / "legado" / "roteador_audio.py").read_text(encoding="utf-8")
    assert 'json={"status": payload_status or None}' in source
    assert 'json={"status": ""}' not in source

@pytest.mark.asyncio
async def test_music_agent_mesma_faixa_nova_geracao_reemite_track_started() -> None:
    from cogs.musica.nucleo.estado import MusicGuildState
    from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente

    state = MusicGuildState()
    started: list[tuple[int, str]] = []

    class Router:
        def get_state(self, _guild_id):
            return state

        @staticmethod
        def _panel_key_for_track(track):
            return str(getattr(track, "title", "") or "") if track is not None else ""

        @staticmethod
        def _set_current_status(st, status):
            st.current_status = status

        @staticmethod
        def _reactivate_panel_controls_now(_guild_id):
            return None

        @staticmethod
        def _schedule_agent_playback_started_effects(guild_id, key):
            started.append((guild_id, key))

        @staticmethod
        def start_music_agent_monitor(*_args, **_kwargs):
            return None

        async def update_panel(self, *_args, **_kwargs):
            return None

    base = {
        "status": "playing",
        "confirmed_playing": True,
        "voice_connected": True,
        "player_present": True,
        "voice_channel_id": 22,
        "text_channel_id": 33,
        "current": {
            "title": "Faixa repetida",
            "webpage_url": "https://example.invalid/a",
            "requester_id": 1,
            "requester_name": "Core",
            "duration": 120,
            "source": "worker-agent",
        },
        "queue": [],
        "queue_size": 0,
    }

    await sincronizar_estado_agente(Router(), 11, agent_state={**base, "playback_token": 7}, create_panel=False)
    await sincronizar_estado_agente(Router(), 11, agent_state={**base, "playback_token": 8}, create_panel=False)

    assert started == [(11, "Faixa repetida"), (11, "Faixa repetida")]
    assert state.agent_playback_token == 8


def test_music_agent_publica_playback_token_no_status() -> None:
    from cogs.musica.runtime_telefone.agente.estado import GuildMusicState

    state = GuildMusicState(guild_id=11)
    state.playback_token = 42
    assert state.public()["playback_token"] == 42


@pytest.mark.asyncio
async def test_gateway_ack_do_proprio_bot_mantem_ownership() -> None:
    router = FakeRouter(fetch_results=[(False, "")])
    controller = VoiceStatusController(router)

    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)
    await controller.handle_gateway_update(router.guild.id, router.channel.id, "A")

    assert router.state.voice_status_owned is True
    assert router.state.voice_status_external_override is False
    assert router.state.voice_status_gateway_status_known is True
    assert router.state.voice_status_gateway_status == "A"
    assert router.clears == 0


@pytest.mark.asyncio
async def test_gateway_override_externo_libera_ownership_e_bloqueia_trocas() -> None:
    router = FakeRouter(fetch_results=[(False, "")])
    controller = VoiceStatusController(router)

    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)
    await controller.handle_gateway_update(router.guild.id, router.channel.id, "status do staff")

    assert router.state.voice_status_owned is False
    assert router.state.voice_status_external_override is True
    assert router.state.voice_status_external_status == "status do staff"
    assert router.saved is None

    router.track = SimpleNamespace(title="B")
    router.state.current = router.track
    generation = controller._bump_generation(router.state, reason="track:B")
    await controller.apply(router.guild, router.channel, router.state, router.track, force=True, generation=generation)

    assert [call[1] for call in router.set_calls] == ["A"]


@pytest.mark.asyncio
async def test_fim_da_sessao_apos_override_nao_apaga_status_do_staff() -> None:
    router = FakeRouter(fetch_results=[(False, "")])
    controller = VoiceStatusController(router)

    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)
    await controller.handle_gateway_update(router.guild.id, router.channel.id, "status do staff")
    await controller.restore(router.guild, router.state, reason="queue_finished")

    assert [call[1] for call in router.set_calls] == ["A"]
    assert router.state.voice_status_channel_id is None
    assert router.state.voice_status_external_override is False


@pytest.mark.asyncio
async def test_write_transitorio_retries_sem_perder_generation() -> None:
    router = FakeRouter(fetch_results=[(False, "")], set_results=[False, False, True])
    controller = VoiceStatusController(router)

    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)

    assert [call[1] for call in router.set_calls] == ["A", "A", "A"]
    assert router.state.voice_status_owned is True
    assert router.state.voice_status_retry_count == 2



def test_integracao_instala_parser_especifico_sem_debug_global() -> None:
    dispatched = []

    class Bot:
        def __init__(self):
            self._connection = SimpleNamespace(parsers={})

        def dispatch(self, name, payload):
            dispatched.append((name, payload))

    bot = Bot()
    assert instalar_ponte_gateway_status_canal(bot) is True

    parser = bot._connection.parsers["VOICE_CHANNEL_STATUS_UPDATE"]
    parser({"guild_id": "11", "id": "22", "status": "x"})

    assert dispatched == [("music_voice_channel_status_update_raw", {"guild_id": "11", "id": "22", "status": "x"})]


def test_integracao_nao_sobrescreve_parser_nativo() -> None:
    native = lambda data: data
    bot = SimpleNamespace(_connection=SimpleNamespace(parsers={"VOICE_CHANNEL_STATUS_UPDATE": native}))

    assert instalar_ponte_gateway_status_canal(bot) is False
    assert bot._connection.parsers["VOICE_CHANNEL_STATUS_UPDATE"] is native


@pytest.mark.asyncio
async def test_rate_limit_429_respeita_retry_after_e_registra_metricas() -> None:
    router = FakeRouter(fetch_results=[(False, "")], set_results=[(False, 429, 0.002), True])
    controller = VoiceStatusController(router)
    started = asyncio.get_running_loop().time()
    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)
    elapsed = asyncio.get_running_loop().time() - started

    assert [call[1] for call in router.set_calls] == ["A", "A"]
    assert elapsed >= 0.09
    metrics = controller.metrics_snapshot(router.guild.id)
    assert metrics["rate_limited"] == 1
    assert metrics["retries"] == 1
    assert metrics["write_success"] == 1


@pytest.mark.asyncio
async def test_http_403_falha_sem_retries_inuteis() -> None:
    router = FakeRouter(fetch_results=[(False, "")], set_results=[(False, 403, 0.0), True])
    controller = VoiceStatusController(router)

    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)

    assert len(router.set_calls) == 1
    metrics = controller.metrics_snapshot(router.guild.id)
    assert metrics["permanent_http_failures"] == 1
    assert metrics.get("retries", 0) == 0


def test_render_status_expoe_placeholders_de_estado_e_posicao_congelada() -> None:
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[4]
    source = (raiz / "cogs" / "musica" / "legado" / "roteador_audio.py").read_text(encoding="utf-8")
    assert '"position": elapsed' in source
    assert '"state": state_label' in source
    assert '"paused": "⏸️"' in source
    assert '"loop": str(loop_mode or "off")' in source
    assert '"volume": f"{' in source
    assert 'voice_status_pause_position_seconds' in source


@pytest.mark.asyncio
async def test_music_agent_pause_resume_dispara_status_sem_trocar_faixa() -> None:
    from cogs.musica.nucleo.estado import MusicGuildState
    from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente

    state = MusicGuildState()
    scheduled: list[str] = []

    class Router:
        def get_state(self, _guild_id):
            return state

        @staticmethod
        def _panel_key_for_track(track):
            return str(getattr(track, "title", "") or "") if track is not None else ""

        @staticmethod
        def _set_current_status(st, status):
            st.current_status = status

        @staticmethod
        def _reactivate_panel_controls_now(_guild_id):
            return None

        @staticmethod
        def _schedule_agent_playback_started_effects(_guild_id, _key):
            return None

        @staticmethod
        def _mark_voice_status_track_change(_state):
            return None

        @staticmethod
        def _schedule_voice_status_track_sync(_guild_id, *, repeat_after=0.0, reason=""):
            scheduled.append(reason)

        @staticmethod
        def start_music_agent_monitor(*_args, **_kwargs):
            return None

        async def update_panel(self, *_args, **_kwargs):
            return None

    base = {
        "confirmed_playing": True,
        "voice_connected": True,
        "player_present": True,
        "voice_channel_id": 22,
        "text_channel_id": 33,
        "playback_token": 9,
        "position_ms": 30_000,
        "current": {
            "title": "Faixa",
            "webpage_url": "https://example.invalid/a",
            "requester_id": 1,
            "requester_name": "Core",
            "duration": 120,
            "source": "worker-agent",
        },
        "queue": [],
        "queue_size": 0,
    }

    await sincronizar_estado_agente(Router(), 11, agent_state={**base, "status": "playing"}, create_panel=False)
    scheduled.clear()
    await sincronizar_estado_agente(
        Router(), 11, agent_state={**base, "status": "paused", "confirmed_playing": False, "position_ms": 31_000}, create_panel=False
    )
    assert scheduled == ["agent_pause"]
    assert state.voice_status_pause_position_seconds == pytest.approx(31.0)

    scheduled.clear()
    await sincronizar_estado_agente(
        Router(), 11, agent_state={**base, "status": "playing", "position_ms": 31_000}, create_panel=False
    )
    assert scheduled == ["agent_resume"]
    assert state.voice_status_pause_position_seconds == -1.0
    assert state.current_start_offset_seconds == pytest.approx(31.0)


def test_painel_documenta_placeholders_novos_e_metricas() -> None:
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[4]
    source = (raiz / "cogs" / "musica" / "interface" / "componentes.py").read_text(encoding="utf-8")
    for token in ("{position}", "{state}", "{paused}", "{loop}", "{volume}"):
        assert token in source
    assert "Diagnóstico: writes=" in source

@pytest.mark.asyncio
async def test_restart_restaura_registro_persistido_e_limpa_ownership() -> None:
    router = FakeRouter(fetch_results=[(True, "A")])
    router.saved = {
        "channel_id": router.channel.id,
        "had_original_status": True,
        "original_known_status": True,
        "original_status": "original",
        "owned_by_bot": True,
        "last_bot_status": "A",
        "last_track_key": "A",
    }
    controller = VoiceStatusController(router)
    router.state.voice_status_channel_id = None

    await controller.restore(router.guild, router.state, reason="restart")

    assert [call[1] for call in router.set_calls] == ["original"]
    assert router.saved is None
    assert router.state.voice_status_channel_id is None
    assert controller.metrics_snapshot(router.guild.id)["restores"] == 1


@pytest.mark.asyncio
async def test_reconcile_mesma_faixa_sem_mudanca_deduplica_put() -> None:
    router = FakeRouter(fetch_results=[(False, "")])
    controller = VoiceStatusController(router)

    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)
    await controller.apply(router.guild, router.channel, router.state, router.track, force=False)

    assert [call[1] for call in router.set_calls] == ["A"]
    assert controller.metrics_snapshot(router.guild.id)["deduplicated"] >= 1

@pytest.mark.asyncio
async def test_rate_limit_429_nao_trunca_retry_after_longo(monkeypatch) -> None:
    router = FakeRouter(fetch_results=[(False, "")], set_results=[(False, 429, 42.0), True])
    controller = VoiceStatusController(router)
    controller.schedule_refresh = lambda *_args, **_kwargs: None
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(float(delay))

    monkeypatch.setattr("cogs.musica.integracoes.status_canal.asyncio.sleep", fake_sleep)
    await controller.apply(router.guild, router.channel, router.state, router.track, force=True)

    assert sleeps == [pytest.approx(42.0)]
    assert [call[1] for call in router.set_calls] == ["A", "A"]


def _audio_router_method_source(method_name: str) -> str:
    import ast
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[4]
    source = (raiz / "cogs" / "musica" / "legado" / "roteador_audio.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "AudioRouter":
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None
                    return segment
    raise AssertionError(f"método AudioRouter.{method_name} não encontrado")


def test_stop_restaura_status_antes_de_desconectar() -> None:
    source = _audio_router_method_source("stop")
    restore = source.index("await self._restore_music_session_side_effects")
    disconnect = source.index("await vc.disconnect(force=True)")
    assert restore < disconnect
    assert 'reason="manual_stop"' in source


def test_music_afk_restaura_status_antes_de_desconectar() -> None:
    source = _audio_router_method_source("_disconnect_music_afk")
    restore = source.index("await self._restore_music_session_side_effects")
    disconnect = source.index("await vc.disconnect(force=False)")
    assert restore < disconnect
    assert 'reason="music_afk"' in source


def test_disconnect_do_agent_finaliza_todos_os_caminhos_idle() -> None:
    source = _audio_router_method_source("handle_bot_voice_disconnect")
    assert 'reason="agent_idle"' in source
    assert 'reason="agent_natural_end"' in source
    assert 'reason="external_disconnect"' in source
    natural = source.index('reason="agent_natural_end"')
    natural_return = source.index("return", natural)
    assert natural < natural_return


def test_idle_sem_voice_client_nao_deixa_status_preso() -> None:
    source = _audio_router_method_source("_maybe_disconnect_idle")
    assert 'reason="idle_disconnected"' in source
    restore = source.index("await self._restore_music_session_side_effects")
    update = source.index("await self.update_panel", restore)
    assert restore < update


def test_finalizador_central_restaura_bitrate_e_status() -> None:
    source = _audio_router_method_source("_restore_music_session_side_effects")
    assert "await self._restore_auto_bitrate_for_state" in source
    assert "await self._restore_voice_status_for_state" in source


@pytest.mark.asyncio
async def test_music_agent_recovery_mesma_faixa_preserva_posicao_visual() -> None:
    from cogs.musica.nucleo.estado import MusicGuildState
    from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente

    state = MusicGuildState()

    class Router:
        def get_state(self, _guild_id):
            return state

        @staticmethod
        def _panel_key_for_track(track):
            return str(getattr(track, "title", "") or "") if track is not None else ""

        @staticmethod
        def _set_current_status(st, status):
            st.current_status = status

        @staticmethod
        def _reactivate_panel_controls_now(_guild_id):
            return None

        @staticmethod
        def _schedule_agent_playback_started_effects(_guild_id, _key):
            return None

        @staticmethod
        def _mark_voice_status_track_change(_state):
            return None

        @staticmethod
        def _schedule_voice_status_track_sync(_guild_id, **_kwargs):
            return None

        @staticmethod
        def start_music_agent_monitor(*_args, **_kwargs):
            return None

        async def update_panel(self, *_args, **_kwargs):
            return None

    base = {
        "status": "playing",
        "confirmed_playing": True,
        "voice_connected": True,
        "player_present": True,
        "voice_channel_id": 22,
        "text_channel_id": 33,
        "current": {
            "title": "Faixa recuperada",
            "webpage_url": "https://example.invalid/a",
            "requester_id": 1,
            "duration": 180,
            "source": "worker-agent",
        },
        "queue": [],
        "queue_size": 0,
    }

    await sincronizar_estado_agente(
        Router(), 11, agent_state={**base, "playback_token": 10, "position_ms": 42_000}, create_panel=False
    )
    # Mesmo no primeiro bind da VPS, o Worker já pode estar no meio da faixa.
    assert state.current_start_offset_seconds == pytest.approx(42.0)

    # O mesmo track volta com token novo após reconexão e o Worker informa a
    # posição autoritativa. A VPS deve rebasear o relógio nessa posição, não 0:00.
    await sincronizar_estado_agente(
        Router(),
        11,
        agent_state={
            **base,
            "playback_token": 11,
            "position_ms": 47_500,
            "voice_runtime_recovery_pending": False,
            "voice_runtime_recovery_attempts": 2,
        },
        create_panel=False,
    )
    assert state.current_start_offset_seconds == pytest.approx(47.5)
    assert state.agent_voice_recovery_attempts == 2


@pytest.mark.asyncio
async def test_music_agent_espelha_auditoria_de_recovery_de_voz() -> None:
    from cogs.musica.nucleo.estado import MusicGuildState
    from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente

    state = MusicGuildState()

    class Router:
        def get_state(self, _guild_id): return state
        @staticmethod
        def _panel_key_for_track(track): return str(getattr(track, "title", "") or "") if track else ""
        @staticmethod
        def _set_current_status(st, status): st.current_status = status
        @staticmethod
        def _reactivate_panel_controls_now(_guild_id): return None
        @staticmethod
        def _schedule_agent_playback_started_effects(_guild_id, _key): return None
        @staticmethod
        def _mark_voice_status_track_change(_state): return None
        @staticmethod
        def _schedule_voice_status_track_sync(_guild_id, **_kwargs): return None
        @staticmethod
        def start_music_agent_monitor(*_args, **_kwargs): return None
        async def update_panel(self, *_args, **_kwargs): return None

    await sincronizar_estado_agente(
        Router(),
        11,
        agent_state={
            "status": "preparing",
            "playback_token": 20,
            "voice_runtime_recovery_pending": True,
            "voice_runtime_recovery_attempts": 3,
            "voice_runtime_recovery_last_error": "VoiceSessionError: rota caiu",
            "current": {
                "title": "Faixa",
                "webpage_url": "https://example.invalid/a",
                "requester_id": 1,
                "source": "worker-agent",
            },
            "queue": [],
            "queue_size": 0,
        },
        create_panel=False,
    )

    assert state.agent_voice_recovery_pending is True
    assert state.agent_voice_recovery_attempts == 3
    assert "rota caiu" in state.agent_voice_recovery_last_error
    assert state.current_status_detail == "voice_recovery:3"


@pytest.mark.asyncio
async def test_music_agent_playing_nao_confirmado_nao_dispara_inicio_em_payload_atual() -> None:
    from cogs.musica.nucleo.estado import MusicGuildState
    from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente

    state = MusicGuildState()
    started: list[str] = []

    class Router:
        def get_state(self, _guild_id): return state
        @staticmethod
        def _panel_key_for_track(track): return str(getattr(track, "title", "") or "") if track else ""
        @staticmethod
        def _set_current_status(st, status): st.current_status = status
        @staticmethod
        def _reactivate_panel_controls_now(_guild_id): return None
        @staticmethod
        def _schedule_agent_playback_started_effects(_guild_id, key): started.append(key)
        @staticmethod
        def _mark_voice_status_track_change(_state): return None
        @staticmethod
        def _schedule_voice_status_track_sync(_guild_id, **_kwargs): return None
        @staticmethod
        def start_music_agent_monitor(*_args, **_kwargs): return None
        async def update_panel(self, *_args, **_kwargs): return None

    await sincronizar_estado_agente(
        Router(),
        11,
        agent_state={
            "status": "playing",
            "confirmed_playing": False,
            "voice_connected": False,
            "player_present": True,
            "playback_token": 7,
            "position_ms": 12_000,
            "current": {
                "title": "Faixa ainda reconectando",
                "webpage_url": "https://example.invalid/a",
                "requester_id": 1,
                "source": "worker-agent",
            },
            "queue": [],
            "queue_size": 0,
        },
        create_panel=False,
    )

    assert state.current_status == "starting"
    assert started == []
    assert state.agent_started_playback_token == -1


@pytest.mark.asyncio
async def test_music_agent_payload_legado_sem_confirmacao_ainda_pode_sinalizar_playing() -> None:
    from cogs.musica.nucleo.estado import MusicGuildState
    from cogs.musica.reproducao.sincronizacao import sincronizar_estado_agente

    state = MusicGuildState()
    started: list[str] = []

    class Router:
        def get_state(self, _guild_id): return state
        @staticmethod
        def _panel_key_for_track(track): return str(getattr(track, "title", "") or "") if track else ""
        @staticmethod
        def _set_current_status(st, status): st.current_status = status
        @staticmethod
        def _reactivate_panel_controls_now(_guild_id): return None
        @staticmethod
        def _schedule_agent_playback_started_effects(_guild_id, key): started.append(key)
        @staticmethod
        def _mark_voice_status_track_change(_state): return None
        @staticmethod
        def _schedule_voice_status_track_sync(_guild_id, **_kwargs): return None
        @staticmethod
        def start_music_agent_monitor(*_args, **_kwargs): return None
        async def update_panel(self, *_args, **_kwargs): return None

    await sincronizar_estado_agente(
        Router(),
        11,
        agent_state={
            "status": "playing",
            "playback_token": 2,
            "position_ms": 9_000,
            "current": {
                "title": "Agente legado",
                "webpage_url": "https://example.invalid/a",
                "requester_id": 1,
                "source": "worker-agent",
            },
            "queue": [],
            "queue_size": 0,
        },
        create_panel=False,
    )

    assert state.current_status == "starting" or state.current_status == "playing"
    assert started == ["Agente legado"]
    assert state.current_start_offset_seconds == pytest.approx(9.0)
