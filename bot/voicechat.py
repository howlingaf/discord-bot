"""
Voice-chat overlay: a /voice-chat slash command generates a unique token
tied to the voice channel the user is currently in.  Opening the link
shows a pop-out page with the member list and live text chat for that
specific channel, streamed over WebSocket.
"""

import json
import secrets
import time
from typing import TYPE_CHECKING

import discord
from aiohttp import web

from .config import PUBLIC_BASE_URL, VOICECHAT_SECRET, GUILD_ID, SPOTIFY_ALLOWED_USER_ID

if TYPE_CHECKING:
    from .client import MyBot

# ── session registry ────────────────────────────────────────────────
# token -> {channel_id, user_id, created_at}
_sessions: dict[str, dict] = {}
_SESSION_TTL = 60 * 60 * 12  # 12 hours

# channel_id -> discord.Webhook (cached per channel)
_webhooks: dict[int, discord.Webhook] = {}

# channel_id -> list[dict]  (recent messages per channel)
_recent: dict[int, list[dict]] = {}
_MAX_RECENT = 100

# channel_id -> set[WebSocketResponse]
_ws_by_channel: dict[int, set[web.WebSocketResponse]] = {}


def _prune_sessions():
    now = time.time()
    expired = [t for t, s in _sessions.items() if now - s["created_at"] > _SESSION_TTL]
    for t in expired:
        del _sessions[t]


def create_session(channel_id: int, user_id: int) -> str:
    _prune_sessions()
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"channel_id": channel_id, "user_id": user_id, "created_at": time.time()}
    return token


def _resolve_token(request: web.Request) -> dict:
    # Channel ID from path or query
    channel = request.match_info.get("channel_id") or request.query.get("channel", "")

    # Static key: /voice-chat?key=<secret>&channel=<id> or /voice-chat/<id>?key=<secret>
    key = request.query.get("key", "")
    if key and channel and VOICECHAT_SECRET and key == VOICECHAT_SECRET:
        try:
            return {"channel_id": int(channel), "user_id": 0}
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid channel ID")

    # Per-session token
    token = request.query.get("token", "")
    session = _sessions.get(token)
    if not session:
        raise web.HTTPForbidden(text="Invalid or expired token")
    return session


def _watched_channels() -> set[int]:
    """Return channel IDs that have at least one active WS client."""
    return {cid for cid, clients in _ws_by_channel.items() if clients}


# ── payloads ────────────────────────────────────────────────────────
def _member_payload(m: discord.Member) -> dict:
    return {
        "id": str(m.id),
        "name": m.display_name,
        "avatar": m.display_avatar.url,
        "bot": m.bot,
    }


def _msg_payload(msg: discord.Message) -> dict:
    attachments = []
    for a in msg.attachments:
        attachments.append({"url": a.url, "filename": a.filename, "content_type": a.content_type or ""})
    return {
        "id": str(msg.id),
        "author": msg.author.display_name,
        "avatar": msg.author.display_avatar.url,
        "content": msg.content,
        "timestamp": msg.created_at.isoformat(),
        "attachments": attachments,
    }


async def _broadcast(channel_id: int, data: dict):
    clients = _ws_by_channel.get(channel_id)
    if not clients:
        return
    payload = json.dumps(data)
    dead = []
    for ws in clients:
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


# ── public hooks (called from events.py) ────────────────────────────
async def on_voice_update(bot: "MyBot", channel_id: int):
    """Re-broadcast the full member list for a watched channel."""
    if channel_id not in _watched_channels():
        return
    ch = bot.get_channel(channel_id)
    if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
        return
    members = [_member_payload(m) for m in ch.members]
    await _broadcast(channel_id, {"type": "members", "members": members})


async def on_chat_message(msg: discord.Message):
    cid = msg.channel.id
    if cid not in _watched_channels():
        return
    p = _msg_payload(msg)
    buf = _recent.setdefault(cid, [])
    buf.append(p)
    if len(buf) > _MAX_RECENT:
        del buf[: len(buf) - _MAX_RECENT]
    await _broadcast(cid, {"type": "message", "message": p})


async def on_chat_edit(msg: discord.Message):
    cid = msg.channel.id
    if cid not in _watched_channels():
        return
    p = _msg_payload(msg)
    for i, m in enumerate(_recent.get(cid, [])):
        if m["id"] == p["id"]:
            _recent[cid][i] = p
            break
    await _broadcast(cid, {"type": "edit", "message": p})


async def on_chat_delete(payload: discord.RawMessageDeleteEvent):
    cid = payload.channel_id
    if cid not in _watched_channels():
        return
    mid = str(payload.message_id)
    if cid in _recent:
        _recent[cid] = [m for m in _recent[cid] if m["id"] != mid]
    await _broadcast(cid, {"type": "delete", "id": mid})


