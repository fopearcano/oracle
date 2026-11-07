#!/usr/bin/env python3
"""Oracle: start every web atlas kept in one folder, with one command.

Put the atlases (clones of lit-atlas, cine-atlas, story, ...) in one folder,
choose which ones you want, and Oracle starts each of them on its own port.
It also serves a dashboard at http://localhost:8800 to open, stop and restart
them, and `oracle.py autostart on` makes it run every time you log in.

    python3 oracle.py                  start the selected atlases + dashboard
    python3 oracle.py list             what is in the folder, and what runs
    python3 oracle.py select NAME...   start these automatically
    python3 oracle.py deselect NAME... stop starting these automatically
    python3 oracle.py sync [NAME...]   download or update atlases from GitHub
    python3 oracle.py config --db DIR  remember which folder holds the atlases
    python3 oracle.py autostart on     run Oracle when you log in
    python3 oracle.py stop             stop a running Oracle and its atlases

Only the Python standard library is used (Python 3.8 or newer). Atlases with
a server of their own need Node.js; `sync` needs git.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
REGISTRY_FILE = HERE / "atlases.json"
CONFIG_FILE = HERE / "oracle.config.json"
DASHBOARD_FILE = HERE / "dashboard.html"
LOG_DIR = HERE / "logs"
DEFAULT_DB = HERE / "db"
DEFAULT_PORT = 8800
FIRST_FREE_PORT = 8100          # where ports for atlases Oracle doesn't know start
LOG_MAX_BYTES = 1_000_000       # a log bigger than this is cleared on the next start

IS_WIN = os.name == "nt"
NO_WINDOW = {"creationflags": 0x08000000} if IS_WIN else {}   # CREATE_NO_WINDOW
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Requests to Oracle itself must never go through an HTTP proxy.
_local = build_opener(ProxyHandler({}))

DEFAULT_CONFIG = {"db": None, "port": DEFAULT_PORT, "lan": False, "skip": []}


# --------------------------------------------------------------------- utils

def say(msg: str) -> None:
    print(f"[oracle] {msg}", flush=True)


def read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_text(path: Path, limit: int = 200_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def port_open(port, host: str = "127.0.0.1") -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=0.4):
            return True
    except OSError:
        return False


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))   # no packet is sent; this only picks a route
            return s.getsockname()[0]
    except OSError:
        return None


def spawn_flags() -> dict:
    """Run children in their own process group so their whole tree can be stopped."""
    if IS_WIN:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def open_log(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        big = path.stat().st_size > LOG_MAX_BYTES
    except OSError:
        big = False
    f = open(path, "wb" if big else "ab")
    f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
    f.flush()
    return f


def tail(path: Path, lines: int = 200) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            data = f.read()
    except OSError:
        return ""
    text = ANSI.sub("", data.decode("utf-8", "replace")).replace("\r\n", "\n")
    return "\n".join(text.split("\n")[-lines:]).strip("\n")


# -------------------------------------------------------------------- config

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    data = read_json(CONFIG_FILE)
    if isinstance(data, dict):
        cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    cfg["skip"] = sorted({s for s in (cfg.get("skip") or []) if isinstance(s, str)})
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def resolve_db(arg, cfg: dict) -> Path:
    raw = arg or os.environ.get("ORACLE_DB") or cfg.get("db")
    if not raw:
        return DEFAULT_DB
    return Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()


def load_registry() -> dict:
    data = read_json(REGISTRY_FILE) or {}
    owner = data.get("github_owner") or ""
    registry = {}
    for item in data.get("atlases") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        spec = dict(item)
        if not spec.get("repo") and owner:
            spec["repo"] = f"{owner}/{spec['name']}"
        registry[spec["name"]] = spec
    return registry


# ----------------------------------------------------------------- detection

PORT_IN_SCRIPT = re.compile(r"(?:http\.server|serve\.py)\s+(\d{2,5})\b")
PORT_IN_SOURCE = re.compile(r"\bPORT\b\s*\)?\s*(?:\|\||\?\?)\s*['\"]?(\d{2,5})\b")
PORT_IN_README = re.compile(r"(?:localhost|127\.0\.0\.1):(\d{2,5})\b|http\.server\s+(\d{2,5})\b")
NODE_ENTRY = re.compile(r"^\s*node\s+(?:--?[\w-]+(?:=\S+)?\s+)*([\w./\\-]+\.[cm]?js)\b")


def readme_of(folder: Path) -> str:
    for name in ("README.md", "readme.md", "README.markdown", "README.txt", "README"):
        text = read_text(folder / name, 100_000)
        if text:
            return text
    return ""


def port_in_readme(folder: Path):
    m = PORT_IN_README.search(readme_of(folder))
    return int(m.group(1) or m.group(2)) if m else None


def port_in_file(path: Path):
    m = PORT_IN_SOURCE.search(read_text(path))
    return int(m.group(1)) if m else None


def detect(folder: Path):
    """Work out how to start an atlas that atlases.json doesn't list.

    Returns a spec such as {"kind": "static", "port": 8010} or None when the
    folder does not look like a web app.
    """
    pkg = read_json(folder / "package.json")
    pkg = pkg if isinstance(pkg, dict) else {}
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    start = str(scripts.get("start") or "")
    spec = None

    m = PORT_IN_SCRIPT.search(f"{start} {scripts.get('serve') or ''}")
    if m and (folder / "index.html").is_file():
        spec = {"kind": "static", "port": int(m.group(1))}
    elif NODE_ENTRY.match(start) and (folder / NODE_ENTRY.match(start).group(1)).is_file():
        entry = NODE_ENTRY.match(start).group(1)
        spec = {"kind": "node", "entry": entry,
                "port": port_in_file(folder / entry) or port_in_readme(folder)}
    elif start:
        spec = {"kind": "npm", "port": port_in_readme(folder)}
    elif (folder / "index.html").is_file():
        spec = {"kind": "static", "port": port_in_readme(folder)}
    elif (folder / "public" / "index.html").is_file():
        spec = {"kind": "static", "root": "public", "port": port_in_readme(folder)}
    if spec is None:
        return None

    heading = re.search(r"^#\s+(.+)$", readme_of(folder), re.M)
    if heading:
        spec["title"] = re.sub(r"[*_`]", "", heading.group(1)).strip()
    if isinstance(pkg.get("description"), str):
        spec["about"] = pkg["description"]
    return spec


def git_remote_repo(folder: Path) -> str:
    """Name of the GitHub repository a clone came from ("lit-atlas"), or ""."""
    text = read_text(folder / ".git" / "config", 50_000)
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text) or re.search(r"url\s*=\s*(\S+)", text)
    if not m:
        return ""
    url = m.group(1).rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return re.split(r"[/:]", url)[-1].lower()


class Atlas:
    def __init__(self, name: str, spec: dict, path, known: bool):
        self.name = name
        self.path = path                   # folder in the db, None when absent
        self.known = known                 # listed in atlases.json
        self.title = spec.get("title") or name
        self.group = spec.get("group") or ("Other" if not known else "Atlases")
        self.about = spec.get("about") or ""
        self.kind = spec.get("kind")
        self.port = spec.get("port")
        self.root = spec.get("root") or "."
        self.entry = spec.get("entry")
        self.cmd = spec.get("cmd")
        self.repo = spec.get("repo")
        self.branch = spec.get("branch")
        self.auto_port = False
        self.problem = None

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}/"


def find_problem(a: Atlas):
    if a.path is None:
        return "not in the folder"
    if a.kind == "static":
        if not (a.path / a.root / "index.html").is_file():
            return f"no index.html in {a.path / a.root}"
    elif a.kind == "node":
        if not a.entry or not (a.path / a.entry).is_file():
            msg = f"{a.entry or 'the server script'} is missing"
            if a.branch:
                msg += (f": this app lives on the '{a.branch}' branch."
                        f" Run `python3 oracle.py sync {a.name}` to switch to it")
            return msg
    elif a.kind == "npm":
        if not (a.path / "package.json").is_file():
            return "package.json is missing"
    elif a.kind == "cmd":
        if not isinstance(a.cmd, list) or not a.cmd:
            return "\"cmd\" must be a list such as [\"python3\", \"serve.py\", \"{port}\"]"
    else:
        return "Oracle doesn't know how to start it (no \"kind\" in atlases.json)"
    return None


def assign_ports(atlases: dict, dashboard_port: int, registry_ports: set) -> None:
    """Keep every atlas's own port, and give the others (or clashes) free ones."""
    taken = {dashboard_port}
    pending = []
    for a in atlases.values():
        if a.path is None:
            continue
        blocked = taken if a.known else taken | registry_ports
        if isinstance(a.port, int) and 0 < a.port < 65536 and a.port not in blocked:
            taken.add(a.port)
        else:
            pending.append(a)
    port = FIRST_FREE_PORT
    for a in pending:
        while port in taken or port in registry_ports:
            port += 1
        a.port, a.auto_port = port, True
        taken.add(port)


