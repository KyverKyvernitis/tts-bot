"""Exercise the status collector against copied Discord and TTS snapshots."""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from utility.status import data
from utility.status.data import (
    StatusCollector,
    collect_engines,
    collect_servers,
    first_present,
    scan_storage,
)


def guild(guild_id: int, name: str, *, members=10, cached_members=()):
    return SimpleNamespace(
        id=guild_id,
        name=name,
        member_count=members,
        members=list(cached_members),
        icon=SimpleNamespace(url=f"https://example.test/{guild_id}.png"),
    )


def bot_for(*guilds, stats=None, metrics=None):
    return SimpleNamespace(
        guilds=list(guilds),
        settings_db=SimpleNamespace(get_all_tts_synt_stats=lambda: stats if stats is not None else {}),
        get_health_snapshot=lambda: {"tts_metrics": metrics if metrics is not None else {}},
        user=SimpleNamespace(display_avatar=SimpleNamespace(url="https://example.test/bot.png")),
    )


def test_server_order_uses_the_displayed_member_fallback_and_stable_ties():
    bot = bot_for(
        guild(7, "Beta", members=5),
        guild(9, "alpha", members=5),
        guild(3, "Alpha", members=5),
        guild(4, "Cached", members=None, cached_members=range(8)),
        guild(8, "Unknown", members=None, cached_members=()),
    )
    rows = collect_servers(bot, {}, stats_available=True)

    assert [row.guild_id for row in rows] == [4, 3, 9, 7, 8]
    assert rows[0].members == 8
    assert rows[0].approximate_members is True
    assert rows[-1].members == 0
    assert rows[-1].approximate_members is True
    assert rows[1].approximate_members is False


@pytest.mark.parametrize("raw_id", [927002914449424404, "927002914449424404"])
def test_real_discord_ids_keep_their_exact_history_lookup(raw_id):
    row, = collect_servers(bot_for(guild(raw_id, "Technical")),
                           {927002914449424404: {"edge": 7}}, stats_available=True)
    assert row.guild_id == 927002914449424404
    assert row.synths == 7


def test_server_history_combines_engine_aliases_and_handles_unavailable_rows():
    rows = collect_servers(
        bot_for(guild(1, "Available"), guild(2, "Malformed"), guild(3, "Empty")),
        {
            "1": {"engines": {"android": "3", "ATTS": 2, "google": 4, "gtts": 1,
                              "custom": 7, "total": 1000, "updated_at": 12345,
                                  "auto": 2}},
            2: None,
        },
        stats_available=True,
    )
    by_id = {row.guild_id: row for row in rows}

    assert dict(by_id[1].engines) == {"android_native": 5, "gtts": 5, "other": 9}
    assert by_id[1].synths == 19
    assert by_id[2].stats_available is False
    assert by_id[2].synths is None
    assert by_id[3].synths == 0
    unavailable = collect_servers(bot_for(guild(1, "Offline")), {}, stats_available=False)[0]
    assert unavailable.synths is None


def test_partially_malformed_server_history_preserves_values_without_a_fake_total():
    row = collect_servers(bot_for(guild(1, "Partial")),
                          {1: {"edge": 3, "android": "invalid"}}, stats_available=True)[0]
    assert row.engines == (("edge", 3),)
    assert row.stats_available is False
    assert row.synths is None


def test_engines_by_route_is_authoritative_and_keeps_worker_and_vps_separate():
    engines = collect_engines({
        "engines": {"edge": {"synth_count": 999}, "tts_agent:edge": {"synth_count": 999}},
        "engines_by_route": {
            "worker": {"edge_tts": {"synth_count": 4, "synth_total_ms": 800},
                       "microsoft": {"synth_count": 1, "avg_synth_ms": 50}},
            "vps": {"edge": {"synth_count": 3, "synth_total_ms": 30}},
        },
    })

    assert [(item.route, item.key, item.count) for item in engines] == [
        ("worker", "edge", 5), ("vps", "edge", 3),
    ]
    assert engines[0].average_ms == pytest.approx(170)
    assert engines[1].average_ms == pytest.approx(10)
    assert collect_engines({"engines_by_route": {}, "engines": {"edge": {"synth_count": 999}}}) == ()


