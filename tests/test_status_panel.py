"""Mobile status pages use the actual discord.py renderer and components."""
from __future__ import annotations

from dataclasses import replace
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utility.status.data import (
    StatusSnapshot,
    StorageSnapshot,
    collect_engines,
    collect_servers,
)
from utility.status.render import (
    PAGE_TEXT_BUDGET,
    ROWS_PER_PAGE,
    panel_pages,
    safe_text,
    server_pages,
    tts_pages,
)
from utility.status.views import StatusView


def snapshot_for(count=0, *, metrics=None, names=None, members=10,
                 stats_available=True, tts_available=True):
    metrics = metrics if metrics is not None else {}
    guilds = [SimpleNamespace(id=index + 1, name=names[index] if names else f"Server {index + 1:04}",
                              member_count=members, members=[],
                              icon=SimpleNamespace(url=f"https://example.test/{index + 1}.png"))
              for index in range(count)]
    stats = {index + 1: {"edge": index + 1} for index in range(count)}
    servers = collect_servers(SimpleNamespace(guilds=guilds), stats, stats_available=stats_available)
    return StatusSnapshot(
        collected_at=1700000000,
        servers=servers,
        servers_available=True,
        stats_available=stats_available,
        tts_available=tts_available,
        tts=metrics,
        engines=collect_engines(metrics),
        storage=StorageSnapshot(1700000000, 0, 0, 0, 0),
        thumbnail_url="https://example.test/bot.png",
    )


def text_of(pages):
    return "\n".join(page.header + "\n" + "\n".join(card.text for card in page.cards) + "\n" + page.footer
                     for page in pages)


def assert_page_budget(pages):
    for page in pages:
        card_text_length = sum(len(card.text) for card in page.cards)
        assert len(page.cards) <= ROWS_PER_PAGE
        assert card_text_length <= PAGE_TEXT_BUDGET
        assert len(page.header) + card_text_length + len(page.footer) <= 4000


@pytest.mark.parametrize("count, expected_pages", [(0, 1), (1, 1), (5, 1), (6, 2), (103, 21)])
def test_every_server_is_accessible_once_in_paginated_cards(count, expected_pages):
    snapshot = snapshot_for(count)
    pages = server_pages(snapshot)

    assert len(pages) == expected_pages
    assert_page_budget(pages)
    if not count:
        assert "Nenhum servidor" in pages[0].cards[0].text
        return
    cards = [card for page in pages for card in page.cards]
    assert len(cards) == count
    for index, card in enumerate(cards, 1):
        assert f"**{index}. Server {index:04}**" in card.text
    for index, page in enumerate(pages, 1):
        assert f"Página {index}/{expected_pages}" in page.footer


def test_long_server_names_are_bounded_and_markdown_and_mentions_are_escaped():
    dangerous_name = "@everyone @here <@123456789012345678> **bold** _name_ " + "X" * 1000
    snapshot = snapshot_for(6, names=[dangerous_name + str(index) for index in range(6)])
    pages = server_pages(snapshot)
    cards = [card for page in pages for card in page.cards]

    assert len(cards) == 6
    assert_page_budget(pages)
    for card in cards:
        assert "@everyone" not in card.text
        assert "@here" not in card.text
        assert "<@123456789012345678>" not in card.text
        assert "\\*\\*bold\\*\\*" in card.text
        assert "…" in card.text
        assert len(card.text) < 300


def test_duplicate_server_names_keep_ids_for_disambiguation():
    pages = server_pages(snapshot_for(3, names=["Repeated", "repeated", "Unique"]))
    cards = [card.text for page in pages for card in page.cards]
    duplicates = [text for text in cards if "repeated" in text.casefold()]
    assert len(duplicates) == 2
    assert any("ID 1" in text for text in duplicates)
    assert any("ID 2" in text for text in duplicates)
    assert "ID 3" not in next(text for text in cards if "Unique" in text)


def test_server_totals_label_member_sum_estimates_and_history_period():
    snapshot = snapshot_for(2)
    snapshot = replace(snapshot, servers=(replace(snapshot.servers[0], approximate_members=True),
                                         snapshot.servers[1]))
    header = server_pages(snapshot)[0].header
    assert "**20** membros somados (aproximado)" in header
    assert "**3** sínteses · histórico persistido" in header