def scan(db: Path, registry: dict, dashboard_port: int = DEFAULT_PORT) -> dict:
    """Every atlas in the db folder, plus the listed ones that are not there yet."""
    by_repo = {(spec.get("repo") or "").rsplit("/", 1)[-1].lower(): name
               for name, spec in registry.items() if spec.get("repo")}
    try:
        folders = sorted((p for p in db.iterdir()
                          if p.is_dir() and not p.name.startswith((".", "_")) and p.name != "node_modules"),
                         key=lambda p: p.name.lower())
    except OSError:
        folders = []

    found = {}
    for folder in folders:
        name = folder.name if folder.name in registry else by_repo.get(git_remote_repo(folder))
        if name and name not in found:
            spec = detect(folder) or {}
            spec.update(registry[name])
            found[name] = Atlas(name, spec, folder, known=True)
        elif folder.name not in registry and folder.name not in found:
            spec = detect(folder)
            if spec:
                found[folder.name] = Atlas(folder.name, spec, folder, known=False)
    for name, spec in registry.items():
        if name not in found:
            found[name] = Atlas(name, dict(spec), None, known=True)

    order = {name: i for i, name in enumerate(registry)}
    atlases = dict(sorted(found.items(),
                          key=lambda kv: (kv[1].path is None, order.get(kv[0], len(order)), kv[0].lower())))
    registry_ports = {s["port"] for s in registry.values() if isinstance(s.get("port"), int)}
    assign_ports(atlases, dashboard_port, registry_ports)
    for a in atlases.values():
        a.problem = find_problem(a)
    return atlases


