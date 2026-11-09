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


IP_ADDR = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: wlp3s0    inet 192.168.1.20/24 brd 192.168.1.255 scope global dynamic wlp3s0\\       valid_lft 80000sec
3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever
4: tailscale0    inet 100.101.2.3/32 scope global tailscale0\\       valid_lft forever
"""

IFCONFIG_MAC = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether a4:83:e7:00:00:00
\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet 10.8.0.2 --> 10.8.0.2 netmask 0xffffffff
"""

IPCONFIG_IT = """
Configurazione IP di Windows

Scheda Ethernet vEthernet (WSL (Hyper-V firewall)):

   Suffisso DNS specifico per connessione:
   Indirizzo IPv4. . . . . . . . . . . . : 172.28.112.1
   Subnet mask . . . . . . . . . . . . . : 255.255.240.0

Scheda LAN wireless Wi-Fi:

   Suffisso DNS specifico per connessione: home
   Indirizzo IPv4. . . . . . . . . . . . : 192.168.1.20
   Gateway predefinito . . . . . . . . . : 192.168.1.1
"""


class AddressTest(unittest.TestCase):
    def setUp(self):
        oracle._addresses = (0.0, [])

    def test_parsers(self):
        self.assertIn(("wlp3s0", "192.168.1.20"), oracle.parse_ip_addr(IP_ADDR))
        self.assertIn(("en0", "192.168.1.20"), oracle.parse_ifconfig(IFCONFIG_MAC))
        self.assertIn(("utun4", "10.8.0.2"), oracle.parse_ifconfig(IFCONFIG_MAC))
        self.assertEqual(oracle.parse_ipconfig(IPCONFIG_IT),
                         [("Scheda Ethernet vEthernet (WSL (Hyper-V firewall))", "172.28.112.1"),
                          ("Scheda LAN wireless Wi-Fi", "192.168.1.20")])

    def test_kinds(self):
        self.assertEqual(oracle.address_kind("en0", "192.168.1.20"), "")
        self.assertEqual(oracle.address_kind("utun4", "10.8.0.2"), "VPN")
        self.assertEqual(oracle.address_kind("tailscale0", "100.101.2.3"), "VPN")
        self.assertEqual(oracle.address_kind("docker0", "172.17.0.1"), "virtual")
        self.assertEqual(oracle.address_kind("Scheda Ethernet vEthernet (WSL)", "172.28.112.1"), "virtual")

    def test_real_adapter_wins_even_when_a_vpn_has_the_default_route(self):
        with mock.patch.object(oracle, "IS_WIN", False), \
             mock.patch.object(oracle.shutil, "which", side_effect=lambda n: "/sbin/ip" if n == "ip" else None), \
             mock.patch.object(oracle, "run_quiet", return_value=IP_ADDR), \
             mock.patch.object(oracle, "route_ip", return_value="100.101.2.3"):
            addrs = oracle.local_addresses(max_age=0)
        self.assertEqual([ip for _, ip, _ in addrs], ["192.168.1.20", "100.101.2.3", "172.17.0.1"])
        self.assertEqual(oracle.best_lan_ip(), "192.168.1.20")

    def test_windows_wsl_adapter_goes_last(self):
        with mock.patch.object(oracle, "IS_WIN", True), \
             mock.patch.object(oracle, "run_quiet", return_value=IPCONFIG_IT), \
             mock.patch.object(oracle, "route_ip", return_value="192.168.1.20"):
            addrs = oracle.local_addresses(max_age=0)
        self.assertEqual(addrs[0][1], "192.168.1.20")
        self.assertEqual(addrs[1][2], "virtual")


WIN_PUBLIC_BLOCKED = """PROFILE|Wi-Fi|Public
FW|Domain|True
FW|Private|True
FW|Public|True
APP|Block|Public|C:\\Python312\\python.exe
APP|Allow|Private|%ProgramFiles%\\nodejs\\node.exe
"""