def test_legacy_engine_metrics_do_not_invent_a_vps_route():
    engines = collect_engines({"engines": {
        "tts_agent:android": {"synth_count": 2},
        "worker_tts_agent": {"synth_count": 1},
        "edge": {"synth_count": 4},
    }})
    assert {(item.route, item.key, item.count) for item in engines} == {
        ("worker", "android_native", 2), ("worker", "auto", 1), ("unknown", "edge", 4),
    }


def test_engine_aliases_use_weighted_samples_and_preserve_zero_values():
    engines = collect_engines({"engines_by_route": {"worker": {
        "android": {"synth_count": 2, "synth_total_ms": 200,
                    "synth_failures": 4, "consecutive_failures": 0,
                    "last_synth_ms": 12, "cache_hits": 0, "cache_misses": 0},
        "ATTS": {"synth_count": 3, "avg_synth_ms": 300,
                 "synth_failures": 1, "consecutive_failures": 0,
                 "last_synth_ms": 0, "cache_hits": 0, "cache_misses": 0},
        "android_tts": {"synth_count": 0, "avg_synth_ms": 10000,
                        "synth_failures": 0, "cache_hits": 0, "cache_misses": 0},
    }}})
    assert len(engines) == 1
    metric = engines[0]
    assert metric.key == "android_native"
    assert metric.count == 5
    assert metric.average_ms == pytest.approx(220)
    assert metric.failures == 5
    assert metric.consecutive_failures == 0
    assert metric.last_ms == 0
    assert metric.cache_hits == metric.cache_misses == 0


def test_merged_aliases_do_not_report_partial_totals_as_complete():
    metric, = collect_engines({"engines_by_route": {"worker": {
        "android": {"synth_count": 2, "synth_failures": 3, "cache_hits": 0},
        "ATTS": {"synth_count": 1},
    }}})
    assert metric.count == 3
    assert metric.failures is None
    assert metric.cache_hits is None


@pytest.mark.parametrize("raw", [None, [], "bad", 1, float("nan")])
def test_malformed_engine_sources_have_no_metrics(raw):
    assert collect_engines({"engines": raw}) == ()


def test_malformed_engine_fields_remain_unavailable_instead_of_becoming_zero():
    engines = collect_engines({"engines": {
        "edge": {"synth_count": "invalid", "synth_failures": float("nan"),
                 "synth_total_ms": float("inf"), "avg_synth_ms": "bad",
                 "last_synth_ms": None, "cache_hits": {}, "cache_misses": []},
        "ignored": None,
        "empty": {},
    }})
    assert len(engines) == 2
    metric = next(item for item in engines if item.key == "edge")
    assert metric.count is metric.failures is metric.average_ms is metric.last_ms is None
    assert metric.cache_hits is metric.cache_misses is None


@pytest.mark.parametrize("value", [False, 0, ""])
def test_current_false_zero_and_empty_values_are_not_replaced_by_legacy_values(value):
    assert first_present({"current": value}, "current", {"legacy": "old"}, "legacy") == value
    assert first_present({"current": None}, "current", {"legacy": "old"}, "legacy") == "old"


def test_storage_counts_regular_files_without_following_symlinks(tmp_path: Path):
    (tmp_path / "runtime").mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "runtime" / "first.wav").write_bytes(b"abc")
    (tmp_path / "runtime" / "nested").mkdir()
    (tmp_path / "cache" / "second.wav").write_bytes(b"12345")
    (tmp_path / "cache" / "alias.wav").symlink_to(tmp_path / "runtime" / "first.wav")

    snapshot = scan_storage(tmp_path)
    assert (snapshot.runtime_files, snapshot.cache_files, snapshot.credential_files) == (1, 1, 0)
    assert snapshot.total_bytes == 8