# ------------------------------------------------------------ node + npm

_node_versions: dict = {}


def node_version(node: str):
    if node not in _node_versions:
        try:
            out = subprocess.run([node, "--version"], capture_output=True, text=True,
                                 timeout=15, **NO_WINDOW).stdout
            _node_versions[node] = tuple(int(x) for x in re.findall(r"\d+", out)[:3]) or None
        except (OSError, subprocess.SubprocessError):
            _node_versions[node] = None
    return _node_versions[node]


def engine_problem(folder: Path, node: str):
    pkg = read_json(folder / "package.json") or {}
    engines = pkg.get("engines") if isinstance(pkg.get("engines"), dict) else {}
    spec = str(engines.get("node") or "")
    m = re.match(r"\s*>=?\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", spec)
    have = node_version(node)
    if not m or not have:
        return None
    need = tuple(int(x or 0) for x in m.groups())
    if tuple(have) + (0,) * (3 - len(have)) < need:
        return f"needs Node.js {spec.strip()}, but this computer has {'.'.join(map(str, have))}"
    return None


def needs_install(folder: Path) -> bool:
    """True when package.json lists dependencies that node_modules lacks or is older than."""
    pkg = read_json(folder / "package.json") or {}
    if not pkg.get("dependencies"):
        return False
    marker = folder / "node_modules" / ".package-lock.json"   # written by every npm install
    if not marker.is_file():
        return True
    newest = 0.0
    for name in ("package.json", "package-lock.json"):
        try:
            newest = max(newest, (folder / name).stat().st_mtime)
        except OSError:
            pass
    return newest > marker.stat().st_mtime


class LaunchError(Exception):
    pass


def need_program(name: str, hint: str) -> str:
    path = shutil.which(name)
    if not path:
        raise LaunchError(f"{name} was not found: {hint}")
    return path


# ------------------------------------------------------------------- runner

ACTIVE = ("installing", "starting", "running", "syncing")


