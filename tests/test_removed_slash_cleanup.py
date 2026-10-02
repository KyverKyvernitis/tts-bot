from __future__ import annotations

import ast
import asyncio
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest


BOT_PATH = Path(__file__).resolve().parents[1] / "bot.py"
METHOD_NAMES = {
    "_cleanup_removed_slash_commands",
    "_removed_slash_cleanup_signature",
    "_cleanup_removed_slash_commands_if_needed",
}


class FakeHTTPException(Exception):
    def __init__(self, message="Discord API failure", *, status=500, code=0):
        super().__init__(message)
        self.status = status
        self.code = code


class FakeNotFound(FakeHTTPException):
    def __init__(self):
        super().__init__("Unknown application command", status=404, code=10063)


class FakeCommand:
    def __init__(self, name, *, error=None):
        self.name = name
        self.error = error
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1
        if self.error is not None:
            raise self.error


class FakeTree:
    def __init__(self, responses):
        self.responses = {scope: deque(values) for scope, values in responses.items()}
        self.fetch_calls = []

    async def fetch_commands(self, *, guild=None):
        scope = None if guild is None else guild.id
        self.fetch_calls.append(scope)
        responses = self.responses.get(scope)
        if not responses:
            raise AssertionError(f"Unexpected fetch for scope {scope}")
        response = responses.popleft()
        if isinstance(response, Exception):
            raise response
        return list(response)


@pytest.fixture(scope="module")
def extracted_cleanup():
    """Compile only cleanup code; importing bot.py would start its boot setup."""
    source = ast.parse(BOT_PATH.read_text(encoding="utf-8"))
    constant = next(
        node
        for node in source.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "REMOVED_SLASH_COMMANDS"
            for target in node.targets
        )
    )
    methods = {
        node.name: node
        for node in ast.walk(source)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in METHOD_NAMES
    }
    assert set(methods) == METHOD_NAMES
    harness_class = ast.ClassDef(
        name="CleanupHarness",
        bases=[],
        keywords=[],
        body=[methods[name] for name in sorted(METHOD_NAMES)],
        decorator_list=[],
    )
    module = ast.fix_missing_locations(
        ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__",
                    names=[ast.alias(name="annotations")],
                    level=0,
                ),
                constant,
                harness_class,
            ],
            type_ignores=[],
        )
    )
    namespace = {
        "asyncio": asyncio,
        "hashlib": hashlib,
        "json": json,
        "datetime": datetime,
        "timezone": timezone,
        "BOOT_LOG": logging.getLogger("test.removed_slash_cleanup"),
        "discord": SimpleNamespace(
            Object=lambda *, id: SimpleNamespace(id=id),
            HTTPException=FakeHTTPException,
            NotFound=FakeNotFound,
        ),
    }
    exec(compile(module, str(BOT_PATH), "exec"), namespace)
    return namespace["CleanupHarness"], namespace["REMOVED_SLASH_COMMANDS"]


@pytest.fixture
def make_bot(extracted_cleanup, tmp_path):
    harness_class, _ = extracted_cleanup

    def create(responses):
        bot = harness_class()
        bot.tree = FakeTree(responses)
        bot._removed_slash_cleanup_state_path = tmp_path / "state" / "removed-slash.json"
        return bot

    return create


def run(awaitable):
    return asyncio.run(awaitable)


def test_removes_vps_in_global_and_guild_scopes_without_touching_active_commands(
    make_bot, extracted_cleanup
):
    _, removed_names = extracted_cleanup
    assert "vps" in removed_names
    global_vps = FakeCommand("vps")
    guild_vps = FakeCommand("vps")
    tts = FakeCommand("tts")
    ping = FakeCommand("ping")
    bot = make_bot(
        {None: [[global_vps, tts], [tts]], 7: [[guild_vps, ping], [ping]]}
    )

    assert run(bot._cleanup_removed_slash_commands({7})) is True
    assert bot.tree.fetch_calls == [None, None, 7, 7]
    assert global_vps.delete_calls == guild_vps.delete_calls == 1
    assert tts.delete_calls == ping.delete_calls == 0
    assert bot._removed_slash_cleanup_failed_scopes == ()