class FirewallTest(TempDirCase):
    def test_windows_public_network_and_block_rule(self):
        programs = [("Python", "C:\\Python312\\python.exe"), ("Node.js", "C:\\Program Files\\nodejs\\node.exe")]
        rep = oracle.Report()
        with mock.patch.object(oracle, "firewall_programs", return_value=programs), \
             mock.patch.dict(os.environ, {"ProgramFiles": "C:\\Program Files"}):
            oracle.check_windows_firewall(rep, [8800, 8010], text=WIN_PUBLIC_BLOCKED)
        texts = " ".join(t for _, _, t in rep.rows)
        self.assertIn("'Wi-Fi' is set to Public", texts)
        self.assertIn("Python is blocked", texts)
        self.assertIsNotNone(rep.fix)
        self.assertIn("8800, 8010", rep.fix_plan[0])
        self.assertTrue(any("Public" in problem for problem, _ in rep.todo))

    def test_windows_rule_already_there(self):
        text = "PROFILE|Wi-Fi|Private\nFW|Private|True\nORACLE|True|Private, Domain|8800,8010,4135\n"
        rep = oracle.Report()
        with mock.patch.object(oracle, "firewall_programs", return_value=[("Python", "C:\\py\\python.exe")]):
            oracle.check_windows_firewall(rep, [8800, 8010], text=text)
        self.assertIsNone(rep.fix)
        self.assertEqual(rep.todo, [])

    def test_windows_firewall_off(self):
        rep = oracle.Report()
        oracle.check_windows_firewall(rep, [8800], text="PROFILE|Wi-Fi|Private\nFW|Private|False\n")
        self.assertIn(("ok", "Firewall", "off for this network"), rep.rows)
        self.assertIsNone(rep.fix)

    def test_mac_blocked_python(self):
        answers = {"--getglobalstate": "Firewall is enabled. (State = 1)",
                   "--getblockall": "Firewall has block all state set to disabled.",
                   "--getappblocked": "The application /fw/Python is blocked from incoming connections."}
        rep = oracle.Report()
        with mock.patch.object(oracle.os.path, "exists", return_value=True), \
             mock.patch.object(oracle, "run_quiet", side_effect=lambda cmd, **kw: answers[cmd[1]]), \
             mock.patch.object(oracle, "firewall_programs", return_value=[("Python", "/fw/Python")]):
            oracle.check_mac_firewall(rep, [8800])
        self.assertIn(("!!", "", "Python: blocked  (/fw/Python)"), rep.rows)
        self.assertIn("--unblockapp", rep.fix_plan[1])

    def test_mac_firewall_off(self):
        rep = oracle.Report()
        with mock.patch.object(oracle.os.path, "exists", return_value=True), \
             mock.patch.object(oracle, "run_quiet", return_value="Firewall is disabled. (State = 0)"):
            oracle.check_mac_firewall(rep, [8800])
        self.assertEqual(rep.rows, [("ok", "Firewall", "off")])

    def test_linux_ufw_rules_stay_on_private_networks(self):
        rep = oracle.Report()
        ports = list(range(8000, 8020))                     # more than ufw's 15 per rule
        with mock.patch.object(oracle.shutil, "which", side_effect=lambda n: "/usr/sbin/ufw" if n == "ufw" else None), \
             mock.patch.object(oracle, "read_text", return_value="ENABLED=yes\n"):
            oracle.check_linux_firewall(rep, ports)
        self.assertEqual(len(rep.fix_plan), 6)              # 2 chunks x 3 private ranges
        self.assertTrue(all(" from 10.0.0.0/8 " in p or " from 172.16.0.0/12 " in p or " from 192.168.0.0/16 " in p
                            for p in rep.fix_plan))