@pytest.mark.asyncio
async def test_collector_snapshots_do_not_change_when_live_sources_change(tmp_path: Path):
    metrics = {"tts_agent": {"last_cache_hit": False, "available_engines": ["edge"]},
               "engines_by_route": {"worker": {"edge": {"synth_count": 2, "synth_total_ms": 80}}}}
    stats = {1: {"edge": 2}}
    live_guild = guild(1, "Before", members=6)
    collector = StatusCollector(bot_for(live_guild, stats=stats, metrics=metrics), storage_root=tmp_path)
    snapshot = await collector.collect()

    metrics["tts_agent"]["last_cache_hit"] = True
    metrics["tts_agent"]["available_engines"].append("gtts")
    metrics["engines_by_route"]["worker"]["edge"]["synth_count"] = 200
    stats[1]["edge"] = 200
    live_guild.name = "After"
    live_guild.member_count = 100

    assert snapshot.tts["tts_agent"] == {"last_cache_hit": False, "available_engines": ["edge"]}
    assert snapshot.engines[0].count == 2
    assert snapshot.servers[0].name == "Before"
    assert snapshot.servers[0].members == 6
    assert snapshot.servers[0].synths == 2
    with pytest.raises(FrozenInstanceError):
        snapshot.collected_at = 0
    with pytest.raises(FrozenInstanceError):
        snapshot.servers[0].name = "Changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_getters", [False, True])
async def test_collector_supports_sync_and_async_sources(tmp_path: Path, async_getters):
    bot = bot_for(guild(1, "Test"), stats={1: {"edge": 3}}, metrics={"cache_hits": 0})
    if async_getters:
        async def stats():
            return {1: {"edge": 3}}

        async def health():
            return {"tts_metrics": {"cache_hits": 0}}

        bot.settings_db.get_all_tts_synt_stats = stats
        bot.get_health_snapshot = health

    snapshot = await StatusCollector(bot, storage_root=tmp_path).collect()
    assert snapshot.servers_available is snapshot.stats_available is snapshot.tts_available is True
    assert snapshot.servers[0].synths == 3
    assert snapshot.tts["cache_hits"] == 0
    assert snapshot.thumbnail_url == "https://example.test/bot.png"


@pytest.mark.asyncio
@pytest.mark.parametrize("health", [None, [], {}, {"tts_metrics": None},
                                    {"tts_metrics": "bad"},
                                    {"tts_metrics": {"cache_hits": 0}, "tts_metrics_error": "unavailable"}])
async def test_collector_distinguishes_unavailable_tts_sources(tmp_path: Path, health):
    bot = bot_for()
    bot.get_health_snapshot = lambda: health
    snapshot = await StatusCollector(bot, storage_root=tmp_path).collect()
    assert snapshot.tts_available is False
    assert snapshot.tts == {}
    assert snapshot.engines == ()


@pytest.mark.asyncio
async def test_collector_recovers_when_sources_raise_or_are_absent(tmp_path: Path):
    def fail():
        raise RuntimeError("source unavailable")

    bot = bot_for(guild(1, "Server"))
    bot.settings_db.get_all_tts_synt_stats = fail
    bot.get_health_snapshot = fail
    snapshot = await StatusCollector(bot, storage_root=tmp_path).collect()
    assert snapshot.servers_available is True
    assert snapshot.stats_available is snapshot.tts_available is False
    assert snapshot.servers[0].synths is None
    absent = await StatusCollector(SimpleNamespace(), storage_root=tmp_path).collect()
    assert absent.servers_available is absent.stats_available is absent.tts_available is False


@pytest.mark.asyncio
async def test_storage_scan_runs_off_event_loop_and_is_shared_under_concurrent_collection(tmp_path: Path, monkeypatch):
    scans = []
    main_thread = threading.get_ident()
    real_scan = data.scan_storage

    def record_scan(root):
        scans.append(threading.get_ident())
        return real_scan(root)

    monkeypatch.setattr(data, "scan_storage", record_scan)
    collector = StatusCollector(bot_for(), storage_root=tmp_path)
    first, second = await asyncio.gather(collector.collect(), collector.collect())

    assert scans and len(scans) == 1
    assert scans[0] != main_thread
    assert first.storage is second.storage