async def _get_or_create_webhook(channel, bot) -> discord.Webhook | None:
    cid = channel.id
    if cid in _webhooks:
        return _webhooks[cid]
    try:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.user and wh.user.id == bot.user.id:
                _webhooks[cid] = wh
                return wh
        wh = await channel.create_webhook(name="Voice Chat")
        _webhooks[cid] = wh
        return wh
    except Exception as e:
        print(f"[VOICECHAT] webhook creation failed: {e}")
        return None


async def _handle_ws(request: web.Request, bot: "MyBot") -> web.WebSocketResponse:
    session = _resolve_token(request)
    cid = session["channel_id"]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _ws_by_channel.setdefault(cid, set()).add(ws)
    try:
        ch = bot.get_channel(cid)
        members = []
        if ch and isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            members = [_member_payload(m) for m in ch.members]
        messages = list(_recent.get(cid, []))

        can_send = session.get("user_id") is not None

        await ws.send_str(json.dumps({
            "type": "init", "members": members, "messages": messages,
            "canSend": can_send,
        }))
        async for ws_msg in ws:
            if ws_msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(ws_msg.data)
            except Exception:
                continue
            if data.get("type") == "send" and can_send and data.get("content", "").strip():
                content = data["content"].strip()[:2000]
                try:
                    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
                    owner = guild.get_member(SPOTIFY_ALLOWED_USER_ID) if guild and SPOTIFY_ALLOWED_USER_ID else None
                    webhook = await _get_or_create_webhook(ch, bot)
                    if webhook and owner:
                        await webhook.send(
                            content,
                            username=owner.display_name,
                            avatar_url=owner.display_avatar.url,
                        )
                    else:
                        await ch.send(content)
                except Exception as e:
                    print(f"[VOICECHAT] send failed: {e}")
    finally:
        _ws_by_channel.get(cid, set()).discard(ws)
    return ws


# ── routes ──────────────────────────────────────────────────────────
def register_routes(app: web.Application, bot: "MyBot"):
    routes = web.RouteTableDef()

    @routes.get("/voice-chat")
    async def voice_chat_page(request: web.Request):
        session = _resolve_token(request)
        qs = request.query_string
        ch = bot.get_channel(session["channel_id"])
        ch_name = getattr(ch, "name", "Voice Chat")
        html = _build_html(qs, ch_name)
        return web.Response(text=html, content_type="text/html")

    @routes.get("/voice-chat/{channel_id}")
    async def voice_chat_page_path(request: web.Request):
        session = _resolve_token(request)
        qs = request.query_string
        ch = bot.get_channel(session["channel_id"])
        ch_name = getattr(ch, "name", "Voice Chat")
        html = _build_html(qs, ch_name, ws_path=f"/voice-chat/{request.match_info['channel_id']}/ws")
        return web.Response(text=html, content_type="text/html")

    @routes.get("/voice-chat/{channel_id}/ws")
    async def voice_chat_ws_path(request: web.Request):
        return await _handle_ws(request, bot)

    @routes.get("/voice-chat/ws")
    async def voice_chat_ws(request: web.Request):
        return await _handle_ws(request, bot)

    app.add_routes(routes)