def test_absent_commands_need_only_one_fetch_per_scope(make_bot):
    active_command = FakeCommand("tts")
    bot = make_bot({None: [[]], 7: [[active_command]], 9: [[]]})

    assert run(bot._cleanup_removed_slash_commands({9, 7})) is True
    assert bot.tree.fetch_calls == [None, 7, 9]
    assert active_command.delete_calls == 0
    assert bot._removed_slash_cleanup_failed_scopes == ()


@pytest.mark.parametrize("failed_scope", [None, 7])
def test_fetch_failure_records_scope_and_still_checks_other_scopes(make_bot, failed_scope):
    responses = {None: [[]], 7: [[]]}
    responses[failed_scope] = [FakeHTTPException()]
    bot = make_bot(responses)

    assert run(bot._cleanup_removed_slash_commands({7})) is False
    assert bot.tree.fetch_calls == [None, 7]
    expected_scope = "GLOBAL" if failed_scope is None else "GUILD 7"
    assert bot._removed_slash_cleanup_failed_scopes == (expected_scope,)


def test_all_failed_scopes_are_reported_in_stable_order(make_bot):
    bot = make_bot(
        {None: [FakeHTTPException()], 7: [FakeHTTPException()], 9: [[]]}
    )

    assert run(bot._cleanup_removed_slash_commands({9, 7})) is False
    assert bot._removed_slash_cleanup_failed_scopes == ("GLOBAL", "GUILD 7")
    assert bot.tree.fetch_calls == [None, 7, 9]


def test_delete_failure_is_not_a_success_even_if_command_later_disappears(make_bot):
    removed = FakeCommand("vps", error=FakeHTTPException(status=403))
    bot = make_bot({None: [[removed], []], 7: [[]]})

    assert run(bot._cleanup_removed_slash_commands({7})) is False
    assert removed.delete_calls == 1
    assert bot._removed_slash_cleanup_failed_scopes == ("GLOBAL",)
    assert 7 in bot.tree.fetch_calls


@pytest.mark.parametrize("post_fetch", [FakeHTTPException(), [FakeCommand("vps")]])
def test_failed_post_delete_verification_records_failure(make_bot, post_fetch):
    removed = FakeCommand("vps")
    bot = make_bot({None: [[removed], post_fetch], 7: [[]]})

    assert run(bot._cleanup_removed_slash_commands({7})) is False
    assert removed.delete_calls == 1
    assert bot.tree.fetch_calls == [None, None, 7]
    assert bot._removed_slash_cleanup_failed_scopes == ("GLOBAL",)


def test_delete_404_is_idempotent_and_still_verifies_absence(make_bot):
    removed = FakeCommand("vps", error=FakeNotFound())
    bot = make_bot({None: [[removed], []]})

    assert run(bot._cleanup_removed_slash_commands(set())) is True
    assert removed.delete_calls == 1
    assert bot.tree.fetch_calls == [None, None]
    assert bot._removed_slash_cleanup_failed_scopes == ()


def test_delete_404_with_command_still_present_is_a_failure(make_bot):
    removed = FakeCommand("vps", error=FakeNotFound())
    bot = make_bot({None: [[removed], [removed]]})

    assert run(bot._cleanup_removed_slash_commands(set())) is False
    assert bot.tree.fetch_calls == [None, None]
    assert bot._removed_slash_cleanup_failed_scopes == ("GLOBAL",)


def test_signature_includes_version_two_and_is_independent_of_guild_order(
    make_bot, extracted_cleanup
):
    _, removed_names = extracted_cleanup
    bot = make_bot({})
    payload = {
        "version": 2,
        "names": sorted(removed_names),
        "guild_ids": [7, 9],
    }
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert bot._removed_slash_cleanup_signature({9, 7}) == expected
    assert bot._removed_slash_cleanup_signature({7, 9}) == expected
    assert bot._removed_slash_cleanup_signature({7}) != expected