def test_unavailable_history_and_servers_have_clear_states():
    snapshot = snapshot_for(1, stats_available=False)
    pages = server_pages(snapshot)
    assert "Total de sínteses históricas indisponível" in pages[0].header
    assert "indisponível sínteses" in pages[0].cards[0].text
    unavailable = replace(snapshot, servers=(), servers_available=False)
    pages = server_pages(unavailable)
    assert "Dados de servidores indisponíveis" in pages[0].header
    assert "Tente atualizar" in pages[0].cards[0].text


@pytest.mark.parametrize("section", ["summary", "engines", "voice"])
def test_missing_tts_sources_stay_unavailable_in_every_section(section):
    pages = tts_pages(snapshot_for(tts_available=False), section)
    assert len(pages) == 1
    assert "Métricas TTS indisponíveis" in pages[0].cards[0].text
    assert "desde o reinício" in pages[0].header
    assert_page_budget(pages)


def test_worker_availability_decision_and_completed_execution_are_distinct():
    pages = tts_pages(snapshot_for(metrics={"tts_agent": {
        "enabled": True, "ok": True, "route": "worker",
        "last_check_age_seconds": 90,
        "last_route_decision": "vps", "last_route_reason": "local_until_measured",
        "last_effective_route": "worker", "last_effective_reason": "worker_ready",
    }}))
    text = text_of(pages)
    assert "Worker disponível" in text
    assert "health há 90 s (desatualizado)" in text
    assert "Última decisão: **VPS** · VPS até obter medições comparáveis" in text
    assert "Última síntese concluída: **Worker** · worker pronto" in text


def test_legacy_health_route_does_not_imply_a_recent_decision_or_execution():
    text = text_of(tts_pages(snapshot_for(metrics={"tts_agent": {"ok": True, "route": "worker"}})))
    assert "Última decisão: **sem decisão registrada**" in text
    assert "Última síntese concluída: **sem síntese registrada**" in text
    assert "sem verificação registrada" in text


def test_current_false_and_zero_worker_values_survive_legacy_fallbacks():
    metrics = {
        "cache_hits": 0, "cache_misses": 0, "cache_stores": 0,
        "tts_agent_last_cache_hit": True, "tts_agent_last_audio_bytes": 2000,
        "tts_agent_last_synth_ms": 200,
        "tts_agent": {"last_requested_engine": "edge", "last_selected_engine": "edge",
                      "last_cache_hit": False, "last_audio_bytes": 0,
                      "last_audio_format": "wav", "last_synth_ms": 0},
    }
    text = text_of(tts_pages(snapshot_for(metrics=metrics)))
    assert "0 hits · 0 misses · 0 gravações · sem consultas" in text
    assert "wav · 0 B · cache miss · 0.00 ms" in text
    assert "cache hit" not in text


def test_historical_failures_do_not_make_a_currently_recovered_engine_red():
    metrics = {"engines_by_route": {"worker": {"edge": {
        "synth_count": 3, "synth_total_ms": 150,
        "synth_failures": 4, "consecutive_failures": 0,
    }}, "vps": {"edge": {"synth_count": 2, "synth_total_ms": 20,
                            "synth_failures": 1, "consecutive_failures": 1}}}}
    pages = tts_pages(snapshot_for(metrics=metrics), "engines")
    text = text_of(pages)
    assert "🟢 **Edge** · Worker" in text
    assert "🔴 **Edge** · VPS" in text
    assert "3 sínteses · 4 falhas · média 50 ms" in text
    summary = text_of(tts_pages(snapshot_for(metrics=metrics)))
    assert "Worker: **3** · VPS: **2**" in summary


def test_legacy_metrics_keep_unidentified_route_visible():
    metrics = {"engines": {"edge": {"synth_count": 4}}}
    assert "Rota não identificada (métricas anteriores): **4**" in text_of(tts_pages(snapshot_for(metrics=metrics)))
    assert "**Edge** · rota não identificada" in text_of(tts_pages(snapshot_for(metrics=metrics), "engines"))


