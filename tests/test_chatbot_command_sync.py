"""Exercise command synchronization without importing the bot startup runtime."""
from __future__ import annotations

import ast
import asyncio
import copy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_sync_class():
    source = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
    bot_class = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "BotLocal"
    )
    methods = [
        node for node in bot_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_app_command_manifest_diff", "_smart_sync_app_commands"}
    ]
    isolated = ast.Module(
        body=[ast.ClassDef(name="SyncBot", bases=[], keywords=[], body=methods, decorator_list=[])],
        type_ignores=[],
    )
    namespace = {
        "datetime": datetime,
        "timezone": timezone,
        "discord": SimpleNamespace(Object=lambda *, id: SimpleNamespace(id=id)),
    }
    exec(compile(ast.fix_missing_locations(isolated), "bot.py", "exec"), namespace)
    return namespace["SyncBot"]


class _RemoteCommand:
    def __init__(self, name: str, command_type: int = 1):
        self.name = name
        self.type = SimpleNamespace(value=command_type)
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _Tree:
    def __init__(self, commands):
        self.commands = commands
        self.fetches = 0
        self.sync_scopes = []
        self.copy_scopes = []

    async def fetch_commands(self):
        self.fetches += 1
        return self.commands

    async def sync(self, *, guild=None):
        self.sync_scopes.append(None if guild is None else guild.id)
        return [SimpleNamespace(name="chatbot")]

    def copy_global_to(self, *, guild):
        self.copy_scopes.append(guild.id)


def _harness(previous_labels, current_labels, *, commands=(), unchanged=False):
    bot = _load_sync_class()()
    current = {"labels": current_labels, "hash": "current"}
    previous = {"labels": previous_labels, "hash": "current" if unchanged else "previous"}
    state = {"previous": previous, "statuses": []}
    bot.tree = _Tree(list(commands))
    bot._build_app_commands_manifest = lambda _guild_ids: copy.deepcopy(current)
    bot._load_previous_app_commands_manifest = lambda: copy.deepcopy(state["previous"])
    bot._save_app_commands_manifest = lambda manifest: state.update(previous=copy.deepcopy(manifest))
    bot._write_app_command_sync_status = lambda status: state["statuses"].append(copy.deepcopy(status))
    return bot, state


def _sync(bot, *, global_scope=False, clear=True, enabled=True):
    return asyncio.run(bot._smart_sync_app_commands(
        {123}, should_sync=enabled, allow_global_sync=global_scope,
        clear_globals_allowed=clear,
    ))


def test_removing_persona_subcommands_preserves_surviving_global_groups():
    chatbot = _RemoteCommand("chatbot")
    admin = _RemoteCommand("chatbotadmin")
    removed_root = _RemoteCommand("oldchat")
    entry_point = _RemoteCommand("entry", command_type=4)
    bot, _ = _harness(
        ["/chatbot", "/chatbot profile", "/chatbot persona", "/chatbot extrovert",
         "/chatbotadmin", "/oldchat", "/entry"],
        ["/chatbot", "/chatbot configurar", "/chatbot memoria", "/chatbotadmin"],
        commands=[chatbot, admin, removed_root, entry_point],
    )

    result = _sync(bot)

    assert not chatbot.deleted
    assert not admin.deleted
    assert removed_root.deleted
    assert not entry_point.deleted
    assert result["clear_performed"]
    assert bot.tree.sync_scopes == [123]
    assert bot.tree.copy_scopes == [123]


def test_removing_only_a_subcommand_does_not_delete_its_global_root():
    chatbot = _RemoteCommand("chatbot")
    bot, _ = _harness(
        ["/chatbot", "/chatbot persona"],
        ["/chatbot", "/chatbot configurar"],
        commands=[chatbot],
    )

    result = _sync(bot)

    assert not chatbot.deleted
    assert not result["clear_performed"]
    assert result["sync_performed"]
    assert None not in bot.tree.sync_scopes


def test_guild_only_baseline_does_not_suppress_later_global_publication():
    bot, state = _harness(
        ["/chatbot", "/chatbot persona"],
        ["/chatbot", "/chatbot configurar"],
    )

    assert _sync(bot)["sync_performed"]
    assert state["previous"]["global_sync_hash"] == ""
    assert _sync(bot)["reason"] == "unchanged"

    publication = _sync(bot, global_scope=True)
    assert not publication["manifest_changed"]
    assert publication["global_sync_pending"]
    assert publication["sync_performed"]
    assert state["previous"]["global_sync_hash"] == "current"
    assert bot.tree.sync_scopes == [123, None, 123]

    assert _sync(bot, global_scope=True)["reason"] == "unchanged"
    assert bot.tree.sync_scopes == [123, None, 123]


def test_existing_manifest_without_global_publication_marker_is_published_once():
    bot, _ = _harness(["/chatbot"], ["/chatbot"], unchanged=True)

    assert _sync(bot, global_scope=True)["sync_performed"]
    assert _sync(bot, global_scope=True)["reason"] == "unchanged"
    assert bot.tree.sync_scopes == [None, 123]


@pytest.mark.parametrize("enabled,clear", [(False, True), (True, False)])
def test_existing_sync_and_clear_controls_remain_effective(enabled, clear):
    removed_root = _RemoteCommand("oldchat")
    bot, state = _harness(
        ["/chatbot", "/oldchat"], ["/chatbot"], commands=[removed_root],
    )

    result = _sync(bot, enabled=enabled, clear=clear)

    assert not removed_root.deleted
    assert bot.tree.fetches == 0
    assert not result["clear_performed"]
    assert bot.tree.sync_scopes == ([123] if enabled else [])
    assert state["statuses"][-1]["reason"] == ("synced" if enabled else "disabled_by_env")