def test_success_writes_marker_and_repetition_skips_discord_requests(make_bot):
    bot = make_bot({None: [[]], 7: [[]]})

    assert run(bot._cleanup_removed_slash_commands_if_needed({7})) is True
    state = json.loads(bot._removed_slash_cleanup_state_path.read_text(encoding="utf-8"))
    assert state["version"] == 2
    assert state["signature"] == bot._removed_slash_cleanup_signature({7})
    assert datetime.fromisoformat(state["updated_at"]).tzinfo is not None
    assert bot.tree.fetch_calls == [None, 7]
    assert run(bot._cleanup_removed_slash_commands_if_needed({7})) is True
    assert bot.tree.fetch_calls == [None, 7]


@pytest.mark.parametrize("failure_stage", ["fetch", "delete", "post_verify"])
def test_failure_does_not_write_marker_and_next_call_retries(make_bot, failure_stage):
    removed = FakeCommand("vps")
    if failure_stage == "fetch":
        responses = [FakeHTTPException(), []]
    elif failure_stage == "delete":
        removed.error = FakeHTTPException(status=403)
        responses = [[removed], [], []]
    else:
        responses = [[removed], FakeHTTPException(), []]
    bot = make_bot({None: responses})

    assert run(bot._cleanup_removed_slash_commands_if_needed(set())) is False
    assert not bot._removed_slash_cleanup_state_path.exists()
    assert bot._removed_slash_cleanup_failed_scopes == ("GLOBAL",)
    calls_before_retry = len(bot.tree.fetch_calls)
    assert run(bot._cleanup_removed_slash_commands_if_needed(set())) is True
    assert len(bot.tree.fetch_calls) > calls_before_retry
    assert bot._removed_slash_cleanup_state_path.exists()
    assert bot._removed_slash_cleanup_failed_scopes == ()


def test_legacy_marker_is_invalidated_by_cleanup_version(make_bot, extracted_cleanup):
    _, removed_names = extracted_cleanup
    bot = make_bot({None: [[]], 7: [[]]})
    legacy_payload = {"names": sorted(removed_names), "guild_ids": [7]}
    legacy_signature = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    marker = bot._removed_slash_cleanup_state_path
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"signature": legacy_signature}), encoding="utf-8")

    assert run(bot._cleanup_removed_slash_commands_if_needed({7})) is True
    assert bot.tree.fetch_calls == [None, 7]
    state = json.loads(marker.read_text(encoding="utf-8"))
    assert state["signature"] != legacy_signature
    assert state["signature"] == bot._removed_slash_cleanup_signature({7})


def test_failure_keeps_stale_marker_unchanged_for_future_retry(make_bot):
    bot = make_bot({None: [FakeHTTPException()]})
    marker = bot._removed_slash_cleanup_state_path
    marker.parent.mkdir(parents=True)
    old_contents = json.dumps({"signature": "stale-marker"})
    marker.write_text(old_contents, encoding="utf-8")

    assert run(bot._cleanup_removed_slash_commands_if_needed(set())) is False
    assert marker.read_text(encoding="utf-8") == old_contents


def test_marker_write_failure_returns_false_without_recording_completion(make_bot, monkeypatch):
    bot = make_bot({None: [[]]})
    marker = bot._removed_slash_cleanup_state_path
    temporary_marker = marker.with_suffix(".json.tmp")
    original_write = Path.write_text

    def fail_marker_write(path, *args, **kwargs):
        if path == temporary_marker:
            raise OSError("Disk full")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_marker_write)

    assert run(bot._cleanup_removed_slash_commands_if_needed(set())) is False
    assert not marker.exists()
    assert bot.tree.fetch_calls == [None]