# ── slash command (registered in commands.py) ───────────────────────
def register_command(bot: "MyBot"):
    @bot.tree.command(name="voice-chat", description="Get a pop-out link for this voice channel's chat")
    async def voice_chat_cmd(interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message("You need to be in a voice channel.", ephemeral=True)
            return

        vc = member.voice.channel
        token = create_session(vc.id, member.id)
        url = f"{PUBLIC_BASE_URL}/voice-chat?token={token}"
        await interaction.response.send_message(
            f"Open this link to pop out **#{vc.name}** chat:\n{url}\n\nThis link is private to you and expires in 12 hours.",
            ephemeral=True,
        )


# ── HTML ────────────────────────────────────────────────────────────
def _build_html(query_string: str, channel_name: str, ws_path: str = "/voice-chat/ws") -> str:
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{channel_name} — Voice Chat</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: #1e1f22; color: #dbdee1; font-family: "gg sans", "Noto Sans", Helvetica, Arial, sans-serif;
  font-size: 14px; display: flex; flex-direction: column; height: 100vh; overflow: hidden;
}}
a {{ color: #00a8fc; }}

/* ── top bar: voice members ── */
#member-bar {{
  display: flex; align-items: center; background: #2b2d31; border-bottom: 1px solid #1e1f22;
  padding: 8px 12px; gap: 8px; min-height: 52px;
}}
#member-bar .label {{
  font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .02em;
  color: #949ba4; white-space: nowrap;
}}
#member-viewport {{
  flex: 1; overflow: hidden; position: relative;
}}
#member-list {{
  display: flex; gap: 8px; transition: transform 0.3s ease;
}}
.member {{
  display: flex; align-items: center; gap: 6px; padding: 4px 8px; border-radius: 4px;
  white-space: nowrap; flex-shrink: 0;
}}
.member:hover {{ background: #35373c; }}
.member img {{ width: 28px; height: 28px; border-radius: 50%; }}
.member .name {{ font-size: 13px; font-weight: 500; }}
.member .bot-tag {{
  background: #5865f2; color: #fff; font-size: 10px; padding: 1px 4px; border-radius: 3px;
  font-weight: 600; margin-left: 2px;
}}
.nav-btn {{
  background: none; border: none; color: #949ba4; cursor: pointer; font-size: 18px;
  padding: 4px 6px; border-radius: 4px; flex-shrink: 0;
}}
.nav-btn:hover {{ background: #35373c; color: #dbdee1; }}
.nav-btn:disabled {{ opacity: 0.3; cursor: default; }}
.nav-btn:disabled:hover {{ background: none; color: #949ba4; }}

/* ── main: chat ── */
#main {{ flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; }}
#chat-header {{
  padding: 12px 16px; font-weight: 600; border-bottom: 1px solid #1e1f22;
  background: #2b2d31; font-size: 15px;
}}
#messages {{
  flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 4px;
}}
.msg {{
  display: flex; gap: 12px; padding: 2px 0;
}}
.msg.grouped {{ padding-left: 52px; }}
.msg img.avatar {{ width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0; margin-top: 2px; }}
.msg .body {{ min-width: 0; }}
.msg .header {{ display: flex; align-items: baseline; gap: 8px; }}
.msg .author {{ font-weight: 600; font-size: 14px; color: #f2f3f5; }}
.msg .time {{ font-size: 11px; color: #949ba4; }}
.msg .text {{ line-height: 1.4; white-space: pre-wrap; word-break: break-word; }}
.msg .text:empty {{ display: none; }}
.msg .attachments {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }}
.msg .attachments img {{ max-width: 300px; max-height: 200px; border-radius: 4px; }}
.msg .attachments a {{ display: block; }}

/* ── input bar ── */
#input-bar {{
  display: none; padding: 0 16px 12px; background: #1e1f22;
}}
#input-bar form {{
  display: flex; gap: 8px;
}}
#input-bar input {{
  flex: 1; padding: 10px 12px; border-radius: 8px; border: none; outline: none;
  background: #383a40; color: #dbdee1; font-size: 14px;
  font-family: inherit;
}}
#input-bar input::placeholder {{ color: #6d6f78; }}

