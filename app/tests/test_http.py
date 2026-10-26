import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

os.environ.setdefault("FLEET_PULSE_DB", os.path.join(tempfile.mkdtemp(), "h.db"))
os.environ["FLEET_PULSE_TOKEN"] = "test-token"
_ui = tempfile.mkdtemp()
with open(os.path.join(_ui, "index.html"), "w") as f:
    f.write("<!doctype html><title>Fleet Pulse</title>")
os.environ["UI_DIR"] = _ui

from fleetpulse_server import main, store  # noqa: E402

main.TOKEN = "test-token"
main.UI_DIR = os.path.realpath(_ui)


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.db()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read().decode()
        conn.close()
        return r.status, data

    def test_writes_need_the_token(self):
        body = json.dumps({"app": "x", "result": "promoted"})
        self.assertEqual(self.request("POST", "/api/deploys", body)[0], 401)
        self.assertEqual(self.request("POST", "/api/deploys", body, {"Authorization": "Bearer wrong"})[0], 401)
        status, _ = self.request("POST", "/api/deploys", body,
                                 {"Authorization": "Bearer test-token", "Content-Type": "application/json"})
        self.assertEqual(status, 201)

    def test_bad_bodies_get_400_not_a_dropped_connection(self):
        auth = {"Authorization": "Bearer test-token"}
        self.assertEqual(self.request("POST", "/api/alertmanager", "[1,2]", auth)[0], 400)
        self.assertEqual(self.request("POST", "/api/alertmanager", "{not json", auth)[0], 400)
        self.assertEqual(self.request("POST", "/api/deploys", json.dumps({"app": "x", "result": "bogus"}), auth)[0], 400)
        self.assertEqual(self.request("POST", "/api/alertmanager", "{}", {**auth, "Content-Length": "-1"})[0], 400)

    def test_known_cause_needs_the_token_and_an_open_incident(self):
        iid = store.create_incident("x", "x: Down", "critical", None, store.now(), 0)
        path = f"/api/incidents/{iid}/ack"
        auth = {"Authorization": "Bearer test-token"}
        body = json.dumps({"note": "drill"})
        self.assertEqual(self.request("POST", path, body)[0], 401)
        self.assertEqual(self.request("POST", path, json.dumps({"note": " "}), auth)[0], 400)
        self.assertEqual(self.request("POST", path, json.dumps({"note": "x" * 501}), auth)[0], 400)
        self.assertEqual(self.request("POST", path, body, auth)[0], 200)
        self.assertEqual(self.request("POST", f"/api/incidents/{iid + 999}/ack", body, auth)[0], 404)
        store.close_incident(iid, store.now(), "")
        self.assertEqual(self.request("POST", path, body, auth)[0], 409)

    def test_static_paths_cannot_leave_the_ui_directory(self):
        for p in ("/../../etc/passwd", "/%2e%2e/%2e%2e/etc/passwd", "//..//..//etc/passwd"):
            status, body = self.request("GET", p)
            self.assertEqual(status, 200)
            self.assertIn("Fleet Pulse", body)

    def test_health_touches_the_database(self):
        self.assertEqual(self.request("GET", "/health"), (200, "ok"))


if __name__ == "__main__":
    unittest.main()
