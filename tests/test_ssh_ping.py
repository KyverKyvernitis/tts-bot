"""The SSH roundtrip authenticates identity without collecting bot inventory."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class PingTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "webserver.py").read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "core_worker_agent_ping")
        function.decorator_list = []
        self.responses = []
        def jsonify(body):
            self.responses.append(body)
            return SimpleNamespace(headers={}, data=json.dumps(body).encode())
        self.auth = mock.Mock(return_value=(True, None, "private-worker-token"))
        self.ns = {"request": SimpleNamespace(args={"worker_id": "phone-test"}, headers={}),
                   "_authenticate_core_worker_agent_request": self.auth, "jsonify": jsonify,
                   "time": SimpleNamespace(time=lambda: 12345),
                   "_health_provider": mock.Mock(side_effect=AssertionError("expensive inventory"))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "webserver.py", "exec"), self.ns)

    def test_authenticated_identity_is_small_and_never_contains_credentials(self):
        response, status = self.ns["core_worker_agent_ping"]()
        self.assertEqual(status, 200)
        self.auth.assert_called_once_with("phone-test")
        body = json.loads(response.data)
        self.assertTrue(body["ok"])
        self.assertEqual(body["worker_id"], "phone-test")
        self.assertEqual(body["service"], "osaka-phone-ssh")
        self.assertLess(len(response.data), 256)
        self.assertNotIn("private-worker-token", response.data.decode())
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.ns["_health_provider"].assert_not_called()

    def test_authentication_failure_never_returns_a_success_or_inventory(self):
        denied = ({"ok": False, "error": "não autorizado"}, 403)
        self.auth.return_value = (False, denied, "")
        self.assertEqual(self.ns["core_worker_agent_ping"](), denied)
        self.assertFalse(self.responses)

    def test_header_identity_is_also_authenticated_and_never_inferred(self):
        self.ns["request"].args = {}
        self.ns["request"].headers = {"X-Core-Worker-Id": "other-phone"}
        self.ns["core_worker_agent_ping"]()
        self.auth.assert_called_once_with("other-phone")
        self.assertEqual(self.responses[0]["worker_id"], "other-phone")


if __name__ == "__main__":
    unittest.main()
