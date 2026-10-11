import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from router import model, web


class WebApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # API/scoring tests simulate installed CLIs and never discover real accounts/models.
        available = lambda binary: f"/test/bin/{binary}"
        for name in ("decide", "rank_at", "territory"):
            cls.enterClassContext(mock.patch.object(web, name, partial(getattr(web, name), which=available)))
        cls.enterClassContext(mock.patch.object(web, "_installed_kinds",
                                               return_value={kind: True for kind in model.KNOWN_KINDS}))
        for name in ("codex_models", "cursor_models", "opencode_models"):
            cls.enterClassContext(mock.patch.object(web.catalog, name, return_value=[]))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": f"{cls.tmp.name}/cfg",
                                               "HERDR_PLUGIN_STATE_DIR": f"{cls.tmp.name}/state"})
        cls.env.start()
        web.Handler.token = cls.token = web.get_token()
        web.Handler.extra_hosts = set()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env.stop()
        cls.tmp.cleanup()

    def req(self, path, method="GET", body=None, token=True, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if token:
            h["X-Router-Token"] = self.token
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_token_required(self):
        self.assertEqual(self.req("/api/policy", token=False)[0], 401)
        self.assertEqual(self.req("/api/policy", headers={"X-Router-Token": "x" * 43}, token=False)[0], 401)
        self.assertEqual(self.req("/api/policy")[0], 200)

    def test_cookie_auth(self):
        status, _ = self.req("/api/policy", token=False, headers={"Cookie": f"hr_token={self.token}"})
        self.assertEqual(status, 200)

    def test_rebinding_host_rejected(self):
        self.assertEqual(self.req("/api/policy", headers={"Host": "evil.example:80"})[0], 403)

    def test_static_is_public(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            self.assertIn(b"herdr-router", r.read())

    def test_editor_language_is_selected_client_side(self):
        for language in ("en-US,en;q=0.9", "zh-CN,zh;q=0.9", "fr"):
            request = urllib.request.Request(self.base + "/", headers={"Accept-Language": language})
            with urllib.request.urlopen(request, timeout=5) as response:
                html = response.read().decode()
                self.assertIn('<html lang="en">', html)
                self.assertIn('data-i18n="save"', html)
                self.assertLess(html.index('/i18n.js'), html.index('/app.js'))
        with urllib.request.urlopen(self.base + "/i18n.js", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/javascript", response.headers["Content-Type"])
            script = response.read().decode()
            self.assertIn("navigator.languages", script)
            self.assertIn('"en":', script)
            self.assertIn('"zh":', script)

    def test_put_validates_then_saves(self):
        _, body = self.req("/api/policy")
        bad = copy.deepcopy(body["policy"])
        bad["profiles"][0]["kind"] = "nope"
        status, out = self.req("/api/policy", "PUT", bad)
        self.assertEqual(status, 422)
        self.assertEqual(out["errors"][0]["path"], "profiles[0].kind")
        good = copy.deepcopy(body["policy"])
        good["scoring"]["cost_weight"] = 0.07
        self.assertEqual(self.req("/api/policy", "PUT", good)[0], 200)
        self.assertEqual(model.load()["scoring"]["cost_weight"], 0.07)

    def test_form_post_rejected(self):
        r = urllib.request.Request(self.base + "/api/policy", data=b"a=b", method="PUT",
                                   headers={"X-Router-Token": self.token,
                                            "Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_route_dry_run_on_draft(self):
        _, body = self.req("/api/policy")
        draft = copy.deepcopy(body["policy"])
        for p in draft["profiles"]:
            p["enabled"] = p["id"] == "mimo-pro"
        status, out = self.req("/api/route", "POST", {"task": "rename x", "policy": draft, "use_backend": False})
        self.assertEqual(status, 200)
        self.assertEqual(out["decision"]["profile_id"], "mimo-pro")
        self.assertEqual(out["decision"]["args"], ["mini", "-m", "opencode-go/mimo-v2.6-pro"])


    def test_new_endpoints_need_token(self):
        self.assertEqual(self.req("/api/models", token=False)[0], 401)
        self.assertEqual(self.req("/api/map", "POST", {"x": "duration", "y": "difficulty"}, token=False)[0], 401)

    def test_map(self):
        status, out = self.req("/api/map", "POST", {"x": "duration", "y": "difficulty", "choice": {"task_type": "review"}})
        self.assertEqual(status, 200)
        self.assertEqual(len(out["grid"]), 48)
        self.assertEqual(out["x"], [-0.5, 3.5])
        status, out = self.req("/api/map", "POST", {"x": "duration", "y": "difficulty", "at": [0, 0]})
        self.assertEqual((status, len(out["top"])), (200, 3))
        self.assertEqual(self.req("/api/map", "POST", {"x": "duration", "y": "difficulty", "at": "x"})[0], 400)
        self.assertEqual(self.req("/api/map", "POST", {"x": "task_type", "y": "difficulty"})[0], 400)
        _, body = self.req("/api/policy")
        bad = copy.deepcopy(body["policy"])
        bad["profiles"][0]["point"]["difficulty"] = 9
        self.assertEqual(self.req("/api/map", "POST", {"policy": bad, "x": "duration", "y": "difficulty"})[0], 422)

    def test_models(self):
        with mock.patch("router.catalog.cursor_models", return_value=[]):
            status, out = self.req("/api/models")
        self.assertEqual(status, 200)
        self.assertIn("opus", [m["id"] for m in out["models"]["claude"]])


class EditorStartupTest(unittest.TestCase):
    def test_first_launch_creates_state_directory_and_returns_authenticated_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "new-state"
            with mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(state)}), \
                    mock.patch.object(web, "running_info", side_effect=[None, {"port": 8798}]), \
                    mock.patch.object(web.subprocess, "Popen") as launch, \
                    mock.patch.object(web.time, "sleep"):
                url = web.ensure_running(port=8798)
                self.assertEqual(url, f"http://127.0.0.1:8798/?token={web.get_token()}")
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[0][-2:], ["--port", "8798"])
                self.assertTrue((state / "ui.log").is_file())
                self.assertTrue(launch.call_args.kwargs["stdout"].closed)


if __name__ == "__main__":
    unittest.main()