class Runner:
    """One atlas process: starts it, watches it, stops it."""

    def __init__(self, atlas: Atlas, host: str, on_change=None):
        self.atlas = atlas
        self.host = host
        self.on_change = on_change
        self.lock = threading.Lock()
        self.proc = None
        self.stopping = False
        self.started_at = None
        self.gen = 0                      # bumped on every start; older runs go quiet
        self.state, self.message = "stopped", ""
        self.reset()

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.atlas.name}.log"

    @property
    def active(self) -> bool:
        return self.state in ACTIVE or self.proc is not None

    def reset(self) -> None:
        """Show an idle atlas as stopped, missing or broken."""
        a = self.atlas
        if a.path is None:
            self.state, self.message = "missing", ""
        elif a.problem:
            self.state, self.message = "error", a.problem
        else:
            self.state, self.message = "stopped", ""

    def set(self, state: str, message: str = "", gen=None) -> None:
        with self.lock:
            if gen is not None and gen != self.gen:
                return
            changed = (state, message) != (self.state, self.message)
            self.state, self.message = state, message
        if changed and self.on_change:
            self.on_change(self)

    def start(self) -> None:
        with self.lock:
            if self.state in ACTIVE or self.proc is not None:
                return
            self.stopping = False
            self.gen += 1
            gen = self.gen
            self.state, self.message = "starting", ""
        threading.Thread(target=self._run, args=(gen,), name=f"run-{self.atlas.name}", daemon=True).start()

    def command(self):
        a = self.atlas
        env = dict(os.environ, PORT=str(a.port), HOST=self.host)
        if a.kind == "static":
            root = str((a.path / a.root).resolve())
            return [sys.executable, str(HERE / "oracle.py"), "serve-static", root,
                    "--port", str(a.port), "--host", self.host], env
        if a.kind == "node":
            node = need_program("node", "install Node.js from https://nodejs.org")
            problem = engine_problem(a.path, node)
            if problem:
                raise LaunchError(problem)
            return [node, a.entry], env
        if a.kind == "npm":
            return [need_program("npm", "install Node.js from https://nodejs.org"), "start"], env
        if a.kind == "cmd":
            values = {"port": a.port, "host": self.host, "python": sys.executable, "dir": str(a.path)}
            cmd = [str(part).format(**values) for part in a.cmd]
            if not os.path.isabs(cmd[0]):
                cmd[0] = need_program(cmd[0], "it is the first word of this atlas's \"cmd\"")
            return cmd, env
        raise LaunchError(a.problem or "unknown kind")

    def _spawn(self, cmd, env, log):
        with self.lock:
            if self.stopping:
                return None
            self.proc = subprocess.Popen(cmd, cwd=str(self.atlas.path), env=env, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT, **spawn_flags())
            return self.proc

    def _run(self, gen: int) -> None:
        a = self.atlas
        set_ = functools.partial(self.set, gen=gen)
        if a.problem:
            return set_("error", a.problem)
        if port_open(a.port):
            return set_("external", f"something was already answering on port {a.port}, "
                                        "so Oracle left it alone")
        try:
            cmd, env = self.command()
        except LaunchError as e:
            return set_("error", str(e))

        log = open_log(self.log_path)
        try:
            if a.kind in ("node", "npm") and needs_install(a.path):
                npm = shutil.which("npm")
                if not npm:
                    return set_("error", "npm was not found: install Node.js from https://nodejs.org")
                set_("installing", "installing dependencies (npm install), only needed once")
                log.write(b"$ npm install\n")
                log.flush()
                proc = self._spawn([npm, "install", "--no-audit", "--no-fund"], env, log)
                code = proc.wait() if proc else None
                with self.lock:
                    self.proc = None
                if proc is None or self.stopping:
                    return set_("stopped")
                if code != 0:
                    return set_("error", "npm install failed: open the log to see why")
            log.write(("$ " + " ".join(cmd) + "\n").encode())
            log.flush()
            proc = self._spawn(cmd, env, log)
        except OSError as e:
            with self.lock:
                self.proc = None
            return set_("error", f"could not start: {e}")
        finally:
            log.close()
        if proc is None:
            return set_("stopped")

        self.started_at = time.time()
        set_("starting", "")
        slow_at = time.time() + 30
        while proc.poll() is None and not port_open(a.port):
            if time.time() > slow_at:
                set_("starting", f"still waiting for it to answer on port {a.port}")
            time.sleep(0.25)
        if proc.poll() is None:
            set_("running", "")
        code = proc.wait()
        with self.lock:
            self.proc = None
            stopping = self.stopping
        if stopping:
            set_("stopped")
        else:
            set_("crashed", f"it quit (exit code {code}): open the log to see why")

    def stop(self) -> None:
        with self.lock:
            self.stopping = True
            proc = self.proc
            if proc is None and self.state in ("starting", "crashed"):
                self.gen += 1                 # a run that hasn't spawned yet gives up
                self.state, self.message = "stopped", ""
        if proc is not None:
            kill_tree(proc)
            for _ in range(100):           # let _run() record the exit
                if self.proc is None:
                    break
                time.sleep(0.05)

    def info(self, selected: bool) -> dict:
        a = self.atlas
        state, message = self.state, self.message
        if state == "external" and not port_open(a.port):
            state, message = "stopped", ""
        uptime = int(time.time() - self.started_at) if state == "running" and self.started_at else None
        return {
            "name": a.name, "title": a.title, "group": a.group, "about": a.about,
            "kind": a.kind, "port": a.port, "url": a.url, "auto_port": a.auto_port,
            "present": a.path is not None, "path": str(a.path) if a.path else None,
            "known": a.known, "repo": a.repo, "branch": a.branch,
            "selected": selected, "state": state, "message": message, "uptime": uptime,
            "has_log": self.log_path.is_file(),
        }


# --------------------------------------------------------------- git sync