@pytest.mark.parametrize("section", ["summary", "engines", "voice"])
def test_malformed_tts_values_render_without_reporting_fake_zeroes(section):
    metrics = {"cache_hits": "bad", "cache_misses": float("nan"),
               "queued_items_current": {}, "tts_agent": ["invalid"],
               "worker_voice_agent": "invalid",
               "engines": {"edge": {"synth_count": "bad", "avg_synth_ms": float("inf")}}}
    pages = tts_pages(snapshot_for(metrics=metrics), section)
    assert_page_budget(pages)
    text = text_of(pages)
    assert "nan" not in text
    assert "inf ms" not in text
    assert "indisponível" in text
    if section == "engines":
        assert "sem amostras" in text
        assert "0 sínteses" not in text


def test_many_long_engine_names_and_errors_are_all_paginated_within_budget():
    metrics = {"engines_by_route": {"worker": {
        f"custom_{index:03}_" + "x" * 200: {"synth_count": 1, "last_error": "@everyone **error** " + "X" * 1000}
        for index in range(71)
    }}}
    pages = tts_pages(snapshot_for(metrics=metrics), "engines")
    text = text_of(pages)
    assert len(pages) > 1
    assert sum(len(page.cards) for page in pages) == 71
    for index in range(71):
        assert text.count(f"custom\\_{index:03}\\_") == 1
    assert "@everyone" not in text
    assert_page_budget(pages)


def test_voice_page_preserves_false_flags_and_escapes_remote_text():
    metrics = {"tts_agent": {"voice_agent": {
        "state": "@everyone **remote**",
        "direct_tts_ready": False, "music_ready": False, "tts_ready": True,
        "session_count": 0,
        "last_handoff": {"session_id_present": False, "endpoint_present": False,
                         "voice_token_present": False},
        "last_connection": {"ready_received": False, "udp_probe_ok": True},
    }}}
    text = text_of(tts_pages(snapshot_for(metrics=metrics), "voice"))
    assert "Áudio direto pronto: não" in text
    assert "música não · TTS sim" in text
    assert "0 sessões" in text
    assert "Sessão presente: não · endpoint presente: não · token presente: não" in text
    assert "WS pronto: não · UDP ok: sim" in text
    assert "@everyone" not in text
    assert "\\*\\*remote\\*\\*" in text


def test_unknown_section_and_tab_have_working_default_pages():
    snapshot = snapshot_for(1)
    assert panel_pages(snapshot, "unknown") == server_pages(snapshot)
    assert panel_pages(snapshot, "tts", "unknown") == tts_pages(snapshot, "summary")
    assert safe_text("\n\r") == "—"


class FakeResponse:
    def __init__(self):
        self.done = False
        self.defer = AsyncMock(side_effect=self._defer)
        self.send_message = AsyncMock(side_effect=self._send)

    async def _defer(self):
        self.done = True

    async def _send(self, *args, **kwargs):
        self.done = True

    def is_done(self):
        return self.done


def interaction_for(owner_id=1, *, message=None):
    message = message or SimpleNamespace(edit=AsyncMock())
    return SimpleNamespace(
        user=SimpleNamespace(id=owner_id),
        response=FakeResponse(),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(return_value=message),
    )


def view_for(snapshot=None, *, collector=None, on_close=None):
    snapshot = snapshot or snapshot_for(11)
    collector = collector or SimpleNamespace(collect=AsyncMock(return_value=snapshot))
    return StatusView(owner_id=1, snapshot=snapshot, collector=collector, on_close=on_close)


def button(view, label):
    return next(child for child in view.walk_children() if getattr(child, "label", None) == label)


def serialized_components(view):
    def walk(item):
        if isinstance(item, dict):
            if "type" in item:
                yield item
            for child in item.values():
                yield from walk(child)
        elif isinstance(item, list):
            for child in item:
                yield from walk(child)

    return list(walk(view.to_components()))


def assert_component_budget(view):
    components = serialized_components(view)
    assert len(components) <= 40
    assert sum(len(item.get("content", "")) for item in components) <= 4000
    assert json.loads(json.dumps(view.to_components())) == view.to_components()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 5, 6, 103])
async def test_real_discord_components_fit_limits_on_every_server_page(count):
    view = view_for(snapshot_for(count))
    try:
        for page in range(len(view._pages())):
            view.page = page
            view._rebuild()
            assert_component_budget(view)
            assert button(view, "Anterior").disabled is (page == 0)
            assert button(view, "Próxima").disabled is (page == len(view._pages()) - 1)
    finally:
        view.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["summary", "engines", "voice"])
