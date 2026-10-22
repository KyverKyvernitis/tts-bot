"""Behavioral checks for the existing Discord panel's SSH presentation.

The source definitions are loaded with small Discord component doubles so these
checks also run on a Termux/CI host without the optional Discord voice packages.
No production registry, token file, socket, or external process is touched.
"""
from __future__ import annotations

import ast
import asyncio
import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "utility/commands/workers.py"


class Component:
    def __init__(self, *children, **values):
        self.children = list(children)
        self.__dict__.update(values)
        self.content = children[0] if children and isinstance(children[0], str) else ""


class Layout:
    def __init__(self, **kwargs):
        self.children = []

    def add_item(self, item):
        self.children.append(item)


def load_panel():
    functions = {
        "_env_bool", "_env_float", "_shorten", "_redact", "_format_seconds", "_format_age",
        "_parse_roles", "_worker_connection", "_ssh_transport_enabled", "_open_worker_http_request",
        "_ssh_local_api_port", "_connection_observation", "_connection_summary", "_connection_detail_text",
        "_configured_ssh_worker", "_ssh_engine_summary", "_ssh_onboarding_text", "_termux_command_block",
        "_telemetry_epoch", "_task_name", "_task_set", "_compact_failure",
    }
    constants = {
        "CORE_WORKER_APK_REPLACES_TERMUX", "WORKERS_DEFAULT_ROLES", "WORKER_ACTION_SPECS",
        "WORKER_EDITABLE_FEATURES", "WORKER_PANEL_ACTION_ORDER", "WORKER_PANEL_ACTION_ORDER_INDEX",
        "WORKER_PANEL_ACTION_VALUES", "_SECRET_PATTERNS", "LEGACY_WORKER_ID", "AUTO_WORKER_ID",
    }
    class_methods = {
        "WorkersCommandMixin": {
            "_worker_base_config", "_request_worker_status_sync", "_collect_workers_snapshot",
            "_run_legacy_worker_action", "_snapshot_needs_auto_wake", "_wake_phone_worker",
            "_run_phone_worker_watch_script", "_start_core_worker_auto_wake_task",
        },
        "WorkersPanelView": {
            "_panel_action_specs_for_selected", "_action_specs_for_selected", "_selected_worker_lines",
            "_rebuild_layout", "_wake_worker", "_show_connection_details", "_show_onboarding_guide",
        },
    }
    tree = ast.parse(SOURCE.read_text())
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            body.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in constants for target in targets):
                body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "WorkerSnapshot":
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in class_methods:
            node.body = [method for method in node.body if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and method.name in class_methods[node.name]]
            body.append(node)
    colors = SimpleNamespace(green=lambda: "green", orange=lambda: "orange", dark_grey=lambda: "grey")
    discord = SimpleNamespace(
        ui=SimpleNamespace(LayoutView=Layout, Container=Component, TextDisplay=Component,
                           Separator=Component, ActionRow=Component, Button=Component),
        Color=colors, ButtonStyle=SimpleNamespace(primary=1, secondary=2),
        AllowedMentions=SimpleNamespace(none=lambda: None),
    )
    module = ModuleType("_workers_ssh_panel_contract")
    module.__dict__.update({
        "asyncio": asyncio, "copy": copy, "dataclass": dataclass, "field": field,
        "hashlib": hashlib, "json": json, "os": os, "re": re, "shlex": shlex,
        "time": time, "Any": Any, "urllib": urllib, "discord": discord,
        "_core_worker_app_normalize_job_type": lambda value: str(value or ""),
        "_automation_status_text": lambda _workers: "em dia",
        "_snapshot_has_online_worker": lambda snapshot: bool(snapshot.online),
        "_agent_version_label": lambda value: str(value or "sem versão"),
        "_physical_telemetry_text": lambda _members: "bateria e rede disponíveis",
        "_queue_status_text": lambda _worker: "livre",
        "_script_health_label": lambda _worker: "scripts ok",
        "_host_label": lambda host: host,
    })
    sys.modules[module.__name__] = module
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(SOURCE), "exec"), module.__dict__)
    return module


PANEL = load_panel()
ENV = {
    "PHONE_WORKER_TRANSPORT": "ssh", "PHONE_WORKER_ID": "phone-test",
    "PHONE_WORKER_HOST": "127.0.0.1", "PHONE_WORKER_PORT": "18766",
    "PHONE_WORKER_TOKEN": "private-test-token", "PHONE_WORKER_ENABLED": "true",
}