def git(args, cwd=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=600, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def remote_has_branch(remote: str, branch: str, cwd=None) -> bool:
    code, out = git(["ls-remote", "--heads", remote, branch], cwd=cwd)
    return code == 0 and bool(out.strip())


def git_sync(a: Atlas, db: Path, out=say):
    """Clone a missing atlas into the db folder, or fast-forward an existing clone."""
    if not shutil.which("git"):
        return False, "git is not installed"
    if not a.repo:
        return False, "atlases.json gives no GitHub repository for it"
    url = f"https://github.com/{a.repo}.git"

    if a.path is None:
        dest = db / a.name
        if dest.exists():
            return False, f"{dest} already exists but is not an atlas Oracle recognises"
        db.mkdir(parents=True, exist_ok=True)
        args = ["clone"]
        if a.branch and remote_has_branch(url, a.branch):
            args += ["--branch", a.branch]
        out(f"{a.name}: cloning {a.repo} into {dest}")
        code, text = git(args + [url, str(dest)])
        return (True, "downloaded") if code == 0 else (False, text.splitlines()[-1] if text else "git clone failed")

    dest = a.path
    if not (dest / ".git").exists():
        return False, f"{dest} is not a git clone, so it was left as it is"
    # The writing tools keep their app on a side branch; switch a clone that has
    # only the default branch checked out, as long as nothing local would be lost.
    if a.problem and a.branch and remote_has_branch("origin", a.branch, cwd=dest):
        code, dirty = git(["status", "--porcelain", "--untracked-files=no"], cwd=dest)
        if code == 0 and not dirty:
            out(f"{a.name}: switching to branch {a.branch}")
            git(["fetch", "origin", f"+refs/heads/{a.branch}:refs/remotes/origin/{a.branch}"], cwd=dest)
            code, text = git(["checkout", a.branch], cwd=dest)
            if code != 0:
                return False, text.splitlines()[-1] if text else "git checkout failed"
    out(f"{a.name}: pulling")
    code, text = git(["pull", "--ff-only"], cwd=dest)
    if code != 0:
        return False, text.splitlines()[-1] if text else "git pull failed"
    return True, text.splitlines()[-1] if text else "up to date"


# --------------------------------------------------------------- supervisor

class Supervisor:
    def __init__(self, db: Path, registry: dict, cfg: dict, host: str = "127.0.0.1",
                 dashboard_port: int = DEFAULT_PORT, quiet: bool = False):
        self.db, self.registry, self.cfg = db, registry, cfg
        self.host, self.dashboard_port, self.quiet = host, dashboard_port, quiet
        self.lock = threading.RLock()
        self.runners: dict = {}
        self.rescan()

    def report(self, r: Runner) -> None:
        if self.quiet:
            return
        line = f"{r.atlas.name:<15} {r.state:<10}"
        if r.state == "running":
            line += f" {r.atlas.url}"
        elif r.message:
            line += f" {r.message}"
        say(line)

    def rescan(self) -> None:
        atlases = scan(self.db, self.registry, self.dashboard_port)
        with self.lock:
            for name, a in atlases.items():
                r = self.runners.get(name)
                if r is None:
                    self.runners[name] = Runner(a, self.host, self.report)
                elif not r.active:
                    r.atlas = a
                    r.reset()
            gone = [n for n, r in self.runners.items() if n not in atlases and not r.active]
            for name in gone:
                del self.runners[name]
            order = list(atlases) + [n for n in self.runners if n not in atlases]
            self.runners = {n: self.runners[n] for n in order}

    def runner(self, name: str):
        with self.lock:
            return self.runners.get(name)

    def is_selected(self, name: str) -> bool:
        return name not in self.cfg.get("skip", [])

    def set_selected(self, name: str, selected: bool) -> None:
        with self.lock:
            skip = set(self.cfg.get("skip", []))
            (skip.discard if selected else skip.add)(name)
            self.cfg["skip"] = sorted(skip)
            save_config(self.cfg)

    def start(self, names=None) -> None:
        with self.lock:
            runners = list(self.runners.values())
        for r in runners:
            wanted = (r.atlas.name in names) if names is not None else self.is_selected(r.atlas.name)
            if wanted and r.atlas.path is not None:
                r.start()

    def stop_all(self) -> None:
        with self.lock:
            runners = [r for r in self.runners.values() if r.proc is not None or r.state == "starting"]
        threads = [threading.Thread(target=r.stop, daemon=True) for r in runners]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

    def restart(self, name: str) -> None:
        r = self.runner(name)
        if r and r.state != "syncing":
            r.stop()
            r.start()

    def download(self, name: str) -> bool:
        r = self.runner(name)
        if r is None or r.active:
            return False
        r.set("syncing", "downloading from GitHub")

        def work():
            log = open_log(r.log_path)

            def out(msg):
                log.write((msg + "\n").encode())
                log.flush()
            try:
                ok, msg = git_sync(r.atlas, self.db, out)
                out(msg)
            finally:
                log.close()
            with r.lock:
                r.state = "stopped"           # idle again, so rescan() refreshes it
            self.rescan()
            if not ok:
                r.set("error", f"download failed: {msg}")
            elif self.is_selected(name) and r.atlas.path is not None and not r.atlas.problem:
                r.start()
        threading.Thread(target=work, name=f"sync-{name}", daemon=True).start()
        return True

    def status(self) -> dict:
        with self.lock:
            runners = list(self.runners.values())
        lan = self.host not in LOCAL_HOSTS
        return {
            "app": "oracle", "version": VERSION, "pid": os.getpid(),
            "db": str(self.db), "db_exists": self.db.is_dir(),
            "lan": lan, "lan_ip": lan_ip() if lan else None,
            "atlases": [r.info(self.is_selected(r.atlas.name)) for r in runners],
        }


# ------------------------------------------------------------------ servers

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not IS_WIN    # on Windows it would let two servers share a port


class StaticHandler(SimpleHTTPRequestHandler):
    """Static files that the browser re-checks on every load (no stale scripts)."""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        # Windows can map these to text/plain in the registry, which breaks modules.
        ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
        ".json": "application/json", ".geojson": "application/geo+json",
        ".topojson": "application/json", ".svg": "image/svg+xml",
        ".wasm": "application/wasm", ".webmanifest": "application/manifest+json",
    }

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_request(self, code="-", size="-"):
        if str(code)[:1] in ("4", "5"):
            super().log_request(code, size)