async def test_real_discord_tts_components_fit_limits_with_large_remote_payloads(section):
    text = "@everyone **remote** " + "X" * 2000
    metrics = {
        "engines_by_route": {"worker": {f"custom_{index}_" + text: {"synth_count": 1, "last_error": text}
                                        for index in range(31)}},
        "tts_agent": {"worker_id": text, "worker_version": text, "last_error": text,
                      "available_engines": [text] * 100,
                      "voice_agent": {"state": text, "missing": [text] * 100,
                                      "last_session": {"guild_id": text, "channel_id": text},
                                      "last_handoff": {"voice_owner": text},
                                      "last_transfer": {"voice_owner": text, "requested_owner": text, "state": text},
                                      "last_connection": {"state": text, "stage": text, "error": text}}},
    }
    view = view_for(snapshot_for(metrics=metrics))
    try:
        view.tab = "tts"
        view.section = section
        for page in range(len(view._pages())):
            view.page = page
            view._rebuild()
            assert_component_budget(view)
        assert any(item["type"] == 3 for item in serialized_components(view))
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_page_and_tab_navigation_use_the_snapshot_without_collecting_again():
    view = view_for()
    try:
        original_pages = view._pages()
        interaction = interaction_for()
        await button(view, "Próxima").callback(interaction)
        assert view.page == 1
        assert view._pages() is original_pages
        assert view.revision == 1
        await button(view, "TTS").callback(interaction_for())
        assert view.tab == "tts"
        assert view.page == 0
        await view.navigate(interaction_for(), "section", value="engines", source_revision=view.revision)
        assert view.section == "engines"
        await button(view, "Servidores").callback(interaction_for())
        assert view._pages() is original_pages
        view.collector.collect.assert_not_awaited()
        interaction.edit_original_response.assert_awaited_once()
        assert interaction.edit_original_response.await_args.kwargs["allowed_mentions"].everyone is False
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_concurrent_clicks_from_one_render_do_not_skip_a_server_page():
    view = view_for(snapshot_for(26))
    try:
        next_button = button(view, "Próxima")
        first = interaction_for()
        second = interaction_for()
        await asyncio.gather(next_button.callback(first), next_button.callback(second))
        assert view.page == 1
        assert view.revision == 1
        assert first.edit_original_response.await_count + second.edit_original_response.await_count == 1
        first.response.defer.assert_awaited_once()
        second.response.defer.assert_awaited_once()
        view.collector.collect.assert_not_awaited()
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_other_users_cannot_collect_or_change_the_panel():
    view = view_for()
    try:
        interaction = interaction_for(owner_id=2)
        await button(view, "Atualizar").callback(interaction)
        assert view.revision == 0
        assert view.page == 0
        view.collector.collect.assert_not_awaited()
        interaction.edit_original_response.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_invalid_page_actions_do_not_edit_the_message():
    view = view_for(snapshot_for(1))
    try:
        for step in (-1, 1):
            interaction = interaction_for()
            await view.navigate(interaction, "page", value=step, source_revision=view.revision)
            assert view.page == view.revision == 0
            interaction.edit_original_response.assert_not_awaited()
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_refresh_invalidates_cached_pages_and_clamps_a_shorter_server_list():
    old = snapshot_for(11)
    new = replace(snapshot_for(1), collected_at=1700000600)
    view = view_for(old, collector=SimpleNamespace(collect=AsyncMock(return_value=new)))
    try:
        view.page = 2
        view._rebuild()
        cached_pages = view._pages()
        await button(view, "Atualizar").callback(interaction_for())
        assert view.snapshot is new
        assert view.page == 0
        assert len(view._pages()) == 1
        assert view._pages() is not cached_pages
        assert button(view, "Próxima").disabled is True
        view.collector.collect.assert_awaited_once()
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_refresh_keeps_the_selected_tts_section():
    old = snapshot_for(metrics={"engines_by_route": {"worker": {f"engine_{index}": {"synth_count": 1}
                                                               for index in range(20)}}})
    new = snapshot_for(metrics={"engines_by_route": {"worker": {"edge": {"synth_count": 1}}}})
    view = view_for(old, collector=SimpleNamespace(collect=AsyncMock(return_value=new)))
    try:
        view.tab, view.section, view.page = "tts", "engines", 3
        view._rebuild()
        await button(view, "Atualizar").callback(interaction_for())
        assert (view.tab, view.section, view.page) == ("tts", "engines", 0)
        assert view.snapshot is new
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_refresh_cooldown_prevents_repeated_collection():
    view = view_for()
    try:
        await button(view, "Atualizar").callback(interaction_for())
        revision = view.revision
        interaction = interaction_for()
        await button(view, "Atualizar").callback(interaction)
        view.collector.collect.assert_awaited_once()
        assert view.revision == revision
        interaction.edit_original_response.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_refresh_collection_error_preserves_previous_snapshot_and_shows_notice():
    old = snapshot_for(11)
    view = view_for(old, collector=SimpleNamespace(collect=AsyncMock(side_effect=RuntimeError("offline"))))
    try:
        view.page = 1
        view._rebuild()
        await button(view, "Atualizar").callback(interaction_for())
        assert view.snapshot is old
        assert view.page == 1
        contents = "\n".join(item.get("content", "") for item in serialized_components(view))
        assert "dados anteriores foram preservados" in contents
        assert_component_budget(view)
    finally:
        view.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_source", ["stats_available", "tts_available", "servers_available"])