IPCONFIG_TWO_ADAPTERS = """
Windows IP Configuration

Ethernet adapter Ethernet 2:

   Connection-specific DNS Suffix  . :
   IPv4 Address. . . . . . . . . . . : 192.168.1.140
   Default Gateway . . . . . . . . . : 192.168.1.1

Ethernet adapter Ethernet:

   IPv4 Address. . . . . . . . . . . : 192.168.1.117
   Default Gateway . . . . . . . . . : 192.168.1.1

Ethernet adapter vEthernet (Default Switch):

   IPv4 Address. . . . . . . . . . . : 172.30.176.1
"""

WIN_BOTH_PUBLIC = """PROFILE|Ethernet 2|Public
PROFILE|Ethernet|Public
FW|Domain|True
FW|Private|True
FW|Public|True
"""


class TwoAdaptersTest(TempDirCase):
    """A Windows PC on the same network through two adapters, both Public."""

    def setUp(self):
        super().setUp()
        oracle.forget_addresses()
        self.addCleanup(oracle.forget_addresses)
        for p in (mock.patch.object(oracle, "IS_WIN", True),
                  mock.patch.object(oracle, "route_ip", return_value="192.168.1.140"),
                  mock.patch.object(oracle, "firewall_programs", return_value=[("Python", "C:\\py\\python.exe")])):
            p.start()
            self.addCleanup(p.stop)

    def addresses(self):
        with mock.patch.object(oracle, "run_quiet", return_value=IPCONFIG_TWO_ADAPTERS):
            return oracle.local_addresses(max_age=0)

    def test_the_chosen_address_comes_first(self):
        self.assertEqual(self.addresses()[0][1], "192.168.1.140")      # default route
        cfg = oracle.load_config()
        cfg["address"] = "192.168.1.117"
        oracle.save_config(cfg)
        addrs = self.addresses()
        self.assertEqual(addrs[0][:2], ("Ethernet adapter Ethernet", "192.168.1.117"))
        self.assertEqual(addrs[-1][2], "virtual")

    def test_only_the_network_in_use_is_flagged(self):
        rep = oracle.Report()
        oracle.check_windows_firewall(rep, [8800, 8010], text=WIN_BOTH_PUBLIC, adapter="Ethernet adapter Ethernet")
        self.assertEqual(rep.public, ["Ethernet"])
        self.assertIn(("", "Network", "'Ethernet 2' is Public too, but other devices don't use it, so it doesn't matter"),
                      rep.rows)
        self.assertIsNotNone(rep.fix)

    def test_a_private_only_rule_does_not_count_on_a_public_network(self):
        text = WIN_BOTH_PUBLIC + "ORACLE|True|Private, Domain|8800,8010\n"
        rep = oracle.Report()
        oracle.check_windows_firewall(rep, [8800, 8010], text=text, adapter="Ethernet adapter Ethernet")
        self.assertIn(("!!", "", "nothing lets Oracle's ports in on this network yet"), rep.rows)
        self.assertIsNotNone(rep.fix)

    def test_after_the_fix_everything_is_fine(self):
        text = ("PROFILE|Ethernet 2|Public\nPROFILE|Ethernet|Private\nFW|Private|True\nFW|Public|True\n"
                "ORACLE|True|Private, Domain|8800,8010\n")
        rep = oracle.Report()
        oracle.check_windows_firewall(rep, [8800, 8010], text=text, adapter="Ethernet adapter Ethernet")
        self.assertEqual((rep.todo, rep.fix, rep.public), ([], None, []))

    def test_fix_script_marks_the_network_private_and_adds_the_rule(self):
        with mock.patch.object(oracle.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(oracle.fix_windows_firewall([8800, 8010], ["C:\\py\\python.exe"], ["Ethernet"]))
        script = (oracle.LOG_DIR / "fix-firewall.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("Set-NetConnectionProfile -InterfaceAlias 'Ethernet' -NetworkCategory Private", script)
        self.assertIn("-LocalPort 8800,8010 -Profile Private,Domain", script)
        self.assertIn("-Verb RunAs", run.call_args[0][0][-1])

    def test_doctor_suggests_choosing_the_address(self):
        live = {"app": "oracle", "lan": True, "atlases": []}

        def quiet(cmd, **kw):
            return IPCONFIG_TWO_ADAPTERS if cmd == ["ipconfig"] else WIN_BOTH_PUBLIC
        out = io.StringIO()
        with mock.patch.object(oracle, "call_oracle", return_value=live), \
             mock.patch.object(oracle, "run_quiet", side_effect=quiet), \
             mock.patch.object(oracle.shutil, "which", return_value="powershell"), \
             mock.patch.object(oracle, "port_open", return_value=True), \
             mock.patch.object(oracle, "is_wsl", return_value=False), \
             mock.patch.object(oracle.sys, "platform", "win32"), \
             contextlib.redirect_stdout(out):
            oracle.cmd_doctor(oracle.build_parser().parse_args(["doctor"]))
        text = out.getvalue()
        self.assertIn("python3 oracle.py config --address 192.168.1.117", text)
        self.assertIn("'Ethernet 2' is set to Public", text)      # .140 is still the one in use here
        self.assertNotIn("'Ethernet' is set to Public", text)

    def test_config_address_validation(self):
        def config(*argv):
            with contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch.object(oracle, "run_quiet", return_value=IPCONFIG_TWO_ADAPTERS):
                return oracle.cmd_config(oracle.build_parser().parse_args(["config", *argv]))
        self.assertEqual(config("--address", "8.8.8.8"), 1)
        self.assertEqual(config("--address", "192.168.1.117"), 0)
        self.assertEqual(oracle.load_config()["address"], "192.168.1.117")
        self.assertEqual(config("--address", ""), 0)
        self.assertIsNone(oracle.load_config()["address"])


class DoctorTest(TempDirCase):
    def test_lan_turned_on_after_start_says_restart(self):
        cfg = oracle.load_config()
        cfg["lan"] = True
        oracle.save_config(cfg)
        live = {"app": "oracle", "lan": False, "atlases": []}
        out = io.StringIO()
        with mock.patch.object(oracle, "call_oracle", return_value=live), \
             mock.patch.object(oracle, "local_addresses", return_value=[("en0", "192.168.1.20", "")]), \
             mock.patch.object(oracle, "is_wsl", return_value=False), \
             mock.patch.object(oracle.sys, "platform", "linux"), \
             mock.patch.object(oracle, "IS_WIN", False), \
             mock.patch.object(oracle, "check_linux_firewall"), \
             contextlib.redirect_stdout(out):
            code = oracle.cmd_doctor(oracle.build_parser().parse_args(["doctor"]))
        self.assertEqual(code, 1)
        self.assertIn("LAN mode was turned on after Oracle started", out.getvalue())
        self.assertIn("python3 oracle.py restart", out.getvalue())
        self.assertIn("http://192.168.1.20:8800", out.getvalue())

    def test_start_warns_when_the_running_oracle_has_the_other_mode(self):
        cfg = oracle.load_config()
        cfg["lan"] = True
        oracle.save_config(cfg)
        out = io.StringIO()
        with mock.patch.object(oracle, "call_oracle", return_value={"app": "oracle", "lan": False}), \
             contextlib.redirect_stdout(out):
            oracle.cmd_start(oracle.build_parser().parse_args(["start", "--no-browser"]))
        self.assertIn("python3 oracle.py restart", out.getvalue())


class PlainHTTPTest(TempDirCase):
    def test_tls_handshake_is_closed_at_once(self):
        import threading
        port = free_port()
        write(self.db / "site" / "index.html", "hi")
        handler = oracle.functools.partial(oracle.StaticHandler, directory=str(self.db / "site"))
        httpd = oracle.Server(("127.0.0.1", port), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            started = time.time()
            s.sendall(bytes.fromhex("16030100a5010000a10303") + b"\0" * 32)
            self.assertEqual(s.recv(10), b"")                 # hung up, didn't wait
            self.assertLess(time.time() - started, 2)
        with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            self.assertEqual(resp.read(), b"hi")


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