def serve_static(root: str, port: int, host: str) -> None:
    handler = functools.partial(StaticHandler, directory=root)
    with Server((host, port), handler) as httpd:
        print(f"Serving {root} at http://localhost:{port}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = f"Oracle/{VERSION}"

    def log_message(self, *args):
        pass

    @property
    def sup(self) -> Supervisor:
        return self.server.supervisor

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host[1:host.find("]")] if "]" in host else host
        else:
            host = host.rsplit(":", 1)[0]
        return host in LOCAL_HOSTS

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "forbidden host"}, 403)
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                body = DASHBOARD_FILE.read_bytes()
            except OSError:
                return self._json({"error": "dashboard.html is missing"}, 500)
            return self._send(200, body, "text/html; charset=utf-8")
        if path == "/api/status":
            return self._json(self.sup.status())
        if path.startswith("/api/logs/"):
            r = self.sup.runner(unquote(path[len("/api/logs/"):]))
            if r is None:
                return self._json({"error": "no such atlas"}, 404)
            return self._send(200, tail(r.log_path).encode(), "text/plain; charset=utf-8")
        return self._json({"error": "not found"}, 404)

    do_HEAD = do_GET

    def do_POST(self):
        # The custom header can't be sent cross-site without a CORS preflight,
        # which Oracle never answers, so other web pages cannot drive it.
        if not self._host_ok() or self.headers.get("X-Oracle") != "1":
            return self._json({"error": "forbidden"}, 403)
        length = min(int(self.headers.get("Content-Length") or 0), 10_000)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            body = {}
        parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/")]
        sup = self.sup

        if parts == ["api", "start-selected"]:
            sup.start()
        elif parts == ["api", "stop-all"]:
            threading.Thread(target=sup.stop_all, daemon=True).start()
        elif parts == ["api", "rescan"]:
            sup.rescan()
        elif parts == ["api", "shutdown"]:
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        elif len(parts) == 4 and parts[:2] == ["api", "atlas"]:
            name, action = parts[2], parts[3]
            r = sup.runner(name)
            if r is None:
                return self._json({"error": "no such atlas"}, 404)
            if action == "start":
                r.start()
            elif action == "stop":
                threading.Thread(target=r.stop, daemon=True).start()
            elif action == "restart":
                threading.Thread(target=sup.restart, args=(name,), daemon=True).start()
            elif action == "select":
                sup.set_selected(name, bool(body.get("selected")))
            elif action == "download":
                if not sup.download(name):
                    return self._json({"error": "it is busy"}, 409)
            else:
                return self._json({"error": "unknown action"}, 404)
        else:
            return self._json({"error": "not found"}, 404)
        time.sleep(0.15)    # let a state change land before answering
        self._json(sup.status())


def make_dashboard(sup: Supervisor, port: int) -> Server:
    httpd = Server(("127.0.0.1", port), DashboardHandler)
    httpd.supervisor = sup
    return httpd


def call_oracle(port: int, path: str, post: bool = False):
    """Talk to an Oracle that is already running; None when there is none."""
    req = Request(f"http://127.0.0.1:{port}{path}", data=b"{}" if post else None,
                  headers={"X-Oracle": "1", "Content-Type": "application/json"},
                  method="POST" if post else "GET")
    try:
        with _local.open(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and (data.get("app") == "oracle" or data.get("ok")) else None


# ---------------------------------------------------------------- autostart

def autostart_file() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / "local.oracle.atlases.plist"
    if IS_WIN:
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Oracle atlases.vbs"
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "autostart" / "oracle-atlases.desktop"


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _desktop_quote(s: str) -> str:
    return '"' + re.sub(r'(["`$\\])', r"\\\1", s) + '"'


def autostart_content() -> str:
    script = str(HERE / "oracle.py")
    if sys.platform == "darwin":
        args = "".join(f"<string>{_xml(x)}</string>" for x in (sys.executable, script, "start", "--no-browser"))
        log = _xml(str(LOG_DIR / "oracle.log"))
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.oracle.atlases</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>WorkingDirectory</key><string>{_xml(str(HERE))}</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{_xml(os.environ.get("PATH", ""))}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    if IS_WIN:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw if pyw.exists() else Path(sys.executable))
        return ('Set sh = CreateObject("WScript.Shell")\r\n'
                f'sh.CurrentDirectory = "{HERE}"\r\n'
                f'sh.Run """{exe}"" ""{script}"" start --no-browser", 0, False\r\n')
    return ("[Desktop Entry]\nType=Application\nName=Oracle atlases\n"
            "Comment=Start the web atlases in the Oracle folder\n"
            f"Exec={_desktop_quote(sys.executable)} {_desktop_quote(script)} start --no-browser\n"
            "Terminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n")