def worker(now=None, **connection_changes):
    now = time.time() if now is None else now
    connection = dict(mode="ssh", state="connected", interactive_state="connected", bulk_state="connected",
                      last_ok_at=now-1, rtt_ms=85.0, reconnect_count=2, generation=5)
    connection.update(connection_changes)
    return {
        "worker_id": "phone-test", "name": "Telefone", "version": "1.11.41", "online": True,
        "enabled": True, "endpoint": "http://127.0.0.1:18766", "last_seen_age_seconds": 1,
        "supported_tasks": ["ping", "ssh_connection_diagnostic"],
        "status": {"connection": connection, "tts_agent": {"ok": True, "available_engines": ["edge", "teto"]}},
        "_panel_http_verified_at": now-1, "_panel_http_verified_worker_id": "phone-test",
    }


def health(worker_id="phone-test", runtime_kind="termux"):
    return {"ok": True, "worker_id": worker_id, "runtime_kind": runtime_kind,
            "connection": worker()["status"]["connection"],
            "tts_agent": {"ok": True, "available_engines": ["edge", "teto"]}}


def response(data):
    result = Mock()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.read = Mock(return_value=json.dumps(data).encode())
    return result


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, ENV, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_connected_requires_fresh_reverse_http_identity(self):
        item = worker(100)
        self.assertEqual(PANEL._connection_observation(item, now=101)["label"], "Conectado via SSH")
        item.pop("_panel_http_verified_at")
        self.assertFalse(PANEL._connection_observation(item, now=101)["reverse_ok"])
        self.assertIn("confirmar acesso da VPS", PANEL._connection_observation(item, now=101)["label"])

    def test_another_worker_identity_does_not_confirm_reverse_connection(self):
        item = worker(100)
        item["_panel_http_verified_worker_id"] = "other-phone"
        self.assertFalse(PANEL._connection_observation(item, now=101)["reverse_ok"])

    def test_stale_data_and_failed_bulk_are_not_presented_as_fully_connected(self):
        item = worker(100)
        self.assertIn("desatualizados", PANEL._connection_observation(item, now=200)["label"])
        item = worker(100, bulk_state="reconnecting")
        self.assertIn("parcial", PANEL._connection_observation(item, now=101)["label"])
        item = worker(100, interactive_state="reconnecting")
        self.assertIn("parcial", PANEL._connection_observation(item, now=101)["label"])

    def test_no_configuration_does_not_create_a_connected_state(self):
        item = {"worker_id": "phone-test"}
        self.assertIn("Configurar", PANEL._connection_observation(item, now=100)["label"])

    def test_initial_connection_and_fresh_failure_have_distinct_states(self):
        for state, expected in (("not_configured", "Configurar"), ("connecting", "Conectando"),
                                ("reconnecting", "Reconectando"), ("worker_unavailable", "sem resposta")):
            with self.subTest(state=state):
                item = worker(100, state=state, last_ok_at=None)
                item.pop("_panel_http_verified_at")
                self.assertIn(expected, PANEL._connection_observation(item, now=101)["label"])
        item = worker(100, state="reconnecting", last_ok_at=1)
        item.pop("_panel_http_verified_at")
        item["last_heartbeat_at"] = 100
        self.assertIn("Reconectando", PANEL._connection_observation(item, now=101)["label"])

    def test_unknown_or_zero_rtt_is_never_shown_as_zero_millisecond_wan(self):
        for value in (None, 0, -1, "nan", "infinity", "bad"):
            with self.subTest(value=value):
                self.assertIsNone(PANEL._connection_observation(worker(100, rtt_ms=value), now=101)["rtt_ms"])

    def test_details_strip_endpoint_credentials_query_and_authorization(self):
        item = worker()
        item["endpoint"] = "http://private-user:private-password@127.0.0.1:18766/?token=private-query"
        item["status"]["connection"]["last_error"] = "Authorization: Bearer private-header-token"
        item["status"]["connection"]["generation"] = float("nan")
        text = PANEL._connection_detail_text(item)
        self.assertLessEqual(len(text), 1900)
        self.assertIn("127.0.0.1:18766", text)
        for secret in ("private-user", "private-password", "private-query", "private-header-token"):
            self.assertNotIn(secret, text)

    def test_guide_uses_termux_and_public_key_exchange_without_vpn_or_apk(self):
        text = PANEL._ssh_onboarding_text("phone-test")
        self.assertLessEqual(len(text), 1900)
        for expected in ("ssh-phone-vps.sh prepare", "prepare --bundle", "enroll", "activate", "Termux"):
            self.assertIn(expected, text)
        for forbidden in ("IP_TAILSCALE", "APK", "private-test-token"):
            self.assertNotIn(forbidden, text)
        self.assertTrue(text.endswith("restrição."), "guide must fit without cutting off its final instruction")

    def make_view(self, *, ssh=True, online=True):
        item = worker()
        if not ssh:
            item["status"].pop("connection")
        item["online"] = online
        snapshot = PANEL.WorkerSnapshot(True, True, online, name="Telefone", status=health(),
            registry_snapshot={"summary": {"registered": 1, "online": int(online)}, "workers": [item]})
        view = PANEL.WorkersPanelView()
        view.snapshot = snapshot
        view._selected_worker = lambda: item
        view._selected_is_legacy = lambda: False
        view._selected_is_auto = lambda: False
        view._selected_supported_tasks = lambda: set(item["supported_tasks"])
        view._registry_workers = lambda: [item]
        view._physical_group_for_worker_id = lambda _id: [item]
        view._group_parent = lambda _members: item
        view._group_apk = lambda _members: None
        view._group_display_name = lambda _members: "Telefone"
        view._physical_build_line = lambda _members: ""
        view._has_legacy_worker = lambda: False
        view._ensure_selected_worker = lambda: None
        view._clear_items = lambda: view.children.clear()
        view._worker_select_options = lambda: []
        view._refresh_panel = AsyncMock()
        return view

    def test_card_and_actions_expose_ssh_details_and_declared_diagnostic(self):
        view = self.make_view()
        values = [item["value"] for item in view._action_specs_for_selected()]
        self.assertIn("_connection_details", values)
        self.assertIn("ssh_connection_diagnostic", values)
        self.assertEqual(next(item for item in view._action_specs_for_selected() if item["value"] == "ping")["label"], "Testar conexão")
        text = "\n".join(view._selected_worker_lines(view.snapshot))
        self.assertIn("Conectado via SSH", text)
        self.assertIn("Edge · Teto", text)
        self.assertNotIn("PID", text)

    def test_legacy_worker_does_not_offer_ssh_diagnostic(self):
        with patch.dict(os.environ, {"PHONE_WORKER_TRANSPORT": "legacy"}):
            values = [item["value"] for item in self.make_view(ssh=False)._action_specs_for_selected()]
        self.assertNotIn("ssh_connection_diagnostic", values)
        self.assertNotIn("_connection_details", values)

    def test_unsupported_diagnostic_is_not_offered(self):
        view = self.make_view()
        view._selected_supported_tasks = lambda: {"ping"}
        self.assertNotIn("ssh_connection_diagnostic", [item["value"] for item in view._action_specs_for_selected()])

    def test_offline_ssh_layout_has_setup_guide_but_no_remote_wake_button(self):
        view = self.make_view(online=False)
        view._rebuild_layout()
        rows = [item for item in view.children[0].children if item.children and isinstance(item.children[0], Component)]
        labels = [getattr(child, "label", "") for row in rows for child in row.children]
        self.assertIn("Guia de conexão SSH", labels)
        self.assertNotIn("Acordar phone-worker", labels)
        self.assertTrue(all(len(row.children) <= 5 for row in rows))
        texts = [item.content for item in view.children[0].children if item.content]
        self.assertLess(sum(map(len, texts)), 4000)

    def test_legacy_offline_layout_preserves_existing_wake(self):
        with patch.dict(os.environ, {"PHONE_WORKER_TRANSPORT": "legacy"}):
            view = self.make_view(ssh=False, online=False)
            view._rebuild_layout()
        labels = [getattr(child, "label", "") for item in view.children[0].children for child in item.children]
        self.assertIn("Acordar phone-worker", labels)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, ENV, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cog = PANEL.WorkersCommandMixin()

    def opener(self, data):
        opener = Mock()
        opener.open.return_value = response(data)
        return opener

    def test_light_health_uses_authenticated_private_route_with_redirects_disabled(self):
        opener = self.opener(health())
        with patch.object(urllib.request, "build_opener", return_value=opener) as build:
            result = self.cog._request_worker_status_sync(timeout=1)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:18766/tts-agent/health")
        self.assertEqual(req.get_header("Authorization"), "Bearer private-test-token")
        proxy, redirect = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(redirect.redirect_request(None, None, None, None, None, None))
        self.assertEqual(result["_panel_http_verified_worker_id"], "phone-test")

    def test_cache_keeps_original_confirmation_and_returns_independent_data(self):
        opener = self.opener(health())
        with patch.object(urllib.request, "build_opener", return_value=opener):
            first = self.cog._request_worker_status_sync(timeout=1)
            second = self.cog._request_worker_status_sync(timeout=1)
            second["connection"]["state"] = "tampered"
            third = self.cog._request_worker_status_sync(timeout=1)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(first["_panel_http_verified_at"], third["_panel_http_verified_at"])
        self.assertEqual(third["connection"]["state"], "connected")

    def test_cache_expires_after_five_seconds_and_configuration_change(self):
        opener = self.opener(health())
        with patch.object(urllib.request, "build_opener", return_value=opener), patch.object(time, "monotonic", return_value=100):
            self.cog._request_worker_status_sync(timeout=1)
        with patch.object(urllib.request, "build_opener", return_value=opener), patch.object(time, "monotonic", return_value=106):
            self.cog._request_worker_status_sync(timeout=1)
            with patch.dict(os.environ, {"PHONE_WORKER_TOKEN": "rotated-private-token"}):
                self.cog._request_worker_status_sync(timeout=1)
        self.assertEqual(opener.open.call_count, 3)

    def test_wrong_identity_or_runtime_is_rejected_and_never_cached(self):
        for data in (health("wrong-phone"), health(runtime_kind="apk")):
            with self.subTest(data=data), patch.object(urllib.request, "build_opener", return_value=self.opener(data)):
                with self.assertRaises(RuntimeError):
                    self.cog._request_worker_status_sync(timeout=1)
                self.assertFalse(hasattr(self.cog, "_ssh_panel_status_cache"))

    def test_missing_identity_or_public_endpoint_fails_before_sending_token(self):
        for changes in ({"PHONE_WORKER_ID": ""}, {"PHONE_WORKER_HOST": "public.example"}):
            with self.subTest(changes=changes), patch.dict(os.environ, changes), patch.object(urllib.request, "build_opener") as build:
                with self.assertRaises(RuntimeError):
                    self.cog._request_worker_status_sync(timeout=1)
                build.assert_not_called()

    def test_full_diagnostic_bypasses_cache_and_requests_inventory(self):
        opener = self.opener(health())
        with patch.object(urllib.request, "build_opener", return_value=opener):
            self.cog._request_worker_status_sync(timeout=1)
            self.cog._request_worker_status_sync(timeout=6, full=True)
        self.assertEqual(opener.open.call_count, 2)
        self.assertTrue(opener.open.call_args.args[0].full_url.endswith("/status"))

    def test_oversized_status_is_rejected(self):
        opener = self.opener(health())
        opener.open.return_value.read.return_value = b"x" * (2*1024*1024+1)
        with patch.object(urllib.request, "build_opener", return_value=opener), self.assertRaisesRegex(RuntimeError, "2 MiB"):
            self.cog._request_worker_status_sync(timeout=1)


class SnapshotAndRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, ENV, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cog = PANEL.WorkersCommandMixin()

    async def test_light_probe_preserves_registry_inventory_and_only_marks_matching_identity(self):
        original = {"workers": [{"worker_id": "phone-test", "status": {"battery": {"level": 70}}},
                                {"worker_id": "another-phone", "status": {"battery": {"level": 25}}}]}
        live = {**health(), "_panel_http_verified_at": time.time(), "_panel_http_verified_worker_id": "phone-test"}
        self.cog._request_worker_status_sync = Mock(return_value=live)
        self.cog._collect_registry_snapshot = AsyncMock(return_value=(original, ""))
        async def direct(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        with patch.object(asyncio, "to_thread", side_effect=direct):
            snapshot = await self.cog._collect_workers_snapshot()
        self.assertTrue(snapshot.online)
        rows = snapshot.registry_snapshot["workers"]
        self.assertEqual(rows[0]["status"]["battery"]["level"], 70)
        self.assertIn("tts_agent", rows[0]["status"])
        self.assertEqual(rows[0]["_panel_http_verified_worker_id"], "phone-test")
        self.assertNotIn("_panel_http_verified_at", rows[1])
        self.assertNotIn("_panel_http_verified_at", original["workers"][0])

    async def test_ssh_wake_paths_do_not_execute_legacy_remote_shell(self):
        self.cog._collect_workers_snapshot = AsyncMock(return_value="snapshot")
        result = await self.cog._run_phone_worker_watch_script(force=True)
        self.assertTrue(result["ssh_managed"])
        self.assertEqual(await self.cog._wake_phone_worker(force=True), "snapshot")
        self.assertIn("abra o Termux", self.cog._collect_workers_snapshot.call_args.kwargs["action_note"])
        snapshot = PANEL.WorkerSnapshot(True, True, False)
        self.assertFalse(self.cog._snapshot_needs_auto_wake(snapshot))
        with patch.object(asyncio, "create_task") as create_task:
            self.cog._start_core_worker_auto_wake_task()
        create_task.assert_not_called()

    async def test_manual_self_check_requests_full_inventory_not_cached_light_health(self):
        self.cog._request_worker_status_sync = Mock(return_value={"ok": True})
        async def direct(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        with patch.object(asyncio, "to_thread", side_effect=direct):
            await self.cog._run_legacy_worker_action(job_type="worker_self_check")
        self.assertTrue(self.cog._request_worker_status_sync.call_args.kwargs["full"])
        self.assertGreaterEqual(self.cog._request_worker_status_sync.call_args.kwargs["timeout"], 6)


if __name__ == "__main__":
    unittest.main()