/* ── status bar ── */
#status {{
  padding: 6px 16px; font-size: 12px; color: #949ba4; background: #2b2d31;
  border-top: 1px solid #1e1f22; text-align: center;
}}
.connected {{ color: #23a55a !important; }}
.disconnected {{ color: #f23f43 !important; }}

/* scrollbar */
::-webkit-scrollbar {{ width: 8px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: #1a1b1e; border-radius: 4px; }}
</style>
</head>
<body>

<div id="member-bar">
  <span class="label">In Voice &mdash; <span id="count">0</span></span>
  <button class="nav-btn" id="nav-left" disabled>&lsaquo;</button>
  <div id="member-viewport"><div id="member-list"></div></div>
  <button class="nav-btn" id="nav-right" disabled>&rsaquo;</button>
</div>

<div id="main">
  <div id="chat-header"># {channel_name}</div>
  <div id="messages"></div>
  <div id="input-bar">
    <form id="send-form"><input type="text" id="msg-input" placeholder="Send a message" autocomplete="off"></form>
  </div>
  <div id="status" class="disconnected">Connecting&hellip;</div>
</div>

<script>
const qs = {json.dumps(query_string)};
const proto = location.protocol === "https:" ? "wss:" : "ws:";
const wsUrl = proto + "//" + location.host + {json.dumps(ws_path)} + "?" + qs;

const memberList = document.getElementById("member-list");
const countEl = document.getElementById("count");
const messagesEl = document.getElementById("messages");
const statusEl = document.getElementById("status");

let ws;
let reconnectDelay = 1000;

function connect() {{
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {{
    statusEl.textContent = "Connected";
    statusEl.className = "connected";
    reconnectDelay = 1000;
  }};

  ws.onclose = () => {{
    statusEl.textContent = "Disconnected — reconnecting\\u2026";
    statusEl.className = "disconnected";
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  }};

  ws.onmessage = (e) => {{
    const data = JSON.parse(e.data);
    if (data.type === "init") {{
      renderMembers(data.members);
      messagesEl.innerHTML = "";
      data.messages.forEach(m => appendMessage(m));
      scrollToBottom(true);
      if (data.canSend) document.getElementById("input-bar").style.display = "block";
    }} else if (data.type === "members") {{
      renderMembers(data.members);
    }} else if (data.type === "message") {{
      appendMessage(data.message);
      scrollToBottom();
    }} else if (data.type === "edit") {{
      editMessage(data.message);
    }} else if (data.type === "delete") {{
      deleteMessage(data.id);
    }}
  }};
}}

let scrollOffset = 0;

function renderMembers(members) {{
  memberList.innerHTML = "";
  countEl.textContent = members.filter(m => !m.bot).length;
  members.forEach(m => {{
    const div = document.createElement("div");
    div.className = "member";
    div.innerHTML = '<img src="' + escHtml(m.avatar) + '" alt="">'
      + '<span class="name">' + escHtml(m.name) + (m.bot ? '<span class="bot-tag">BOT</span>' : '') + '</span>';
    memberList.appendChild(div);
  }});
  scrollOffset = 0;
  memberList.style.transform = "translateX(0)";
  updateNavButtons();
}}

const viewport = document.getElementById("member-viewport");
const navLeft = document.getElementById("nav-left");
const navRight = document.getElementById("nav-right");

function updateNavButtons() {{
  const maxScroll = Math.max(0, memberList.scrollWidth - viewport.clientWidth);
  navLeft.disabled = scrollOffset <= 0;
  navRight.disabled = scrollOffset >= maxScroll;
}}

navLeft.addEventListener("click", () => {{
  scrollOffset = Math.max(0, scrollOffset - 200);
  memberList.style.transform = "translateX(-" + scrollOffset + "px)";
  updateNavButtons();
}});

navRight.addEventListener("click", () => {{
  const maxScroll = Math.max(0, memberList.scrollWidth - viewport.clientWidth);
  scrollOffset = Math.min(maxScroll, scrollOffset + 200);
  memberList.style.transform = "translateX(-" + scrollOffset + "px)";
  updateNavButtons();
}});

window.addEventListener("resize", updateNavButtons);

let lastAuthor = null;
let lastTimestamp = 0;

function appendMessage(m) {{
  const div = document.createElement("div");
  const ts = new Date(m.timestamp).getTime();
  const grouped = m.author === lastAuthor && (ts - lastTimestamp) < 300000;
  div.className = "msg" + (grouped ? " grouped" : "");
  div.dataset.id = m.id;

  let html = "";
  if (!grouped) {{
    html += '<img class="avatar" src="' + escHtml(m.avatar) + '" alt="">';
    html += '<div class="body"><div class="header"><span class="author">' + escHtml(m.author)
      + '</span><span class="time">' + formatTime(m.timestamp) + '</span></div>'
      + '<div class="text">' + escHtml(m.content) + '</div>' + renderAttachments(m.attachments) + '</div>';
  }} else {{
    html += '<div class="body"><div class="text">' + escHtml(m.content) + '</div>' + renderAttachments(m.attachments) + '</div>';
  }}
  div.innerHTML = html;
  messagesEl.appendChild(div);
  lastAuthor = m.author;
  lastTimestamp = ts;
}}

function editMessage(m) {{
  const el = messagesEl.querySelector('[data-id="' + m.id + '"]');
  if (!el) return;
  const textEl = el.querySelector(".text");
  if (textEl) textEl.textContent = m.content;
}}

function deleteMessage(id) {{
  const el = messagesEl.querySelector('[data-id="' + id + '"]');
  if (el) el.remove();
}}

function renderAttachments(attachments) {{
  if (!attachments || !attachments.length) return "";
  let html = '<div class="attachments">';
  attachments.forEach(a => {{
    if (a.content_type && a.content_type.startsWith("image/")) {{
      html += '<a href="' + escHtml(a.url) + '" target="_blank"><img src="' + escHtml(a.url) + '" alt="' + escHtml(a.filename) + '"></a>';
    }} else {{
      html += '<a href="' + escHtml(a.url) + '" target="_blank">' + escHtml(a.filename) + '</a>';
    }}
  }});
  return html + "</div>";
}}

function formatTime(iso) {{
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString([], {{ hour: "2-digit", minute: "2-digit" }});
  return sameDay ? time : d.toLocaleDateString([], {{ month: "short", day: "numeric" }}) + " " + time;
}}

function isNearBottom() {{
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
}}

function scrollToBottom(force) {{
  if (force || isNearBottom()) {{
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }}
}}

function escHtml(s) {{
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}}

document.getElementById("send-form").addEventListener("submit", (e) => {{
  e.preventDefault();
  const input = document.getElementById("msg-input");
  const text = input.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({{ type: "send", content: text }}));
  input.value = "";
}});

connect();
</script>
</body>
</html>'''