# ---------------------------------------------------------------- commands

def cmd_start(args) -> int:
    cfg = load_config()
    db = resolve_db(args.db, cfg)
    port = args.port or cfg.get("port") or DEFAULT_PORT
    url = f"http://localhost:{port}"

    if call_oracle(port, "/api/status"):
        say(f"Oracle is already running: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    host = "0.0.0.0" if (args.lan or cfg.get("lan")) else "127.0.0.1"
    sup = Supervisor(db, load_registry(), cfg, host=host, dashboard_port=port)
    try:
        httpd = make_dashboard(sup, port)
    except OSError as e:
        say(f"Cannot open the dashboard on port {port} ({e.strerror or e}).")
        say("Pick another one with --port, or for good with: oracle.py config --port 8899")
        return 1

    present = [r.atlas for r in sup.runners.values() if r.atlas.path is not None]
    say(f"Oracle {VERSION}, atlas folder: {db}")
    if not present:
        say("There are no atlases in that folder yet. Put them there, or download them with:")
        say("    python3 oracle.py sync")
    names = None
    if args.all:
        names = [a.name for a in present]
    elif args.only:
        names = [n.strip() for n in ",".join(args.only).split(",") if n.strip()]
        unknown = [n for n in names if sup.runner(n) is None or sup.runner(n).atlas.path is None]
        for n in unknown:
            say(f"{n}: not in the folder, skipped")
    sup.start(names)
    say(f"Dashboard: {url}   (Ctrl+C stops Oracle and every atlas it started)")
    if host != "127.0.0.1":
        say(f"Atlases are also open to your network at http://{lan_ip() or 'this-computer'}:<port>")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()

    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        say("Stopping the atlases...")
        sup.stop_all()
        httpd.server_close()
        say("Bye.")
    return 0


def cmd_list(args) -> int:
    cfg = load_config()
    port = cfg.get("port") or DEFAULT_PORT
    live = call_oracle(port, "/api/status")
    if live and not args.db:
        rows, db = live["atlases"], live["db"]
        say(f"Atlas folder: {db}   (Oracle is running: http://localhost:{port})")
    else:
        db = resolve_db(args.db, cfg)
        sup = Supervisor(db, load_registry(), cfg, dashboard_port=port, quiet=True)
        rows = sup.status()["atlases"]
        for row in rows:
            if row["present"] and row["state"] == "stopped" and port_open(row["port"]):
                row["state"] = "answering"
        say(f"Atlas folder: {db}   (Oracle is not running)")
    print()
    print(f"  {'auto':<5} {'name':<15} {'port':<6} {'kind':<7} {'state':<10} notes")
    for row in rows:
        mark = "[x]" if row["selected"] else "[ ]"
        note = row["url"] if row["state"] in ("running", "external", "answering") else (row["message"] or "")
        if row["state"] == "missing":
            note = f"not in the folder: python3 oracle.py sync {row['name']}"
        print(f"  {mark:<5} {row['name']:<15} {row['port'] or '-':<6} {row['kind'] or '?':<7} {row['state']:<10} {note}")
    print()
    return 0


def _known_names(db: Path, cfg: dict) -> list:
    return list(scan(db, load_registry(), cfg.get("port") or DEFAULT_PORT))


def cmd_select(args, selected: bool) -> int:
    cfg = load_config()
    db = resolve_db(args.db, cfg)
    known = _known_names(db, cfg)
    skip = set(cfg["skip"])
    if args.all:
        skip = set() if selected else set(known)
    else:
        if not args.names:
            say("Name one or more atlases, or use --all. `python3 oracle.py list` shows them.")
            return 1
        for name in args.names:
            if name not in known:
                say(f"{name}: no such atlas (it is neither in atlases.json nor in {db})")
                return 1
            (skip.discard if selected else skip.add)(name)
    cfg["skip"] = sorted(skip)
    save_config(cfg)
    chosen = [n for n in known if n not in skip]
    say("Starts automatically: " + (", ".join(chosen) if chosen else "nothing"))
    if call_oracle(cfg.get("port") or DEFAULT_PORT, "/api/status"):
        say("Oracle is running: restart it (or use the dashboard) for this to take effect now.")
    return 0


def cmd_sync(args) -> int:
    cfg = load_config()
    db = resolve_db(args.db, cfg)
    atlases = scan(db, load_registry(), cfg.get("port") or DEFAULT_PORT)
    names = args.names or [n for n, a in atlases.items()
                           if a.repo and (not args.selected or n not in cfg["skip"])]
    failed = 0
    for name in names:
        a = atlases.get(name)
        if a is None:
            say(f"{name}: no such atlas")
            failed += 1
            continue
        ok, msg = git_sync(a, db)
        say(f"{name}: {msg}")
        failed += not ok
    if failed:
        say(f"{failed} could not be synced.")
    return 1 if failed else 0


def cmd_stop(args) -> int:
    port = args.port or load_config().get("port") or DEFAULT_PORT
    if call_oracle(port, "/api/shutdown", post=True):
        say("Oracle is stopping, along with the atlases it started.")
        return 0
    say(f"No Oracle is running on port {port}.")
    return 1


def cmd_config(args) -> int:
    cfg = load_config()
    changed = False
    if args.db is not None:
        cfg["db"] = str(Path(os.path.expanduser(args.db)).resolve()) if args.db else None
        changed = True
    if args.port is not None:
        cfg["port"] = args.port
        changed = True
    if args.lan is not None:
        cfg["lan"] = args.lan == "on"
        changed = True
    if changed:
        save_config(cfg)
    print(json.dumps({**cfg, "db (resolved)": str(resolve_db(None, cfg))}, indent=2))
    if changed and call_oracle(load_config().get("port") or DEFAULT_PORT, "/api/status"):
        say("Oracle is running: restart it for this to take effect.")
    return 0


def cmd_autostart(args) -> int:
    target = autostart_file()
    if args.action == "status":
        say(f"Autostart is {'on' if target.exists() else 'off'} ({target})")
        return 0
    if args.action == "off":
        if target.exists():
            target.unlink()
        say(f"Autostart is off (removed {target}).")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(autostart_content(), encoding="utf-8")
    cfg = load_config()
    db = resolve_db(None, cfg)
    say(f"Autostart is on: Oracle will start when you log in ({target}).")
    say(f"It will use the atlas folder {db} and the dashboard http://localhost:{cfg.get('port') or DEFAULT_PORT}")
    if not any(a.path for a in scan(db, load_registry()).values()):
        say("That folder has no atlases yet: set the right one with `python3 oracle.py config --db DIR`.")
    if sys.platform == "darwin" and any(part in ("Desktop", "Documents", "Downloads") for part in HERE.parts):
        say("Note: macOS may block programs started at login from reading Desktop, Documents and")
        say("Downloads. If nothing starts, move Oracle and the atlas folder somewhere else in your home.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oracle.py", description="Start the web atlases kept in one folder.")
    sub = p.add_subparsers(dest="command")

    def with_db(sp):
        sp.add_argument("--db", metavar="DIR", help="atlas folder for this run only (see `config --db`)")
        return sp

    s = with_db(sub.add_parser("start", help="start the selected atlases and the dashboard (default)"))
    s.add_argument("--port", type=int, help="dashboard port (default 8800)")
    s.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")
    s.add_argument("--lan", action="store_true", help="let other devices on your network open the atlases")
    s.add_argument("--all", action="store_true", help="start every atlas in the folder, selected or not")
    s.add_argument("--only", nargs="+", metavar="NAME", help="start just these atlases this time")

    with_db(sub.add_parser("list", help="show the atlases, their ports and which ones start"))
    for name, text in (("select", "start these atlases automatically"),
                       ("deselect", "stop starting these atlases automatically")):
        sp = with_db(sub.add_parser(name, help=text))
        sp.add_argument("names", nargs="*", metavar="NAME")
        sp.add_argument("--all", action="store_true", help="every atlas")

    s = with_db(sub.add_parser("sync", help="clone missing atlases from GitHub and update the others"))
    s.add_argument("names", nargs="*", metavar="NAME", help="default: all atlases in atlases.json")
    s.add_argument("--selected", action="store_true", help="only the selected ones")

    s = sub.add_parser("stop", help="stop a running Oracle and its atlases")
    s.add_argument("--port", type=int)

    s = sub.add_parser("config", help="show or change the saved settings")
    s.add_argument("--db", metavar="DIR", help="folder that holds the atlases ('' resets it)")
    s.add_argument("--port", type=int, help="dashboard port")
    s.add_argument("--lan", choices=("on", "off"), help="open the atlases to your network")

    s = sub.add_parser("autostart", help="start Oracle when you log in")
    s.add_argument("action", choices=("on", "off", "status"))

    s = sub.add_parser("serve-static", help="serve one folder without caching (used internally)")
    s.add_argument("root")
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--host", default="127.0.0.1")
    return p


def main(argv=None) -> int:
    if sys.stdout is None:                      # pythonw.exe at login: no console
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(LOG_DIR / "oracle.log", "a", encoding="utf-8", buffering=1)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["start"] + list(argv)
    args = build_parser().parse_args(argv)
    if args.command == "serve-static":
        serve_static(args.root, args.port, args.host)
        return 0
    handlers = {
        "start": cmd_start, "list": cmd_list, "sync": cmd_sync, "stop": cmd_stop,
        "config": cmd_config, "autostart": cmd_autostart,
        "select": lambda a: cmd_select(a, True), "deselect": lambda a: cmd_select(a, False),
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
