"""Follow-me Discord voice overlay for OBS. Runs on the streaming PC, not the server.

Talks to the local Discord desktop client over its IPC pipe, follows whatever
call you're in (any server, DM, group DM), and serves a StreamKit-style overlay
for an OBS Browser Source at http://127.0.0.1:7373/.

The client secret stays on the server: codes and refresh tokens are swapped at
https://discord.howling.one/voice-rpc/token (bot/voicerpc.py).

Config: voice_overlay.json next to this file, {"key": "<VOICECHAT_SECRET>"}.
Optional keys: "server", "port", "client_id".

    python voice_overlay.py        (pythonw.exe to run with no console window)

Overlay URL options: ?hide_self=1  ?names=0
"""

import json
import os
import socket
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "voice_overlay.json").read_text())
KEY = CONFIG["key"]
SERVER = CONFIG.get("server", "https://discord.howling.one").rstrip("/")
PORT = int(CONFIG.get("port", 7373))
CLIENT_ID = str(CONFIG.get("client_id", "1461955868617867408"))
AUTH_PATH = HERE / "voice_overlay_auth.json"
LOG_PATH = HERE / "voice_overlay.log"

SCOPE_SETS = [["rpc", "rpc.voice.read", "identify"], ["rpc", "identify"]]
VOICE_EVENTS = ["VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE",
                "SPEAKING_START", "SPEAKING_STOP"]

OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = range(5)


def log(*a):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True) if sys.stdout else None
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ shared state

class State:
    def __init__(self):
        self.cond = threading.Condition()
        self.version = 0
        self.channel = None  # {"name", "guild_id"} or None
        self.people = {}  # user id -> dict
        self.me = None

    def snapshot(self):
        return {"version": self.version, "me": self.me, "channel": self.channel,
                "people": list(self.people.values())}

    def changed(self):
        with self.cond:
            self.version += 1
            self.cond.notify_all()


STATE = State()


# ------------------------------------------------------------------ IPC transport

class Pipe:
    """Blocking IPC connection. All reads and writes happen on one thread: a
    synchronous Windows pipe handle serializes I/O, so a reader thread would
    block every write."""

    def __init__(self):
        self.f = self.sock = None
        for i in range(10):
            try:
                if os.name == "nt":
                    self.f = open(rf"\\.\pipe\discord-ipc-{i}", "r+b", buffering=0)
                else:
                    base = next((os.environ[v] for v in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP")
                                 if os.environ.get(v)), "/tmp")
                    self.sock = socket.socket(socket.AF_UNIX)
                    self.sock.connect(os.path.join(base, f"discord-ipc-{i}"))
                return
            except OSError:
                self.sock = None
                continue
        raise ConnectionError("Discord isn't running (no discord-ipc pipe)")

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.f.read(n - len(buf)) if self.f else self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("pipe closed")
            buf += chunk
        return buf

    def send(self, op, payload):
        data = json.dumps(payload).encode()
        frame = struct.pack("<II", op, len(data)) + data
        if self.f:
            self.f.write(frame)
            self.f.flush()
        else:
            self.sock.sendall(frame)

    def recv(self):
        op, n = struct.unpack("<II", self._read(8))
        return op, json.loads(self._read(n))

    def close(self):
        for h in (self.f, self.sock):
            try:
                h and h.close()
            except OSError:
                pass


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self):
        self.pipe = Pipe()
        self.backlog = []  # DISPATCH events read while waiting on a reply

    def handshake(self):
        self.pipe.send(OP_HANDSHAKE, {"v": 1, "client_id": CLIENT_ID})
        op, msg = self.pipe.recv()
        if op == OP_CLOSE:
            raise RpcError(f"handshake refused: {msg}")
        log("connected to Discord", msg.get("data", {}).get("user", {}).get("username", ""))

    def _next(self):
        while True:
            op, msg = self.pipe.recv()
            if op == OP_PING:
                self.pipe.send(OP_PONG, msg)
            elif op == OP_CLOSE:
                raise ConnectionError(f"Discord closed the connection: {msg}")
            else:
                return msg

    def call(self, cmd, args=None, evt=None):
        nonce = str(uuid.uuid4())
        msg = {"cmd": cmd, "args": args or {}, "nonce": nonce}
        if evt:
            msg["evt"] = evt
        self.pipe.send(OP_FRAME, msg)
        while True:
            m = self._next()
            if m.get("nonce") == nonce:
                if m.get("evt") == "ERROR":
                    raise RpcError(f"{cmd} {evt or ''}: {m['data'].get('code')} {m['data'].get('message')}")
                return m.get("data")
            if m.get("cmd") == "DISPATCH":
                self.backlog.append(m)

    def close(self):
        self.pipe.close()

    def next_event(self):
        if self.backlog:
            return self.backlog.pop(0)
        while True:
            m = self._next()
            if m.get("cmd") == "DISPATCH":
                return m


