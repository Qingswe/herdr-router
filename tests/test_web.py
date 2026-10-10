import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from router import model, web


class WebApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
            p["enabled"] = p["id"] == "opencode-default"
        status, out = self.req("/api/route", "POST", {"task": "rename x", "policy": draft, "use_backend": False})
        self.assertEqual(status, 200)
        self.assertEqual(out["decision"]["profile_id"], "opencode-default")


if __name__ == "__main__":
    unittest.main()
