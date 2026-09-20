from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cogs.musica.integracoes.status_canal import VoiceStatusController


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
    def __init__(self, *, fetch_results=None, idle=""):
        self.channel = FakeChannel()
        self.guild = FakeGuild(self.channel)
        self.bot = FakeBot(self.guild, self.channel)
        self.track = SimpleNamespace(title="A")
        self.state = FakeState(self.track)
        self.fetch_results = list(fetch_results or [(False, "")])
        self.idle = idle
        self.set_calls = []
        self.saved = None
        self.clears = 0
        self._voice_status_update_interval_seconds = 60.0

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