# ------------------------------------------------------------------ auth

def token_call(body):
    req = urllib.request.Request(
        f"{SERVER}/voice-rpc/token", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Key": KEY,
                 "User-Agent": "voice-overlay/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RpcError(f"token exchange {e.code}: {e.read()[:300]!r}")
    data["expires_at"] = time.time() + data["expires_in"]
    AUTH_PATH.write_text(json.dumps(data))
    return data["access_token"]


def access_token(rpc, force_new=False):
    auth = None if force_new else (json.loads(AUTH_PATH.read_text()) if AUTH_PATH.exists() else None)
    if auth and auth["expires_at"] - time.time() > 86400:
        return auth["access_token"]
    if auth and auth.get("refresh_token"):
        try:
            return token_call({"refresh_token": auth["refresh_token"]})
        except Exception as e:
            log("refresh failed, re-authorizing:", e)
    last = None
    for scopes in SCOPE_SETS:
        log("asking Discord to authorize", scopes, "- approve the popup in the Discord app")
        try:
            code = rpc.call("AUTHORIZE", {"client_id": CLIENT_ID, "scopes": scopes})["code"]
            return token_call({"code": code})
        except RpcError as e:
            log("authorize failed:", e)
            last = e
    raise last


# ------------------------------------------------------------------ voice tracking

def person(vs):
    u, s = vs["user"], vs.get("voice_state") or {}
    return {"id": u["id"], "name": vs.get("nick") or u.get("global_name") or u["username"],
            "avatar": (f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png?size=128"
                       if u.get("avatar") else
                       f"https://cdn.discordapp.com/embed/avatars/{(int(u['id']) >> 22) % 6}.png"),
            "muted": bool(s.get("self_mute") or s.get("mute") or s.get("suppress")),
            "deafened": bool(s.get("self_deaf") or s.get("deaf")),
            "speaking": False}


class Tracker:
    def __init__(self, rpc):
        self.rpc = rpc
        self.channel_id = None

    def watch(self, channel):
        """channel: a full channel object (with voice_states), or None."""
        if self.channel_id:
            for evt in VOICE_EVENTS:
                try:
                    self.rpc.call("UNSUBSCRIBE", {"channel_id": self.channel_id}, evt)
                except RpcError as e:
                    log("unsubscribe:", e)
        self.channel_id = channel["id"] if channel else None
        with STATE.cond:
            STATE.people = {}
            STATE.channel = None
            if channel:
                STATE.channel = {"name": channel.get("name") or "",
                                 "guild_id": channel.get("guild_id"), "type": channel.get("type")}
                for vs in channel.get("voice_states") or []:
                    STATE.people[vs["user"]["id"]] = person(vs)
        if channel:
            for evt in VOICE_EVENTS:
                try:
                    self.rpc.call("SUBSCRIBE", {"channel_id": channel["id"]}, evt)
                except RpcError as e:
                    log("subscribe:", e)
            log(f"following {STATE.channel['name'] or '(call)'} "
                f"[{'server ' + str(channel.get('guild_id')) if channel.get('guild_id') else 'DM/group'}] "
                f"with {len(STATE.people)} people")
        else:
            log("not in a call")
        STATE.changed()

    def handle(self, evt, data):
        if evt == "VOICE_CHANNEL_SELECT":
            ch_id = data.get("channel_id")
            self.watch(self.rpc.call("GET_CHANNEL", {"channel_id": ch_id}) if ch_id else None)
            return
        with STATE.cond:
            if evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE"):
                prev = STATE.people.get(data["user"]["id"])
                p = person(data)
                p["speaking"] = bool(prev and prev["speaking"])
                STATE.people[p["id"]] = p
            elif evt == "VOICE_STATE_DELETE":
                STATE.people.pop(data["user"]["id"], None)
            elif evt in ("SPEAKING_START", "SPEAKING_STOP"):
                p = STATE.people.get(str(data.get("user_id")))
                if not p:
                    return
                p["speaking"] = evt == "SPEAKING_START"
            else:
                return
        STATE.changed()


def session():
    rpc = Rpc()
    try:
        rpc.handshake()
        for attempt in (1, 2):
            try:
                me = rpc.call("AUTHENTICATE", {"access_token": access_token(rpc, force_new=attempt == 2)})
                break
            except RpcError as e:
                if attempt == 2:
                    raise
                log("authenticate failed, re-authorizing:", e)
        STATE.me = me["user"]["id"]
        log("authenticated as", me["user"]["username"])
        tracker = Tracker(rpc)
        rpc.call("SUBSCRIBE", {}, "VOICE_CHANNEL_SELECT")
        tracker.watch(rpc.call("GET_SELECTED_VOICE_CHANNEL"))
        while True:
            m = rpc.next_event()
            tracker.handle(m.get("evt"), m.get("data") or {})
    finally:
        rpc.close()
        with STATE.cond:
            STATE.channel, STATE.people = None, {}
        STATE.changed()


def rpc_loop():
    quiet = False
    while True:
        try:
            session()
        except ConnectionError as e:
            # "not running" repeats every 5s while Discord is closed; log it once
            if not quiet or "isn't running" not in str(e):
                log(e, "- retrying every 5s")
            quiet = "isn't running" in str(e)
        except Exception:
            log("error:", traceback.format_exc())
            quiet = False
        else:
            quiet = False
        time.sleep(5)


# ------------------------------------------------------------------ HTTP / overlay

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = OVERLAY_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/state":
            body = json.dumps(STATE.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            seen = -1
            try:
                while True:
                    with STATE.cond:
                        if STATE.version == seen:
                            STATE.cond.wait(timeout=15)
                        snap = STATE.snapshot()
                    if snap["version"] != seen:
                        seen = snap["version"]
                        self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode())
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except OSError:
                return
        else:
            self.send_error(404)


OVERLAY_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Voice</title>
<style>
  html, body { margin: 0; background: transparent; overflow: hidden; }
  body { font-family: "gg sans", "Whitney", "Helvetica Neue", Helvetica, Arial, sans-serif; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { display: flex; align-items: center; height: 44px; margin-bottom: 4px; }
  .av { width: 40px; height: 40px; border-radius: 50%; box-sizing: border-box;
        border: 3px solid transparent; flex: none; transition: border-color .08s; }
  li.speaking .av { border-color: #43b581; }
  .name { margin-left: 8px; padding: 4px 6px; border-radius: 3px; font-size: 14px;
          color: #fff; background: rgba(30, 33, 36, .95); white-space: nowrap; }
  li.speaking .name { color: #fff; }
  .ic { width: 16px; height: 16px; margin-left: 4px; flex: none; }
  li.dimmed .av { opacity: .6; }
</style></head>
<body><ul id="list"></ul>
<script>
const q = new URLSearchParams(location.search);
const hideSelf = q.get("hide_self") === "1", showNames = q.get("names") !== "0";
const MIC_OFF = '<svg class="ic" viewBox="0 0 24 24"><path fill="#f04747" d="M6.7 11H5c0 1.19.34 2.3.9 3.28l1.23-1.23A4.9 4.9 0 0 1 6.7 11zM9.01 11.085V5a3 3 0 0 1 5.99-.3L9.01 11.085zM11.724 16.93 13.2 15.46A4 4 0 0 0 16 12v-1h1.7a5.7 5.7 0 0 1-4.7 5.6V20h-2v-3.07h.724zM21 4.27 19.73 3 3 19.73 4.27 21l4.3-4.3.01.01L21 4.27z"/></svg>';
const DEAF = '<svg class="ic" viewBox="0 0 24 24"><path fill="#f04747" d="M6.16 15.19 4.02 17.33A3 3 0 0 1 3 15v-3a9 9 0 0 1 14.53-7.1l-1.43 1.43A7 7 0 0 0 5 12v1h2c.2 0 .4.03.58.08l-1.42 1.11zM21 4.27 19.73 3 3 19.73 4.27 21l3.06-3.06V20c0 .55.45 1 1 1h1a3 3 0 0 0 3-3v-3a3 3 0 0 0-2.07-2.85L18.9 4.18l.01.01L21 4.27zM19 12v1h-.83l1.72-1.72c.07.23.11.47.11.72v3a3 3 0 0 1-3 3h-1v-5.83l2.9-2.9c.07.57.1 1.14.1 1.73z"/></svg>';

function esc(s) { return s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function render(s) {
  const people = (s.people || []).filter(p => !(hideSelf && p.id === s.me));
  document.getElementById("list").innerHTML = people.map(p => {
    const cls = [p.speaking ? "speaking" : "", p.muted || p.deafened ? "dimmed" : ""].join(" ");
    return `<li class="${cls}"><img class="av" src="${esc(p.avatar)}">` +
      (showNames ? `<span class="name">${esc(p.name)}</span>` : "") +
      (p.deafened ? DEAF : p.muted ? MIC_OFF : "") + `</li>`;
  }).join("");
}
function connect() {
  const es = new EventSource("/events");
  es.onmessage = e => render(JSON.parse(e.data));
  es.onerror = () => { render({ people: [] }); es.close(); setTimeout(connect, 2000); };
}
connect();
</script></body></html>
"""


def main():
    threading.Thread(target=rpc_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    log(f"overlay at http://127.0.0.1:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
