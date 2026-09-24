from __future__ import annotations

import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from cogs.musica.agente_telefone import monitor
from cogs.musica.integracoes import tts as tts_integration


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "interface" / "componentes.py"
ROUTER = ROOT / "legado" / "roteador_audio.py"


def _method_source(path: Path, class_name: str, method_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next(
        node for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name
    )
    return ast.get_source_segment(source, method) or ""


def test_queue_acknowledges_discord_before_remote_io_and_does_not_expire_silently() -> None:
    source = COMPONENTS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    queue_cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QueueView")
    init = next(node for node in queue_cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    init_source = ast.get_source_segment(source, init) or ""
    assert "super().__init__(timeout=None)" in init_source

    redraw = _method_source(COMPONENTS, "QueueView", "_redraw")
    play = _method_source(COMPONENTS, "QueueView", "play_selected")
    confirm = _method_source(COMPONENTS, "QueueConfirmView", "confirm")
    move_submit = _method_source(COMPONENTS, "MoveSelectedModal", "on_submit")

    assert redraw.index("await interaction.response.defer()") < redraw.index("await self._prepare_page()")
    assert play.index("await interaction.response.defer") < play.index("await self.router.skip_to")
    assert confirm.index("await interaction.response.defer()") < min(
        pos for pos in (confirm.find("await self.router.replace_queue"), confirm.find("await self.router.remove_at")) if pos >= 0
    )
    assert move_submit.index("await interaction.response.defer") < move_submit.index("await self.router.move")


def test_queue_confirmation_timeout_becomes_visibly_expired() -> None:
    timeout_source = _method_source(COMPONENTS, "QueueConfirmView", "on_timeout")
    assert "Confirmação expirada" in timeout_source
    assert "confirm_message" in timeout_source
    assert "await message.edit" in timeout_source


def test_modal_controls_never_wait_for_worker_health_before_discord_ack() -> None:
    volume = _method_source(COMPONENTS, "VolumeModal", "on_submit")
    seek = _method_source(COMPONENTS, "SeekModal", "on_submit")
    add = _method_source(COMPONENTS, "AddSongModal", "on_submit")
    move = _method_source(COMPONENTS, "MoveSelectedModal", "on_submit")

    for source in (volume, seek, add, move):
        assert "check_worker=False" in source

    assert volume.index("await interaction.response.defer") < volume.index("await self.router.set_volume")
    assert seek.index("await interaction.response.defer") < seek.index("await self.router.seek_to")
    assert add.index("await interaction.response.defer") < add.index("await _extract_batch_for_add_modal")
    assert move.index("await interaction.response.defer") < move.index("await self.router.move")


@pytest.mark.asyncio
async def test_tts_revalidates_authoritative_music_route_before_allowing_local_voice(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(tts_integration, "deve_bloquear_voz_tts_local", lambda *args, **kwargs: False)

    async def revalidate(bot, guild_id: int, channel_id: int) -> bool:
        calls.append((guild_id, channel_id))
        return True

    monkeypatch.setattr(tts_integration, "revalidar_rota_tts_agente", revalidate)

    owner = SimpleNamespace(bot=object())
    guild = SimpleNamespace(id=77)
    item = SimpleNamespace(channel_id=88)
    handled, voice_client = await tts_integration.preparar_cliente_voz_tts(owner, guild, item, SimpleNamespace(), None)

    assert calls == [(77, 88)]
    assert handled is True
    assert voice_client is None


def test_tts_keeps_remote_ownership_when_monitor_is_dead_and_mirror_channel_may_be_stale() -> None:
    source = _method_source(ROUTER, "AudioRouter", "should_route_tts_to_music_agent")
    assert "monitor_dead = bool(monitor_task is None or monitor_task.done())" in source
    assert "or monitor_dead" in source
    # Em divergência de canal, o monitor degradado/morto preserva a posse remota
    # em vez de liberar um VoiceClient local concorrente.
    assert "requested_channel != remembered_channel" in source
    assert "return uncertain" in source


@pytest.mark.asyncio
async def test_active_monitor_survives_failure_threshold_and_forces_full_recovery_snapshot(monkeypatch) -> None:
    real_sleep = asyncio.sleep
    old_track = SimpleNamespace(title="Antiga", queue_item_id="old")
    state = SimpleNamespace(
        agent_monitor_task=None,
        now_message=object(),
        current_backend="agent",
        current_status="playing",
        current_status_detail="playing",
        current=old_track,
        music_session_active=True,
        agent_playback_token=10,
        agent_voice_session_mode="music_active",
        agent_monitor_failures=0,
        agent_monitor_last_error="",
        agent_monitor_reconnecting_since=0.0,
        agent_monitor_recoveries=0,
        agent_monitor_last_cycle_at=0.0,
        agent_monitor_last_success_at=0.0,
        agent_monitor_last_full_sync_at=0.0,
        agent_monitor_restart_count=0,
    )
    calls = 0
    known_revisions: list[str] = []
    synced: list[tuple[str, int]] = []
    status_sync_reasons: list[str] = []

    class Router:
        def get_state(self, guild_id):
            return state

        def _set_current_status(self, st, status):
            st.current_status = status

        async def update_panel(self, guild_id, *, create=True, repost=False):
            return None

        async def sync_music_agent_state(self, guild_id, track, remote, **kwargs):
            status = str(remote.get("status") or "idle")
            token = int(remote.get("playback_token") or 0)
            synced.append((status, token))
            state.current_backend = "agent"
            state.current_status = status
            state.agent_playback_token = token
            current = remote.get("current")
            if current:
                state.current = SimpleNamespace(
                    title=str(current.get("title") or ""),
                    queue_item_id=str(current.get("queue_item_id") or ""),
                )
                state.music_session_active = True
            else:
                state.current = None
                state.music_session_active = False

        def _mark_voice_status_track_change(self, st):
            return None

        def _schedule_voice_status_track_sync(self, guild_id, *, repeat_after=0.0, reason=""):
            status_sync_reasons.append(reason)

    async def status_fake(**kwargs):
        nonlocal calls
        calls += 1
        known_revisions.append(str(kwargs.get("known_revision") or ""))
        if calls <= 4:
            return {"ok": False, "available": False, "error": "route down"}
        if calls == 5:
            return {
                "ok": True,
                "available": True,
                "guilds": {
                    "555": {
                        "status": "playing",
                        "confirmed_playing": True,
                        "voice_connected": True,
                        "player_present": True,
                        "state_revision": "new-rev",
                        "playback_token": 11,
                        "current": {
                            "title": "Nova",
                            "queue_item_id": "new",
                            "webpage_url": "https://example.invalid/new",
                        },
                        "queue": [],
                        "queue_size": 0,
                    }
                },
            }
        return {
            "ok": True,
            "available": True,
            "guilds": {
                "555": {
                    "status": "idle",
                    "state_revision": f"idle-{calls}",
                    "playback_token": 11,
                    "current": None,
                    "queue": [],
                    "queue_size": 0,
                }
            },
        }

    async def yield_sleep(delay):
        await real_sleep(0)

    monkeypatch.setattr(monitor, "music_agent_status", status_fake)
    monkeypatch.setattr(monitor, "schedule_playlist_refill_if_needed", lambda *args, **kwargs: None)
    monkeypatch.setattr(monitor, "desvincular_guild_worker", lambda guild_id: None)
    monkeypatch.setattr(monitor.asyncio, "sleep", yield_sleep)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_MONITOR_UI_FAILURES", 2, raising=False)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_MONITOR_REBIND_FAILURES", 2, raising=False)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_MONITOR_MAX_FAILURES", 3, raising=False)
    monkeypatch.setattr(monitor.config, "MUSIC_AGENT_PANEL_REFRESH_SECONDS", 300.0, raising=False)

    router = Router()
    monitor.iniciar_monitor_music_agent(router, 555)
    task = state.agent_monitor_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)

    # Quatro falhas ultrapassam o threshold=3, mas a sessão ativa não perde o watcher.
    assert calls >= 8
    assert ("playing", 11) in synced
    # Todo poll durante/na primeira recuperação precisa ignorar revisão antiga.
    assert known_revisions[:5] == ["", "", "", "", ""]
    assert state.agent_monitor_recoveries == 1
    assert state.agent_monitor_last_success_at > 0
    assert state.agent_monitor_last_full_sync_at > 0
    assert "agent_monitor_reconcile" in status_sync_reasons

@pytest.mark.asyncio
async def test_tts_ok_false_from_agent_is_not_silently_consumed(monkeypatch) -> None:
    monkeypatch.setattr(tts_integration, "deve_rotear_tts_para_agente", lambda *args, **kwargs: True)
    monkeypatch.setattr(tts_integration, "suporta_cache_tts_agente", lambda *args, **kwargs: False)
    monkeypatch.setattr(tts_integration, "musica_ativa", lambda *args, **kwargs: True)

    async def play(*args, **kwargs):
        return {
            "ok": False,
            "tts_agent_route": True,
            "worker_result": {"ok": False, "error": "sem sessão musical ativa no worker"},
        }

    monkeypatch.setattr(tts_integration, "tocar_tts_via_agente", play)

    class Owner:
        bot = object()

        def _estimate_playback_timeout(self, item):
            return 5.0

    item = SimpleNamespace(
        channel_id=88,
        text="oi",
        engine="gtts",
        voice="",
        language="pt-br",
        rate="+0%",
        pitch="+0Hz",
        enqueued_at_monotonic=1.0,
        _dequeued_at_monotonic=1.0,
        _tts_remote_active=False,
    )

    consumed, task = await tts_integration.rotear_item_tts_para_musica(
        Owner(), SimpleNamespace(id=77), item, None
    )
    assert consumed is False
    assert task is None
    assert item._skip_music_agent_tts_route is True
