"""Local backend for the GD Claude app.

Serves the UI and a small JSON API on 127.0.0.1 (protected by a random token).
Holds the per-level Claude sessions, the queue of accepted changes, and a
watcher that applies the queue to the save file as soon as GD is closed.
"""

import base64
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

from catalog import Catalog  # noqa: E402
from deco import ChangeSet, image_to_grid, grid_to_objects, level_shapes, rgb_to_hex, shape_row  # noqa: E402
from deco_agent import DEFAULT_MODEL, DecoSession  # noqa: E402
from gdlevel import Level  # noqa: E402
from save_file import LocalLevels, default_save_path  # noqa: E402
from vault import Vault  # noqa: E402

STATIC = ROOT / "app" / "static"
STEAM_URL = "steam://rungameid/322170"
MAX_BACKUPS = 20


def config_dir():
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", Path.home())) / "GDClaude"
    return Path.home() / ".config" / "gdclaude"


def gd_running():
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq GeometryDash.exe", "/NH"],
                                 capture_output=True, text=True, timeout=5,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return "GeometryDash.exe" in out
        out = subprocess.run(["pgrep", "-f", "GeometryDash"], capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def decode_data_url(data_url):
    """'data:image/png;base64,...' -> (bytes, mime)."""
    header, _, data = data_url.partition(",")
    mime = header[5:].split(";")[0] if header.startswith("data:") else "image/png"
    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        raise ValueError("please use a PNG, JPG, GIF or WebP image")
    return base64.b64decode(data), mime


class AppState:
    def __init__(self, cfg_dir=None, client_factory=None, gd_check=gd_running):
        self.dir = Path(cfg_dir) if cfg_dir else config_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.config = self._read_json("config.json", {"auto_apply": True, "model": DEFAULT_MODEL})
        self.queue = self._read_json("queue.json", [])
        self.history = self._read_json("history.json", [])
        self.client_factory = client_factory
        self.gd_check = gd_check
        self.catalog = Catalog(cache_dir=self.dir)
        self.sessions = {}
        self.jobs = {}
        self._levels = None
        self._levels_mtime = None
        self._gd_cache = (0.0, False)
        self.last_event = ""
        self.window = None  # pywebview window, for folder dialogs

    # --- persistence ---

    def _read_json(self, name, default):
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write_json(self, name, data):
        tmp = self.dir / f"{name}.tmp"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.dir / name)

    def save_config(self):
        self._write_json("config.json", self.config)

    # --- settings ---

    def save_path(self):
        return Path(self.config.get("save_path") or default_save_path())

    def api_key(self):
        return self.config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")

    def vault(self):
        path = self.config.get("vault")
        if not path:
            return None
        try:
            return Vault(path)
        except OSError:
            return None

    def client(self):
        if self.client_factory:
            return self.client_factory()
        if not self.api_key():
            raise RuntimeError("Add your Claude API key in Settings first.")
        return anthropic.Anthropic(api_key=self.api_key())

    def settings(self):
        key = self.api_key()
        return {"api_key_set": bool(key), "api_key_hint": f"...{key[-4:]}" if key else "",
                "vault": self.config.get("vault", ""), "save_path": str(self.save_path()),
                "save_found": self.save_path().exists(), "auto_apply": self.config.get("auto_apply", True),
                "model": self.config.get("model", DEFAULT_MODEL), "catalog": self.catalog.source,
                "config_dir": str(self.dir)}

    def update_settings(self, data):
        with self.lock:
            if "api_key" in data and data["api_key"] is not None:
                self.config["api_key"] = str(data["api_key"]).strip()
            if "vault" in data:
                vault = str(data["vault"] or "").strip()
                if vault and not Path(vault).expanduser().is_dir():
                    raise ValueError(f"Vault folder not found: {vault}")
                self.config["vault"] = vault
            if "save_path" in data:
                self.config["save_path"] = str(data["save_path"] or "").strip()
                self._levels = None
            if "auto_apply" in data:
                self.config["auto_apply"] = bool(data["auto_apply"])
            if data.get("model"):
                self.config["model"] = str(data["model"]).strip()
            self.save_config()
            for session in self.sessions.values():  # new key/vault/model take effect next session
                session.cancelled = True
            self.sessions = {}
        return self.settings()

    # --- levels ---

    def levels(self, refresh=False):
        with self.lock:
            path = self.save_path()
            mtime = path.stat().st_mtime if path.exists() else None
            if self._levels is None or refresh or mtime != self._levels_mtime:
                self._levels = LocalLevels(path)
                self._levels_mtime = mtime
                for name, session in self.sessions.items():  # GD saved new edits: refresh sessions
                    try:
                        session.level = Level(self._levels.level_string(self._find(name)))
                        session.event_note = ("The level was reloaded from the game (the player edited it), "
                                              "so object refs have changed; call get_objects again.")
                    except KeyError:
                        pass
            return self._levels

    def _find(self, name):
        for lvl in self._levels.levels:
            if lvl.get("k2", "") == name:
                return lvl
        raise KeyError(f"No level called '{name}'")

    def level_list(self):
        levels = self.levels()
        return [{"name": s["name"], "objects": s["objects"]} for s in levels.summaries()]

    def session(self, name):
        with self.lock:
            if name not in self.sessions:
                self.levels()
                level = Level(self._levels.level_string(self._find(name)))
                client = self.client() if (self.api_key() or self.client_factory) else None
                self.sessions[name] = DecoSession(level, self.catalog, client=client, vault=self.vault(),
                                                  model=self.config.get("model", DEFAULT_MODEL))
            return self.sessions[name]

    def level_view(self, name):
        self.levels()
        level = Level(self._levels.level_string(self._find(name)))
        queued = [ChangeSet.from_json(q["changes"]) for q in self.queue if q["level"] == name]
        for cs in queued:  # show queued recolours as if already applied
            cs.recolour(level)
        x0, y0, x1, y1 = level.bounds()
        bg = level.color_rgb(1000) or (40, 125, 255, 1)
        ground = level.color_rgb(1001) or (0, 102, 255, 1)
        return {"name": name, "shapes": level_shapes(level, self.catalog),
                "bounds": [x0, y0, x1, y1], "bg": rgb_to_hex(bg), "ground": rgb_to_hex(ground),
                "queued_shapes": self._shapes_for(queued, level)}

    def _shapes_for(self, changesets, level):
        return [shape_row(props, level, self.catalog) for cs in changesets for props in cs.add]

    def proposal_view(self, name):
        session = self.sessions.get(name)
        if not session:
            return {"shapes": [], "removed": [], "summary": "", "adds": 0, "removes": 0, "channels": {}}
        cs = session.proposal
        removed = set(cs.remove)
        return {"shapes": self._shapes_for([cs], session.level),
                "removed": [i for i, o in enumerate(session.level.objects) if o.raw in removed],
                "summary": cs.summary, "adds": len(cs.add), "removes": len(cs.remove),
                "channels": {str(k): v for k, v in cs.channels.items()}}

    # --- Claude jobs ---

    def start_job(self, name, prompt, selection, image=None, mode=None):
        if not (self.api_key() or self.client_factory):
            raise RuntimeError("Add your Claude API key in Settings first.")
        if not prompt.strip():
            raise ValueError("Type what you want first.")
        session = self.session(name)
        if session.client is None:
            session.client = self.client()
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "level": name, "status": "running", "text": "", "events": [], "error": ""}
        self.jobs[job_id] = job

        def on_event(event):
            if event["type"] == "text":
                job["text"] += event["text"]
            else:
                job["events"].append(event)

        image_bytes, mime = decode_data_url(image) if image else (None, None)

        def run():
            session.on_event = on_event
            try:
                session.ask(prompt, selection, image_bytes, mime or "image/png", mode)
                job["status"] = "done"
            except anthropic.AuthenticationError:
                job["status"], job["error"] = "error", "Your Claude API key was rejected. Check it in Settings."
            except anthropic.APIConnectionError:
                job["status"], job["error"] = "error", "Couldn't reach Claude. Check your internet connection."
            except anthropic.RateLimitError:
                job["status"], job["error"] = "error", "Too many requests right now; wait a minute and try again."
            except anthropic.APIStatusError as e:
                job["status"], job["error"] = "error", f"Claude API error {e.status_code}: {e.message}"
            except Exception as e:  # surface anything else to the UI instead of dying silently
                traceback.print_exc()
                job["status"], job["error"] = "error", str(e) or type(e).__name__

        threading.Thread(target=run, daemon=True).start()
        return job_id

    def convert_image(self, name, selection, image, pixel_size, colours):
        """Image -> pixel art without asking Claude (free, instant)."""
        session = self.session(name)
        data, _ = decode_data_url(image)
        x0, y0, x1, y1 = selection
        pixel = max(1.0, float(pixel_size))
        cols, rows = int(abs(x1 - x0) // pixel), int(abs(y1 - y0) // pixel)
        if cols < 1 or rows < 1:
            raise ValueError("The selection is smaller than one pixel; select a bigger area.")
        if cols * rows > 256 * 256:
            raise ValueError("That's more than 65k pixels; use a bigger pixel size.")
        grid, palette = image_to_grid(data, cols, rows, int(colours))
        width, height = len(grid[0]) * pixel, len(grid) * pixel
        left = min(x0, x1) + (abs(x1 - x0) - width) / 2
        top = max(y0, y1) - (abs(y1 - y0) - height) / 2
        objects = grid_to_objects(grid, palette, left, top, pixel)
        session.proposal.extend(ChangeSet(objects, [], f"image as {len(grid[0])}x{len(grid)} pixel art"))
        return {"objects": len(objects), "pixels": f"{len(grid[0])}x{len(grid)}", "colours": len(palette)}

    # --- queue ---

    def accept(self, name):
        session = self.sessions.get(name)
        if not session or not session.proposal:
            raise ValueError("There's nothing to accept yet.")
        with self.lock:
            self.queue.append({"level": name, "changes": session.proposal.to_json(),
                               "summary": session.proposal.summary or "changes", "time": time.time()})
            self._write_json("queue.json", self.queue)
            session.finish_proposal(True)
        return self.queue_view()

    def discard(self, name):
        session = self.sessions.get(name)
        if session:
            session.finish_proposal(False)
        return {"ok": True}

    def queue_view(self):
        return [{"level": q["level"], "summary": q["summary"], "adds": len(q["changes"]["add"]),
                 "removes": len(q["changes"]["remove"])} for q in self.queue]

    def unqueue(self, index):
        with self.lock:
            if 0 <= index < len(self.queue):
                self.queue.pop(index)
                self._write_json("queue.json", self.queue)
        return self.queue_view()

    def apply_queue(self):
        """Write all queued changes into the save file. GD must be closed."""
        with self.lock:
            if not self.queue:
                return {"applied": 0}
            if self.is_gd_running(fresh=True):
                raise RuntimeError("Close Geometry Dash first (Save and Exit), then apply.")
            levels = LocalLevels(self.save_path())
            report = {"applied": 0, "added": 0, "removed": 0, "missing_levels": []}
            for item in self.queue:
                try:
                    target = next(lv for lv in levels.levels if lv.get("k2", "") == item["level"])
                except StopIteration:
                    report["missing_levels"].append(item["level"])
                    continue
                level = Level(levels.level_string(target))
                result = ChangeSet.from_json(item["changes"]).apply(level)
                levels.set_level_string(target, level.to_string())
                report["applied"] += 1
                report["added"] += result["added"]
                report["removed"] += result["removed"]
            backup = levels.save(backup_dir=self.dir / "backups", quiet=True)
            self._prune_backups()
            self.history.append({"time": time.time(), "backup": backup.name if backup else "",
                                 **{k: v for k, v in report.items() if k != "missing_levels"},
                                 "levels": sorted({q["level"] for q in self.queue})})
            self.history = self.history[-50:]
            self._write_json("history.json", self.history)
            self.queue = [q for q in self.queue if q["level"] in report["missing_levels"]]
            self._write_json("queue.json", self.queue)
            self._levels = None
            self.levels()
            self.last_event = (f"Applied {report['added']} new objects to your save. "
                               "Open Geometry Dash to see them!")
            return report

    def _prune_backups(self):
        backups = sorted((self.dir / "backups").glob("*.backup-*"))
        for old in backups[:-MAX_BACKUPS]:
            old.unlink(missing_ok=True)

    def backups(self):
        return [{"name": p.name, "size": p.stat().st_size, "time": p.stat().st_mtime}
                for p in sorted((self.dir / "backups").glob("*.backup-*"), reverse=True)]

    def restore_backup(self, name):
        source = self.dir / "backups" / Path(name).name
        if not source.exists():
            raise ValueError("Backup not found.")
        if self.is_gd_running(fresh=True):
            raise RuntimeError("Close Geometry Dash first, then restore.")
        with self.lock:
            shutil.copy2(source, self.save_path())
            self._levels = None
            self.sessions = {}
        self.last_event = "Backup restored."
        return {"ok": True}

    # --- GD process ---

    def is_gd_running(self, fresh=False):
        checked, running = self._gd_cache
        if fresh or time.time() - checked > 2:
            running = self.gd_check()
            self._gd_cache = (time.time(), running)
        return running

    def launch_gd(self):
        if platform.system() == "Windows":
            os.startfile(STEAM_URL)  # noqa: S606 -- opens Steam, which starts GD
        else:
            webbrowser.open(STEAM_URL)
        return {"ok": True}

    def watch(self, stop):
        """Background loop: auto-apply the queue when GD isn't running."""
        while not stop.is_set():
            try:
                if self.queue and self.config.get("auto_apply", True) and not self.is_gd_running(fresh=True):
                    self.apply_queue()
            except Exception as e:  # keep watching; show the problem in the UI
                self.last_event = f"Couldn't apply: {e}"
            stop.wait(2)

    def status(self):
        path = self.save_path()
        return {"gd_running": self.is_gd_running(), "queue": self.queue_view(), "event": self.last_event,
                "save_mtime": path.stat().st_mtime if path.exists() else None, **self.settings()}


def make_handler(state, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path):
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            file = (STATIC / name).resolve()
            if STATIC.resolve() not in file.parents or not file.is_file():
                return self._send(404, {"error": "not found"})
            types = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                     ".svg": "image/svg+xml", ".png": "image/png"}
            self._send(200, file.read_bytes(), types.get(file.suffix, "application/octet-stream"))

        def _authorised(self):
            return secrets.compare_digest(self.headers.get("X-Token", ""), token)

        def do_GET(self):
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                return self._static(url.path)
            if not self._authorised():
                return self._send(403, {"error": "forbidden"})
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            self._dispatch(url.path, q, "GET")

        def do_POST(self):
            url = urlparse(self.path)
            if not self._authorised():
                return self._send(403, {"error": "forbidden"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 40_000_000:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            self._dispatch(url.path, body, "POST")

        def _dispatch(self, path, data, method):
            routes = {
                ("GET", "/api/status"): lambda: state.status(),
                ("GET", "/api/levels"): lambda: state.level_list(),
                ("GET", "/api/level"): lambda: state.level_view(data["name"]),
                ("GET", "/api/proposal"): lambda: state.proposal_view(data["level"]),
                ("GET", "/api/job"): lambda: state.jobs[data["id"]],
                ("GET", "/api/backups"): lambda: state.backups(),
                ("GET", "/api/settings"): lambda: state.settings(),
                ("POST", "/api/settings"): lambda: state.update_settings(data),
                ("POST", "/api/ask"): lambda: {"job": state.start_job(
                    data["level"], str(data["prompt"]), [float(v) for v in data["selection"]],
                    data.get("image"), data.get("mode"))},
                ("POST", "/api/stop"): lambda: setattr(state.session(data["level"]), "cancelled", True) or {"ok": True},
                ("POST", "/api/convert"): lambda: state.convert_image(
                    data["level"], [float(v) for v in data["selection"]], data["image"],
                    data.get("pixel_size", 7.5), data.get("colours", 24)),
                ("POST", "/api/accept"): lambda: state.accept(data["level"]),
                ("POST", "/api/discard"): lambda: state.discard(data["level"]),
                ("POST", "/api/unqueue"): lambda: state.unqueue(int(data["index"])),
                ("POST", "/api/apply"): lambda: state.apply_queue(),
                ("POST", "/api/restore"): lambda: state.restore_backup(data["name"]),
                ("POST", "/api/launch_gd"): lambda: state.launch_gd(),
                ("POST", "/api/pick_folder"): lambda: pick_folder(state),
            }
            handler = routes.get((method, path))
            if not handler:
                return self._send(404, {"error": "unknown endpoint"})
            try:
                self._send(200, handler())
            except FileNotFoundError as e:
                self._send(400, {"error": f"{e}. Set the save file path in Settings."})
            except (KeyError, ValueError, RuntimeError) as e:
                self._send(400, {"error": str(e).strip("'\"")})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def pick_folder(state):
    if not state.window:
        raise RuntimeError("Folder picker isn't available; paste the path instead.")
    import webview
    result = state.window.create_file_dialog(webview.FOLDER_DIALOG)
    return {"path": result[0] if result else ""}


def serve(state, port=0):
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    threading.Thread(target=state.watch, args=(stop,), daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    return server, url, stop