async def test_refresh_source_regression_preserves_previous_snapshot(missing_source):
    old = snapshot_for(6)
    new = replace(snapshot_for(3), **{missing_source: False})
    view = view_for(old, collector=SimpleNamespace(collect=AsyncMock(return_value=new)))
    try:
        await button(view, "Atualizar").callback(interaction_for())
        assert view.snapshot is old
        contents = "\n".join(item.get("content", "") for item in serialized_components(view))
        assert "dados anteriores foram preservados" in contents
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_failed_message_edit_restores_page_revision_snapshot_and_cached_render():
    old = snapshot_for(11)
    new = snapshot_for(1)
    view = view_for(old, collector=SimpleNamespace(collect=AsyncMock(return_value=new)))
    try:
        view.page = 1
        view._rebuild()
        original_pages = view._pages()
        interaction = interaction_for()
        interaction.edit_original_response.side_effect = RuntimeError("message unavailable")
        await button(view, "Atualizar").callback(interaction)
        assert view.snapshot is old
        assert view.page == 1
        assert view.revision == 0
        assert view._pages() is original_pages
        interaction.followup.send.assert_awaited_once()
        assert "atualizar a mensagem" in interaction.followup.send.await_args.args[0]
    finally:
        view.stop()


@pytest.mark.asyncio
async def test_timeout_keeps_report_and_disables_all_controls_once():
    closed = []
    view = view_for(on_close=closed.append)
    view.tab = "tts"
    view._rebuild()
    message = SimpleNamespace(edit=AsyncMock())
    view.message = message
    await view.on_timeout()
    await view.close()

    assert view.closed is True
    assert view.is_finished() is True
    assert closed == [view]
    controls = [item for item in view.walk_children() if hasattr(item, "disabled")]
    assert controls and all(item.disabled for item in controls)
    text = "\n".join(item.get("content", "") for item in serialized_components(view))
    assert "Painel encerrado" in text
    assert "status" in text
    assert "Rota e disponibilidade" in text
    assert_component_budget(view)
    interaction = interaction_for()
    await view.navigate(interaction, "refresh", source_revision=view.revision)
    view.collector.collect.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_deactivation_during_collection_does_not_replace_the_snapshot_or_edit():
    started, release = asyncio.Event(), asyncio.Event()
    old, new = snapshot_for(11), snapshot_for(1)

    async def collect():
        started.set()
        await release.wait()
        return new

    view = view_for(old, collector=SimpleNamespace(collect=AsyncMock(side_effect=collect)))
    interaction = interaction_for()
    task = asyncio.create_task(button(view, "Atualizar").callback(interaction))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        view.deactivate()
        release.set()
        await asyncio.wait_for(task, timeout=1)
        assert view.snapshot is old
        assert view.revision == 0
        interaction.edit_original_response.assert_not_awaited()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        view.stop()
