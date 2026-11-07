"""Tests for oracle.py. Run with: python3 -m unittest discover tests"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("oracle", ROOT / "oracle.py")
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)

opener = build_opener(ProxyHandler({}))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def wait_for(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oracle-test-"))
        self.db = self.tmp / "db"
        self.db.mkdir()
        patches = [
            mock.patch.object(oracle, "CONFIG_FILE", self.tmp / "oracle.config.json"),
            mock.patch.object(oracle, "LOG_DIR", self.tmp / "logs"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class DetectTest(TempDirCase):
    def test_static_site_with_http_server_script(self):
        write(self.db / "a" / "index.html", "<h1>a</h1>")
        write(self.db / "a" / "package.json", json.dumps({"scripts": {"start": "python3 -m http.server 8010"}}))
        self.assertEqual(oracle.detect(self.db / "a")["kind"], "static")
        self.assertEqual(oracle.detect(self.db / "a")["port"], 8010)

    def test_static_site_with_serve_py(self):
        write(self.db / "a" / "index.html", "")
        write(self.db / "a" / "package.json", json.dumps({"scripts": {"serve": "python3 scripts/serve.py 8007"}}))
        self.assertEqual(oracle.detect(self.db / "a")["port"], 8007)

    def test_node_server_and_its_default_port(self):
        write(self.db / "a" / "package.json", json.dumps({"scripts": {"start": "node server/index.js"}}))
        write(self.db / "a" / "server" / "index.js", "const port = Number(process.env.PORT) || 4133;")
        spec = oracle.detect(self.db / "a")
        self.assertEqual((spec["kind"], spec["entry"], spec["port"]), ("node", "server/index.js", 4133))

    def test_port_fallback_inside_number_call(self):
        write(self.db / "a" / "server.js", "const PORT = Number(process.env.PORT || 4135);")
        self.assertEqual(oracle.port_in_file(self.db / "a" / "server.js"), 4135)
        write(self.db / "b" / "server.js", "const port = process.env.PORT ?? '3000';")
        self.assertEqual(oracle.port_in_file(self.db / "b" / "server.js"), 3000)

    def test_plain_index_html_takes_the_port_from_the_readme(self):
        write(self.db / "a" / "index.html", "")
        write(self.db / "a" / "README.md", "# Atlante\n\nRun `python3 -m http.server 8099`, open http://localhost:8099")
        spec = oracle.detect(self.db / "a")
        self.assertEqual((spec["kind"], spec["port"], spec["title"]), ("static", 8099, "Atlante"))

    def test_public_folder(self):
        write(self.db / "a" / "public" / "index.html", "")
        self.assertEqual(oracle.detect(self.db / "a")["root"], "public")

    def test_other_folders_are_ignored(self):
        write(self.db / "a" / "notes.txt", "")
        self.assertIsNone(oracle.detect(self.db / "a"))


class ScanTest(TempDirCase):
    REGISTRY = {
        "lit-atlas": {"name": "lit-atlas", "kind": "static", "port": 8010, "repo": "me/lit-atlas"},
        "story": {"name": "story", "kind": "node", "entry": "server.js", "port": 4135,
                  "repo": "me/story", "branch": "claude/app"},
    }

    def test_known_unknown_and_missing_atlases(self):
        write(self.db / "lit-atlas" / "index.html", "")
        write(self.db / "my-map" / "index.html", "")                      # not in the registry
        write(self.db / "my-map" / "README.md", "open http://localhost:8010")  # clashes with lit-atlas
        write(self.db / "node_modules" / "index.html", "")                # never an atlas
        atlases = oracle.scan(self.db, self.REGISTRY)
        self.assertEqual(list(atlases), ["lit-atlas", "my-map", "story"])
        self.assertEqual(atlases["lit-atlas"].port, 8010)
        self.assertTrue(atlases["my-map"].auto_port)
        self.assertEqual(atlases["my-map"].port, oracle.FIRST_FREE_PORT)
        self.assertIsNone(atlases["story"].path)
        self.assertEqual(atlases["story"].problem, "not in the folder")

    def test_clone_in_a_differently_named_folder_is_matched_by_its_remote(self):
        write(self.db / "LitAtlas-copy" / "index.html", "")
        write(self.db / "LitAtlas-copy" / ".git" / "config",
              '[remote "origin"]\n\turl = https://github.com/me/lit-atlas.git\n')
        atlases = oracle.scan(self.db, self.REGISTRY)
        self.assertEqual(atlases["lit-atlas"].path.name, "LitAtlas-copy")

    def test_app_on_another_branch_is_explained(self):
        write(self.db / "story" / "docs" / "book.epub", "")
        problem = oracle.scan(self.db, self.REGISTRY)["story"].problem
        self.assertIn("claude/app", problem)
        self.assertIn("oracle.py sync story", problem)


class InstallAndEngineTest(TempDirCase):
    def test_needs_install_follows_node_modules(self):
        app = self.db / "app"
        write(app / "package.json", json.dumps({"dependencies": {"express": "^4"}}))
        self.assertTrue(oracle.needs_install(app))
        write(app / "node_modules" / ".package-lock.json", "{}")
        future = time.time() + 10
        os.utime(app / "node_modules" / ".package-lock.json", (future, future))
        self.assertFalse(oracle.needs_install(app))
        os.utime(app / "package.json", (future + 10, future + 10))
        self.assertTrue(oracle.needs_install(app))

    def test_no_dependencies_no_install(self):
        write(self.db / "app" / "package.json", json.dumps({"scripts": {"start": "node server.js"}}))
        self.assertFalse(oracle.needs_install(self.db / "app"))

    def test_engine_check(self):
        write(self.db / "app" / "package.json", json.dumps({"engines": {"node": ">=20.12"}}))
        with mock.patch.object(oracle, "node_version", return_value=(18, 19, 0)):
            self.assertIn("needs Node.js >=20.12", oracle.engine_problem(self.db / "app", "node"))
        with mock.patch.object(oracle, "node_version", return_value=(22, 1, 0)):
            self.assertIsNone(oracle.engine_problem(self.db / "app", "node"))


class ConfigTest(TempDirCase):
    def test_selection_is_saved_as_a_skip_list(self):
        sup = oracle.Supervisor(self.db, ScanTest.REGISTRY, oracle.load_config(), dashboard_port=free_port(), quiet=True)
        self.assertTrue(sup.is_selected("story"))
        sup.set_selected("story", False)
        self.assertEqual(oracle.load_config()["skip"], ["story"])
        sup.set_selected("story", True)
        self.assertEqual(oracle.load_config()["skip"], [])

    def test_db_resolution_order(self):
        cfg = {"db": str(self.tmp / "from-config")}
        self.assertEqual(oracle.resolve_db(None, cfg), (self.tmp / "from-config").resolve())
        self.assertEqual(oracle.resolve_db(str(self.tmp / "arg"), cfg), (self.tmp / "arg").resolve())
        with mock.patch.dict(os.environ, {"ORACLE_DB": str(self.tmp / "env")}):
            self.assertEqual(oracle.resolve_db(None, cfg), (self.tmp / "env").resolve())


class RunnerTest(TempDirCase):
    def atlas(self, name, spec):
        return oracle.scan(self.db, {name: dict(spec, name=name)})[name]

    def test_static_atlas_starts_serves_and_stops(self):
        port = free_port()
        write(self.db / "site" / "index.html", "<h1>hello</h1>")
        write(self.db / "site" / "app.js", "export {}")
        r = oracle.Runner(self.atlas("site", {"kind": "static", "port": port}), "127.0.0.1")
        r.start()
        self.assertTrue(wait_for(lambda: r.state == "running"), r.message)
        with opener.open(f"http://127.0.0.1:{port}/app.js", timeout=5) as resp:
            self.assertEqual(resp.headers["Content-Type"], "text/javascript")
            self.assertEqual(resp.headers["Cache-Control"], "no-cache")
        r.stop()
        self.assertEqual(r.state, "stopped")
        self.assertFalse(oracle.port_open(port))

    def test_port_already_in_use_is_left_alone(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            write(self.db / "site" / "index.html", "")
            r = oracle.Runner(self.atlas("site", {"kind": "static", "port": port}), "127.0.0.1")
            r.start()
            self.assertTrue(wait_for(lambda: r.state == "external"))
            self.assertIsNone(r.proc)

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_node_atlas_gets_its_port_and_a_crash_is_reported(self):
        port = free_port()
        write(self.db / "app" / "server.js",
              "const http = require('http');\n"
              "const s = http.createServer((q, r) => { r.end('ok'); if (q.url === '/die') process.exit(3); });\n"
              "s.listen(Number(process.env.PORT), process.env.HOST);\n")
        r = oracle.Runner(self.atlas("app", {"kind": "node", "entry": "server.js", "port": port}), "127.0.0.1")
        r.start()
        self.assertTrue(wait_for(lambda: r.state == "running"), r.message)
        try:
            opener.open(f"http://127.0.0.1:{port}/die", timeout=5).read()
        except OSError:
            pass
        self.assertTrue(wait_for(lambda: r.state == "crashed"))
        self.assertIn("exit code 3", r.message)
        r.start()                                   # restarting after a crash works
        self.assertTrue(wait_for(lambda: r.state == "running"), r.message)
        r.stop()
        self.assertEqual(r.state, "stopped")

    def test_broken_atlas_does_not_start(self):
        write(self.db / "site" / "README.md", "")
        r = oracle.Runner(self.atlas("site", {"kind": "static", "port": free_port()}), "127.0.0.1")
        self.assertEqual(r.state, "error")
        r.start()
        self.assertTrue(wait_for(lambda: r.state == "error"))
        self.assertIsNone(r.proc)


class DashboardTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.site_port = free_port()
        write(self.db / "site" / "index.html", "")
        registry = {"site": {"name": "site", "kind": "static", "port": self.site_port}}
        self.sup = oracle.Supervisor(self.db, registry, oracle.load_config(), dashboard_port=self.port, quiet=True)
        self.httpd = oracle.make_dashboard(self.sup, self.port)
        import threading
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.sup.stop_all()
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def request(self, path, post=False, headers=None):
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=b"{}" if post else None,
                      headers=headers or {}, method="POST" if post else "GET")
        try:
            with opener.open(req, timeout=5) as resp:
                return resp.status, resp.read()
        except Exception as e:                       # HTTPError carries the status
            return getattr(e, "code", None), b""

    def test_status_and_page(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")   # private unless LAN mode
        code, body = self.request("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["atlases"][0]["name"], "site")
        code, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(b"<title>Oracle atlases</title>", body)

    def test_posts_need_the_oracle_header(self):
        self.assertEqual(self.request("/api/atlas/site/start", post=True)[0], 403)
        code, _ = self.request("/api/atlas/site/start", post=True, headers={"X-Oracle": "1"})
        self.assertEqual(code, 200)
        self.assertTrue(wait_for(lambda: oracle.port_open(self.site_port)))

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.request("/api/status", headers={"Host": "evil.example"})[0], 403)

    def test_call_oracle_finds_a_running_instance(self):
        self.assertEqual(oracle.call_oracle(self.port, "/api/status")["app"], "oracle")
        self.assertIsNone(oracle.call_oracle(free_port(), "/api/status"))


class ClientScopeTest(unittest.TestCase):
    def test_addresses(self):
        cases = {
            "127.0.0.1": "local", "::1": "local",
            "192.168.1.20": "lan", "10.0.0.7": "lan", "172.16.4.2": "lan", "fe80::1": "lan",
            "::ffff:192.168.1.5": "lan", "100.101.1.2": "lan",          # Tailscale
            "8.8.8.8": "public", "2a00:1450::1": "public", "nonsense": "public",
        }
        for address, scope in cases.items():
            self.assertEqual(oracle.client_scope(address), scope, address)

    def test_codes_ignore_case_spaces_and_dashes(self):
        self.assertEqual(oracle.norm_code(" K7MP q2xa "), oracle.norm_code("k7mp-q2xa"))
        self.assertRegex(oracle.new_code(), r"^[a-z2-9]{4}-[a-z2-9]{4}$")


class LanConfigTest(TempDirCase):
    def run_config(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return oracle.cmd_config(oracle.build_parser().parse_args(["config", *argv]))

    def test_turning_lan_on_creates_an_access_code(self):
        self.assertEqual(self.run_config("--lan", "on"), 0)
        cfg = oracle.load_config()
        self.assertTrue(cfg["lan"])
        self.assertRegex(cfg["access_code"], r"^[a-z2-9]{4}-[a-z2-9]{4}$")
        self.assertTrue(cfg["session_secret"])
        old = cfg["access_code"]
        self.run_config("--new-code")
        self.assertNotEqual(oracle.load_config()["access_code"], old)
        self.assertEqual(self.run_config("--code", "a-b"), 1)          # too short
        self.run_config("--code", "studio 42")
        self.assertEqual(oracle.load_config()["access_code"], "studio 42")

    def test_dashboard_toggles_keep_a_code_set_from_the_command_line(self):
        sup = oracle.Supervisor(self.db, {}, oracle.load_config(), dashboard_port=free_port(), quiet=True)
        self.run_config("--code", "changed-meanwhile")
        sup.set_selected("anything", False)
        cfg = oracle.load_config()
        self.assertEqual((cfg["access_code"], cfg["skip"]), ("changed-meanwhile", ["anything"]))


class RemoteDashboardTest(TempDirCase):
    """Another device on the network, simulated by faking the client address.

    The test server still listens on 127.0.0.1 only."""

    def setUp(self):
        super().setUp()
        cfg = oracle.load_config()
        oracle.ensure_access_code(cfg)
        cfg["access_code"] = "k7mp-q2xa"
        oracle.save_config(cfg)
        self.port = free_port()
        self.sup = oracle.Supervisor(self.db, {}, cfg, dashboard_port=self.port, quiet=True)
        self.sup.lan = True
        self.httpd = oracle.make_dashboard(self.sup, self.port, bind="127.0.0.1")
        self.ip = "192.168.1.50"
        patch = mock.patch.object(oracle.DashboardHandler, "_client_ip", lambda handler: self.ip)
        patch.start()
        self.addCleanup(patch.stop)
        import threading
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def request(self, path, body=None, cookie=None, header=True):
        headers = {"Content-Type": "application/json"}
        if header:
            headers["X-Oracle"] = "1"
        if cookie:
            headers["Cookie"] = cookie
        req = Request(f"http://127.0.0.1:{self.port}{path}",
                      data=None if body is None else json.dumps(body).encode(),
                      headers=headers, method="GET" if body is None else "POST")
        try:
            with opener.open(req, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except Exception as e:
            return getattr(e, "code", None), b"", getattr(e, "headers", {})

    def login(self, code="k7mp-q2xa"):
        status, _, headers = self.request("/api/login", {"code": code})
        cookie = (headers.get("Set-Cookie") or "").split(";")[0] if status == 200 else None
        return status, cookie

    def test_the_page_loads_but_the_api_wants_the_code(self):
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/api/status")[0], 401)
        self.assertEqual(self.request("/api/rescan", {})[0], 401)

    def test_right_code_gives_a_session(self):
        self.assertEqual(self.login("wrong-code")[0], 401)
        status, cookie = self.login(" K7MP Q2XA ")
        self.assertEqual(status, 200)
        self.assertTrue(cookie.startswith("oracle_session="))
        status, body, _ = self.request("/api/status", cookie=cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["client"], "lan")
        self.assertNotIn("access_code", data)       # never shown to other devices

    def test_cookie_is_http_only_and_same_site(self):
        status, _, headers = self.request("/api/login", {"code": "k7mp-q2xa"})
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])

    def test_other_devices_cannot_quit_oracle(self):
        _, cookie = self.login()
        self.assertEqual(self.request("/api/shutdown", {}, cookie=cookie)[0], 403)

    def test_posts_still_need_the_oracle_header(self):
        _, cookie = self.login()
        self.assertEqual(self.request("/api/rescan", {}, cookie=cookie, header=False)[0], 403)
        self.assertEqual(self.request("/api/rescan", {}, cookie=cookie)[0], 200)

    def test_too_many_wrong_codes(self):
        for _ in range(oracle.LOGIN_LIMIT):
            self.assertEqual(self.login("nope-nope")[0], 401)
        self.assertEqual(self.login()[0], 429)       # even the right one, for a while
        self.ip = "192.168.1.51"                    # other devices are not affected
        self.assertEqual(self.login()[0], 200)

    def test_changing_the_code_signs_devices_out(self):
        _, cookie = self.login()
        cfg = oracle.load_config()
        cfg["access_code"] = "brand-new1"
        oracle.save_config(cfg)
        self.assertEqual(self.request("/api/status", cookie=cookie)[0], 401)

    def test_public_addresses_are_refused(self):
        self.ip = "8.8.8.8"
        self.assertEqual(self.request("/")[0], 403)
        self.assertEqual(self.login()[0], 403)

    def test_without_lan_mode_other_devices_are_refused(self):
        self.sup.lan = False
        self.assertEqual(self.request("/")[0], 403)

    def test_this_computer_needs_no_code_and_sees_it(self):
        self.ip = "127.0.0.1"
        status, body, _ = self.request("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual((data["client"], data["access_code"]), ("local", "k7mp-q2xa"))
        self.assertTrue(data["remote_url"].endswith(f":{self.port}"))


class AutostartTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux autostart entry")
    def test_desktop_entry(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            self.assertEqual(oracle.autostart_file(), Path(d) / "autostart" / "oracle-atlases.desktop")
            text = oracle.autostart_content()
            self.assertIn("start --no-browser", text)
            self.assertIn(str(ROOT / "oracle.py"), text)


if __name__ == "__main__":
    unittest.main()
