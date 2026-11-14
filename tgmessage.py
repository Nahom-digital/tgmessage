#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tgmessage - talk to your Telegram bot from any terminal.

    Me[Alice]>  hello there
    Bot> hey, got it

One folder, two faces:

    tgmessage.py daemon    long-polls Telegram, keeps per-chat history, and
                           serves a small JSON-lines socket on TG_HOST:TG_PORT.
                           This is what the systemd service runs, so messages
                           keep arriving (and are kept) across reboots.

    tgmessage.py chat      the interactive Me>/Bot> terminal UI. It talks to
                           the daemon over that socket. Ctrl+C exits.

Multi-chat model
----------------
A bot can be messaged by many people, but it may only talk to people who are
allowed. Permission levels are managed in .env by the CLI:

    tgmessage allow <id>         chat only (send/receive/files)
    tgmessage allow sys <id>     + /status, /logs, /cancel_upload, /ai and the
                                 opt-in /sh-on root shell from Telegram
    tgmessage allow mang <id>    + watcher reports and /recover (manage)
    tgmessage deny [sys|mang] <id>
    tgmessage service add <unit> watch a systemd service (restart + alert)
    tgmessage shell on           enable /sh-on for sys chats (off by default)

Anything from an unknown private chat is held as a *request*
(state/pending.json) until you allow it:

    tgmessage --request          list who is asking to talk
    tgmessage --allow 12345      allow that chat (saved to .env)
    tgmessage --deny  12345      revoke it

Every allowed chat has its own history file, state/<chatid>.jsonl, and its own
session in the UI:

    /session            list sessions and pick one
    /session 2          switch to session 2
    /session 12345      switch by chat id

Everything lives in TG_HOME (default /root/tgmessage):

    .env                token + allowed chats + default chat + host + port
    tgmessage.py        this file
    tgmessage           shell wrapper (this is what you type)
    tgmessage.service   systemd unit -> survives reboot
    install.sh          installer
    watcher/            the self-healing system watcher - part of this app
        watcher.py      repairs network, clock, services, disk...
        watcher.sh      `watcher` CLI (also /usr/local/bin/watcher)
        logs/watcher.log  its log - the /logs chat command reads this
    state/
        offset.json     Telegram update offset (so nothing is lost on reboot)
        <chatid>.jsonl  one history file per allowed chat
        pending.json    chats waiting to be allowed
        chats.json      chat metadata (names, counts, last seen)
        workspace.json  saved cd, used by /sh-on and /ai
        daemon.log      daemon output
        daemon.pid      pid, only when running without systemd

Only the standard library is used. No pip installs.

Large files: Telegram's cloud Bot API caps bot downloads at 20 MB.
`tgmessage local-api <api_id> <api_hash>` configures the optional local
Bot API server (tdlib/telegram-bot-api), which serves files from local disk
and lifts the cap to 2 GB. Incoming files are downloaded through one ordered
queue, so a low-end machine only transfers one file at a time.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import getpass
import json
import mimetypes
import os
import queue
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_HOME = "/root/tgmessage"
DEFAULT_API_ROOT = "https://api.telegram.org"
LOCAL_API_PORT = 8081
LOCAL_API_SERVICE = "telegram-bot-api"
LOCAL_API_UNIT = "/etc/systemd/system/telegram-bot-api.service"
LOCAL_API_BIN = "/usr/local/bin/telegram-bot-api"
LOCAL_API_REPO = "https://github.com/tdlib/telegram-bot-api"
LOCAL_API_SRC = "/opt/telegram-bot-api"
SERVICE_NAME = "tgmessage"
MAX_FILE = 50 * 1024 * 1024  # Telegram cloud Bot API limit for uploads
LOCAL_GETFILE_TIMEOUT = 6 * 60 * 60  # local server may spend hours fetching a file


def api_root(cfg):
    """The Bot API root in use (official cloud or a local server)."""
    return (getattr(cfg, "api_root", "") or DEFAULT_API_ROOT).rstrip("/")


def api_url(cfg, method, token=None):
    return f"{api_root(cfg)}/bot{token or cfg.token}/{method}"


C = {
    "rst": "\033[0m",
    "b": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "red": "\033[31m",
    "yellow": "\033[33m",
}


def colour(ok):
    """Strip colour when the output is not a terminal."""
    if ok:
        return C
    return {k: "" for k in C}


def human_size(n):
    """1234 -> '1.2 KB'. Good enough for a chat label."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def parse_ids(raw):
    """'123, 456 789' -> ['123', '456', '789'] (order kept, deduped)."""
    if raw is None:
        return []
    out, seen = [], set()
    for chunk in str(raw).replace(",", " ").split():
        cid = chunk.strip()
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def parse_bool(raw):
    """Parse .env booleans: 1/true/yes/on (case-insensitive) mean true."""
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on", "y", "enabled")


def parse_seconds(raw, default, minimum, maximum):
    """Parse a bounded seconds setting, falling back to `default`."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def human_duration(seconds):
    """600 -> '10m', 1800 -> '30m', 45 -> '45s'."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def id_sort_key(cid):
    try:
        return (0, int(cid))
    except (TypeError, ValueError):
        return (1, str(cid))


def looks_like_id(text):
    try:
        int(str(text).strip())
        return True
    except (TypeError, ValueError):
        return False


def chat_meta(chat):
    """The bits of a Telegram `chat` object worth remembering/displaying."""
    cid = str(chat.get("id", ""))
    name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x).strip()
    if not name:
        name = (chat.get("title") or "").strip()
    username = chat.get("username") or ""
    if not name:
        name = f"@{username}" if username else cid
    return {"name": name or cid, "username": username, "type": chat.get("type") or ""}


def tmux_client_ttys():
    """The ttys of every tmux client attached to the server, if any."""
    try:
        done = subprocess.run(
            ["tmux", "list-clients", "-F", "#{client_tty}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [line.strip() for line in done.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def osc52(text):
    """Put text in the clipboard of the terminal the user is actually at.

    Uses the OSC 52 escape sequence, so it works over SSH and in containers
    with no X server or clipboard daemon. Returns a short note for the UI.
    """
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    seq = f"\033]52;c;{payload}\007"
    note = ""

    if os.environ.get("TMUX"):
        # tmux swallows escape sequences written from inside a pane, so write to
        # the attached client's tty instead of our own.
        ttys = tmux_client_ttys()
        for tty in ttys:
            try:
                with open(tty, "wb") as fh:
                    fh.write(seq.encode("ascii"))
                return note
            except OSError:
                continue
        note = " (no tmux client attached)"
        seq = f"\033Ptmux;\033{seq}\033\\"

    try:
        with open("/dev/tty", "wb") as tty:
            tty.write(seq.encode("ascii"))
        return note
    except OSError:
        sys.stdout.write(seq)
        sys.stdout.flush()
        return note + " (via stdout)"


# --------------------------------------------------------------------------
# .env handling
# --------------------------------------------------------------------------

ENV_TEMPLATE = [
    ("TG_BOT_TOKEN", "", "BotFather token, looks like 123456789:AA........"),
    ("TG_API_ROOT", "", "Bot API root; empty = Telegram cloud, local server: http://127.0.0.1:8081"),
    ("TG_API_ID", "", "Telegram api_id for the local Bot API server (my.telegram.org)"),
    ("TG_API_HASH", "", "Telegram api_hash for the local Bot API server"),
    ("TG_LOCAL_API_PORT", "8081", "local Bot API server port"),
    ("TG_LOCAL_API_DIR", "/var/lib/telegram-bot-api", "local Bot API server file storage"),
    ("TG_LOCAL_API_SRC", LOCAL_API_SRC, "local Bot API source; built automatically if missing"),
    ("TG_LOCAL_API_JOBS", "1", "build jobs (1 = low-end friendly)"),
    ("TG_MAX_DOWNLOAD_MB", "", "max download size in MB (default: 20 cloud, 2000 local)"),
    ("TG_MAX_UPLOAD_MB", "", "max upload size in MB (default: 50 cloud, 2000 local)"),
    ("TG_ALLOWED_CHAT_IDS", "", "chat ids allowed to talk to this bot"),
    ("TG_SYS_CHAT_IDS", "", "chat ids allowed to use /status, /logs, /cancel_upload and the root shell (tgmessage allow sys)"),
    ("TG_MANG_CHAT_IDS", "", "chat ids that receive watcher reports (tgmessage allow mang)"),
    ("TG_SHELL_ENABLED", "0", "root shell from Telegram for sys chats - /sh-on, /sh-off (tgmessage shell on)"),
    ("TG_SHELL_IDLE_SECONDS", "600", "close an idle /sh-on shell after this many seconds"),
    ("TG_SHELL_MAX_SECONDS", "1800", "maximum lifetime of one /sh-on shell, in seconds"),
    ("TG_SHELL_TIMEOUT", "120", "kill a Telegram shell command after this many seconds"),
    ("TG_WATCH_SERVICES", "", "systemd services watched by the watcher (tgmessage service add)"),
    ("TG_SERVICE_NO_RESTART", "", "watched services the watcher reports but never restarts (tgmessage service <unit> stop)"),
    ("TG_DEFAULT_CHAT", "", "chat id used by one-shot sends and on startup"),
    ("TG_HOST", "127.0.0.1", "daemon bind address"),
    ("TG_PORT", "8765", "daemon port"),
    ("TG_HISTORY", "300", "how many messages of backlog to keep per chat"),
    ("TG_POLL_TIMEOUT", "30", "getUpdates long-poll seconds"),
    ("TG_WATCHER_LOG", "", "watcher log shown by /logs (default: <home>/watcher/logs/watcher.log)"),
]

UNIT_SUFFIXES = (
    ".service", ".timer", ".path", ".socket", ".target", ".mount",
    ".automount", ".swap", ".slice", ".scope", ".device",
)


def normalize_unit(name):
    """'nginx' -> 'nginx.service'; units with a known suffix are kept."""
    name = str(name or "").strip()
    if not name:
        return ""
    if not name.endswith(UNIT_SUFFIXES):
        name += ".service"
    if not re.fullmatch(r"[A-Za-z0-9@_.:\\-]+", name):
        return ""
    return name


def parse_units(raw):
    """Parse TG_WATCH_SERVICES into a stable, de-duplicated unit list."""
    out = []
    for chunk in str(raw or "").replace(",", " ").split():
        unit = normalize_unit(chunk)
        if unit and unit not in out:
            out.append(unit)
    return out


def env_file(home):
    return os.path.join(home, ".env")


def read_env(home):
    data = {}
    path = env_file(home)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                data[key] = val
    return data


def write_env(home, updates):
    """Merge `updates` into .env, keeping everything else and the comments.

    A value of None deletes the key.
    """
    path = env_file(home)
    current = read_env(home)
    for key, val in updates.items():
        if val is None:
            current.pop(str(key), None)
        else:
            current[str(key)] = str(val)

    lines = [
        "# tgmessage configuration",
        "# Edited by `tgmessage --token ...` / `--port ...`, safe to edit by hand.",
        "",
    ]
    written = set()
    for key, default, comment in ENV_TEMPLATE:
        val = current.get(key, default)
        written.add(key)
        lines.append(f"# {comment}")
        lines.append(f"{key}={val}")
        lines.append("")
    for key in sorted(set(current) - written):
        lines.append(f"{key}={current[key]}")
    os.makedirs(home, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    os.chmod(tmp, 0o600)  # the file holds the bot token and API hash
    os.replace(tmp, path)
    return current


class Config:
    def __init__(self, home):
        self.home = os.path.abspath(os.path.expanduser(home))
        disk = read_env(self.home)

        def get(key, default=""):
            return os.environ.get(key) or disk.get(key) or default

        def shell_get(key, default=""):
            # `.env` is the source of truth for the shell: systemd's
            # EnvironmentFile has a stale copy in os.environ after the CLI
            # edits the file.
            if key in disk:
                return disk[key]
            return os.environ.get(key) or default

        self.token = get("TG_BOT_TOKEN").strip()
        self.allowed = parse_ids(get("TG_ALLOWED_CHAT_IDS"))
        self.sys = parse_ids(get("TG_SYS_CHAT_IDS"))
        self.mang = parse_ids(get("TG_MANG_CHAT_IDS"))
        # Higher levels imply lower ones: mang -> sys -> chat.
        self.mang = list(dict.fromkeys(self.mang))
        self.sys = list(dict.fromkeys(self.sys + self.mang))
        self.allowed = list(dict.fromkeys(self.allowed + self.sys))
        self.watch_services = parse_units(get("TG_WATCH_SERVICES"))
        self.service_no_restart = parse_units(get("TG_SERVICE_NO_RESTART"))
        legacy = get("TG_CHAT_ID").strip()
        if legacy and legacy not in self.allowed:
            self.allowed.append(legacy)
        self.default_chat = get("TG_DEFAULT_CHAT").strip() or legacy
        if not self.default_chat and self.allowed:
            self.default_chat = self.allowed[0]
        self.host = get("TG_HOST", "127.0.0.1")
        self.port = int(get("TG_PORT", "8765") or 8765)
        self.api_root = get("TG_API_ROOT").strip().rstrip("/")
        self.api_id = get("TG_API_ID").strip()
        self.api_hash = get("TG_API_HASH").strip()
        self.local_api_port = int(get("TG_LOCAL_API_PORT", str(LOCAL_API_PORT)) or LOCAL_API_PORT)
        self.local_api_dir = os.path.abspath(os.path.expanduser(
            get("TG_LOCAL_API_DIR") or "/var/lib/telegram-bot-api"))
        self.local_api_src = os.path.abspath(os.path.expanduser(
            get("TG_LOCAL_API_SRC") or LOCAL_API_SRC))
        try:
            self.local_api_jobs = max(1, min(int(get("TG_LOCAL_API_JOBS", "1") or 1),
                                            os.cpu_count() or 1))
        except (TypeError, ValueError):
            self.local_api_jobs = 1
        self.is_local_api = bool(self.api_root) and "api.telegram.org" not in self.api_root
        default_download_mb = 2000 if self.is_local_api else 20
        default_upload_mb = 2000 if self.is_local_api else 50
        try:
            self.max_download = max(1, int(get("TG_MAX_DOWNLOAD_MB", str(default_download_mb)))) * 1024 * 1024
        except ValueError:
            self.max_download = default_download_mb * 1024 * 1024
        try:
            self.max_upload = max(1, int(get("TG_MAX_UPLOAD_MB", str(default_upload_mb)))) * 1024 * 1024
        except ValueError:
            self.max_upload = default_upload_mb * 1024 * 1024
        self.history = int(get("TG_HISTORY", "300") or 300)
        self.poll_timeout = int(get("TG_POLL_TIMEOUT", "30") or 30)
        # Root shell from Telegram: disabled unless explicitly turned on.
        self.shell_enabled = parse_bool(shell_get("TG_SHELL_ENABLED", "0"))
        self.shell_idle = parse_seconds(
            shell_get("TG_SHELL_IDLE_SECONDS", "600"), 600, 60, 86400)
        self.shell_max = max(
            self.shell_idle,
            parse_seconds(shell_get("TG_SHELL_MAX_SECONDS", "1800"), 1800, 60, 604800),
        )
        self.shell_timeout = parse_seconds(
            shell_get("TG_SHELL_TIMEOUT", "120"), 120, 5, 3600)
        # AI mode: sys chats can talk to the pi coding harness from Telegram.
        self.ai_enabled = parse_bool(shell_get("TG_AI_ENABLED", "1"))
        self.pi_bin = get("TG_PI_BIN", "").strip()
        self.ai_provider = get("TG_AI_PROVIDER", "").strip()
        self.ai_model = get("TG_AI_MODEL", "").strip()
        self.state_dir = os.path.join(self.home, "state")
        self.files_dir = os.path.abspath(os.path.expanduser(
            get("TG_FILES_DIR") or os.path.join(self.home, "files")))
        self.watcher_log = os.path.abspath(os.path.expanduser(
            get("TG_WATCHER_LOG") or os.path.join(self.home, "watcher", "logs", "watcher.log")))

    def ensure(self):
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.files_dir, exist_ok=True)
        if not os.path.exists(env_file(self.home)):
            write_env(self.home, {})

    def is_allowed(self, cid):
        return str(cid) in self.allowed

    @property
    def log_path(self):
        return os.path.join(self.state_dir, "daemon.log")

    @property
    def pid_path(self):
        return os.path.join(self.state_dir, "daemon.pid")


def persist_permissions(cfg, allowed, sys_ids, mang_ids, default_chat):
    """Write the permission lists / default chat back into .env."""
    mang_ids = set(mang_ids)
    sys_ids = set(sys_ids) | mang_ids
    allowed = set(allowed) | sys_ids
    updates = {
        "TG_ALLOWED_CHAT_IDS": ",".join(sorted(allowed, key=id_sort_key)),
        "TG_SYS_CHAT_IDS": ",".join(sorted(sys_ids, key=id_sort_key)),
        "TG_MANG_CHAT_IDS": ",".join(sorted(mang_ids, key=id_sort_key)),
    }
    if default_chat:
        updates["TG_DEFAULT_CHAT"] = default_chat
    write_env(cfg.home, updates)
    return updates


def migrate_legacy(cfg):
    """One-time upgrade from the single-chat layout.

      * TG_CHAT_ID                    -> TG_ALLOWED_CHAT_IDS + TG_DEFAULT_CHAT
      * state/history.jsonl           -> state/<chatid>.jsonl (old kept as .bak)
    """
    disk = read_env(cfg.home)
    legacy = (disk.get("TG_CHAT_ID") or "").strip()
    allowed = parse_ids(disk.get("TG_ALLOWED_CHAT_IDS"))

    if legacy and not allowed:
        write_env(
            cfg.home,
            {
                "TG_ALLOWED_CHAT_IDS": legacy,
                "TG_DEFAULT_CHAT": disk.get("TG_DEFAULT_CHAT") or legacy,
                "TG_CHAT_ID": None,
            },
        )
        allowed = [legacy]
        note = f"migrated chat {legacy} into TG_ALLOWED_CHAT_IDS"

    default = (disk.get("TG_DEFAULT_CHAT") or "").strip() or (allowed[0] if allowed else "")

    old = os.path.join(cfg.state_dir, "history.jsonl")
    if default and os.path.exists(old):
        dest = os.path.join(cfg.state_dir, f"{default}.jsonl")
        if not os.path.exists(dest):
            os.makedirs(cfg.state_dir, exist_ok=True)
            try:
                with open(old, encoding="utf-8") as src, open(dest, "w", encoding="utf-8") as out:
                    for line in src:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        rec.setdefault("chat_id", default)
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                os.replace(old, old + ".bak")
                return f"migrated state/history.jsonl -> state/{default}.jsonl (old kept as history.jsonl.bak)"
            except OSError:
                pass
    return None


def log(cfg, message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        with open(cfg.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Telegram Bot API
# --------------------------------------------------------------------------


class TelegramError(Exception):
    pass


class DownloadCancelled(Exception):
    """An in-flight download was stopped by /cancel_upload."""


class Telegram:
    def __init__(self, cfg):
        self.cfg = cfg

    def call(self, method, params=None, timeout=45):
        return self._post(
            method, urllib.parse.urlencode(params or {}).encode(), timeout=timeout
        )

    def _post(self, method, data, headers=None, timeout=45):
        if not self.cfg.token:
            raise TelegramError("no bot token configured yet")
        url = api_url(self.cfg, method)
        hdrs = {"User-Agent": "tgmessage/1.0"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except Exception:
                raise TelegramError(f"HTTP {exc.code} from Telegram") from None
        except urllib.error.URLError as exc:
            raise TelegramError(f"network problem: {exc.reason}") from None
        except (TimeoutError, socket.timeout):
            raise TelegramError("network timeout") from None

        if not payload.get("ok"):
            code = payload.get("error_code", 0)
            desc = payload.get("description", "unknown error")
            if code == 401:
                desc += "  <- token rejected. Re-copy it from BotFather and run: tgmessage --token '<new token>'"
            elif code == 409:
                desc += "  <- another process is polling this bot"
            raise TelegramError(desc)
        return payload.get("result")

    def get_me(self):
        return self.call("getMe", timeout=15)

    def get_chat(self, chat_id):
        return self.call("getChat", {"chat_id": chat_id}, timeout=15)

    def get_updates(self, offset):
        return self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": self.cfg.poll_timeout,
                "allowed_updates": json.dumps(
                    ["message", "edited_message", "callback_query"]),
            },
            timeout=self.cfg.poll_timeout + 20,
        )

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        params = {"chat_id": chat_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self.call("sendMessage", params, timeout=30)

    def edit_message_text(self, chat_id, message_id, text, parse_mode=None,
                          reply_markup=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self.call("editMessageText", params, timeout=30)

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        params = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text[:200]
            params["show_alert"] = "true" if show_alert else "false"
        return self.call("answerCallbackQuery", params, timeout=15)

    def send_chat_action(self, chat_id, action="typing"):
        return self.call(
            "sendChatAction", {"chat_id": chat_id, "action": action}, timeout=15)

    def send_document(self, chat_id, path, caption=None):
        """Upload a file with sendDocument (multipart/form-data, stdlib only)."""
        filename = os.path.basename(path)
        if getattr(self.cfg, "is_local_api", False):
            # A local Bot API server accepts a local path instead of loading the
            # whole file into memory, so large uploads work too.
            params = {"chat_id": str(chat_id), "document": "file://" + urllib.parse.quote(path)}
            if caption:
                params["caption"] = caption[:1024]
            return self.call("sendDocument", params, timeout=3600)
        # A CR/LF or quote in the name would break the part headers.
        safe_name = filename.replace("\r", " ").replace("\n", " ").replace('"', "'")
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            blob = fh.read()

        boundary = "----tgmessage" + uuid.uuid4().hex

        def text_part(name, value):
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")

        parts = [text_part("chat_id", str(chat_id))]
        if caption:
            parts.append(text_part("caption", caption[:1024]))
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="document"; filename="{safe_name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(blob)
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        return self._post(
            "sendDocument",
            body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=300,
        )

    def get_file(self, file_id):
        """Ask Telegram for the server path of a file (getFile)."""
        # A local server blocks getFile until it has fetched the whole file.
        # Large files on a slow link can take much longer than the old 5 min.
        timeout = LOCAL_GETFILE_TIMEOUT if getattr(self.cfg, "is_local_api", False) else 30
        return self.call("getFile", {"file_id": file_id}, timeout=timeout)

    @staticmethod
    def _copy_local_file(src, dest_path, progress=None, cancelled=None):
        """Copy a file already stored by a local Bot API server."""
        if not os.path.isfile(src):
            raise TelegramError(f"local Bot API file not found: {src}")
        total = os.path.getsize(src)
        if cancelled and cancelled():
            raise DownloadCancelled()
        # Same filesystem: hard-link instead of copying gigabytes of data.
        try:
            os.link(src, dest_path)
        except OSError:
            pass
        else:
            if progress:
                try:
                    progress(total, total)
                except Exception:  # noqa: BLE001
                    pass
            return
        done = 0
        with open(src, "rb") as inp, open(dest_path, "wb") as out:
            while True:
                if cancelled and cancelled():
                    raise DownloadCancelled()
                chunk = inp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    try:
                        progress(done, total)
                    except Exception:  # noqa: BLE001 - never abort a download
                        pass
        if progress:
            try:
                progress(done, total)
            except Exception:  # noqa: BLE001
                pass

    def download_file(self, server_path, dest_path, progress=None, cancelled=None):
        """Download a file from Telegram's servers to dest_path (stdlib only).

        progress(done, total), when given, is called after every chunk.
        total is 0 when the server does not report a Content-Length.
        cancelled(), when given, is checked before every chunk; returning True
        aborts the transfer with DownloadCancelled.

        A local Bot API server started with --local returns an absolute path
        instead of a relative download path; those files are copied from disk
        directly, which is what lifts the cloud API's 20 MB download limit.
        """
        if not self.cfg.token:
            raise TelegramError("no bot token configured yet")
        if os.path.isabs(server_path):
            self._copy_local_file(server_path, dest_path, progress, cancelled)
            return
        url = "{root}/file/bot{token}/{path}".format(
            root=api_root(self.cfg),
            token=self.cfg.token,
            path=urllib.parse.quote(server_path),
        )
        req = urllib.request.Request(url, headers={"User-Agent": "tgmessage/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp, \
                    open(dest_path, "wb") as out:
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    total = 0
                done = 0
                while True:
                    if cancelled and cancelled():
                        raise DownloadCancelled()
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        try:
                            progress(done, total)
                        except Exception:  # noqa: BLE001 - never abort a download
                            pass
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"download failed: HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise TelegramError(f"download failed: {exc.reason}") from None
        except (TimeoutError, socket.timeout):
            raise TelegramError("download timed out") from None


# --------------------------------------------------------------------------
# stores: per-chat history, pending requests, chat metadata
# --------------------------------------------------------------------------


class Store:
    """Append-only JSON-lines log for one chat."""

    def __init__(self, path, limit):
        self.path = path
        self.limit = limit
        self.lock = threading.Lock()
        self.items = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()[-self.limit :]
            for line in lines:
                line = line.strip()
                if line:
                    try:
                        self.items.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass

    def append(self, rec):
        with self.lock:
            self.items.append(rec)
            self.items = self.items[-self.limit :]
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def tail(self, n):
        with self.lock:
            return list(self.items[-n:])

    def count(self):
        with self.lock:
            return len(self.items)


class History:
    """A Store per chat id, files live at state/<chatid>.jsonl."""

    def __init__(self, state_dir, limit):
        self.dir = state_dir
        self.limit = limit
        self.lock = threading.Lock()
        self.stores = {}

    def path(self, cid):
        return os.path.join(self.dir, f"{cid}.jsonl")

    def _store(self, cid):
        cid = str(cid)
        with self.lock:
            store = self.stores.get(cid)
            if store is None:
                os.makedirs(self.dir, exist_ok=True)
                store = Store(self.path(cid), self.limit)
                self.stores[cid] = store
            return store

    def append(self, cid, rec):
        self._store(cid).append(rec)

    def tail(self, cid, n):
        return self._store(cid).tail(n)

    def count(self, cid):
        return self._store(cid).count()

    def chats(self):
        out = []
        if os.path.isdir(self.dir):
            for name in os.listdir(self.dir):
                if name.endswith(".jsonl") and not name.startswith("history."):
                    out.append(name[: -len(".jsonl")])
        return out


class Pending:
    """Chats that messaged the bot but are not allowed yet."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.items = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self.items = {str(k): v for k, v in data.items()}
        except (OSError, ValueError):
            self.items = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.items, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def add(self, cid, meta, text):
        cid = str(cid)
        now = time.time()
        with self.lock:
            entry = self.items.get(cid) or {"chat_id": cid, "first_ts": now, "count": 0}
            for key in ("name", "username", "type"):
                if meta.get(key):
                    entry[key] = meta[key]
            entry["count"] = entry.get("count", 0) + 1
            entry["last_ts"] = now
            entry["last_text"] = text
            self.items[cid] = entry
            self._save()

    def remove(self, cid):
        cid = str(cid)
        with self.lock:
            if cid in self.items:
                del self.items[cid]
                self._save()

    def all(self):
        with self.lock:
            return sorted(self.items.values(), key=lambda e: e.get("last_ts") or 0, reverse=True)


class Registry:
    """Names and counts for every chat we have ever seen, allowed or not."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.items = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self.items = {str(k): v for k, v in data.items()}
        except (OSError, ValueError):
            self.items = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.items, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def touch(self, cid, meta, direction, stamp=True):
        cid = str(cid)
        now = time.time()
        with self.lock:
            entry = self.items.get(cid) or {"chat_id": cid, "first_ts": now}
            for key in ("name", "username", "type"):
                if meta.get(key):
                    entry[key] = meta[key]
            if stamp:
                entry["last_ts"] = now
            elif "last_ts" not in entry:
                entry["last_ts"] = now
            if direction == "in":
                entry["in"] = entry.get("in", 0) + 1
            elif direction == "out":
                entry["out"] = entry.get("out", 0) + 1
            self.items[cid] = entry
            self._save()

    def get(self, cid):
        with self.lock:
            return dict(self.items.get(str(cid)) or {})

    def all(self):
        with self.lock:
            return {k: dict(v) for k, v in self.items.items()}


def message_text(msg):
    for key in ("text", "caption"):
        if msg.get(key):
            return msg[key]
    for key, label in (
        ("photo", "photo"),
        ("voice", "voice note"),
        ("audio", "audio"),
        ("video", "video"),
        ("document", "file"),
        ("sticker", "sticker"),
        ("location", "location"),
        ("contact", "contact"),
        ("poll", "poll"),
    ):
        if key in msg:
            info = msg.get(key)
            if isinstance(info, dict) and info.get("file_name"):
                name = info["file_name"]
                size = human_size(info["file_size"]) if info.get("file_size") else ""
                return f"[{label}] {name}" + (f" ({size})" if size else "")
            return f"[{label}]"
    return "[message]"


# --------------------------------------------------------------------------
# /sh-on - root shell from a private Telegram chat (sys access only)
# --------------------------------------------------------------------------

SHELL_MAX_COMMAND = 4000           # Telegram text messages are 4096 max anyway
SHELL_MAX_OUTPUT = 16 * 1024       # bytes kept for the Telegram reply
SHELL_MAX_READ = 64 * 1024 * 1024  # total pipe read before a runaway is killed
SHELL_CHUNK = 3500                 # UTF-16 units per Telegram reply (limit 4096)
SHELL_MAX_CONCURRENT = 2           # total commands running across all chats
SHELL_RATE_LIMIT = 20              # commands per chat ...
SHELL_RATE_WINDOW = 60.0           # ... per this many seconds
SHELL_STATE_FILE = "shell.json"    # active sessions survive a daemon restart

_ANSI_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"           # CSI
    r"|\x1b\][^\x07]*(?:\x07|\x1b\\)"    # OSC
    r"|\x1b[@-_]"                        # other two-byte escapes
)


def current_boot_id():
    """Identify this boot so shell sessions survive service, not machine, restarts."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def clean_terminal_output(data):
    """Decode command output and remove control sequences Telegram cannot show."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    text = _ANSI_ESCAPE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return text.strip("\n")


def telegram_code_block(text):
    """Wrap plain text in Telegram's <pre> code block, escaping HTML."""
    text = str(text or "")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<pre>{text}</pre>"


def split_message(text, limit=SHELL_CHUNK):
    """Split long shell output into Telegram-sized chunks, preferring newlines.

    Telegram counts UTF-16 code units, so an emoji can use two of the 4096
    allowed units.  Splitting by units keeps every chunk valid.
    """
    text = str(text or "")
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return [text]
    chunks = []
    start = 0
    total = len(text)
    while start < total:
        end = start
        units = 0
        while end < total:
            width = 2 if ord(text[end]) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            end += 1
        if end == start:  # a single over-wide character, should not happen
            end += 1
        cut = text.rfind("\n", start, end)
        if cut >= start + (end - start) // 2:
            end = cut
        chunk = text[start:end].rstrip("\n")
        if chunk:
            chunks.append(chunk)
        start = end
        while start < total and text[start] == "\n":
            start += 1
    return chunks or [text]


def kill_process_group(proc):
    """Kill a command and every child it started, never raise."""
    try:
        # start_new_session=True makes the child its own process group leader.
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def run_shell_command(command, cwd, timeout, max_output=SHELL_MAX_OUTPUT,
                      max_total=SHELL_MAX_READ, on_start=None, cwd_file=None):
    """Run one command with a time limit and bounded output.

    Returns a dict with text/exit/timed_out/truncated/flooded.  Only
    `max_output` bytes are kept for the reply, but the pipe keeps draining so
    a large-but-legitimate command (apt upgrade) is not killed halfway.  A
    command that reads more than `max_total` bytes is treated as a runaway and
    killed.  stdin is /dev/null, so interactive programs fail fast.
    """
    home = os.path.expanduser("~")
    env = {
        # Fixed PATH: the shell must not accidentally pick up a user-writable
        # directory from whatever environment the daemon inherited.
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": home or cwd,
        "USER": getpass.getuser(),
        "LOGNAME": getpass.getuser(),
        "SHELL": "/bin/bash",
        "TERM": "dumb",
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "SYSTEMD_PAGER": "cat",
        "DEBIAN_FRONTEND": "noninteractive",
        "NO_COLOR": "1",
    }
    if os.environ.get("LC_ALL"):
        env["LC_ALL"] = os.environ["LC_ALL"]

    script = command
    if cwd_file:
        # The EXIT trap records the final directory even for `cd`, `exit` or a
        # failing command.  A clean, non-login shell keeps it predictable.
        env["TG_SHELL_CWD_FILE"] = cwd_file
        script = (
            "trap 'printf \"%s\" \"$PWD\" > \"$TG_SHELL_CWD_FILE\"' EXIT\n"
            + command
        )

    try:
        proc = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc", "-c", script],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return {
            "text": f"could not start the shell: {exc}",
            "exit": None,
            "timed_out": False,
            "truncated": False,
            "flooded": False,
        }

    if on_start is not None:
        try:
            on_start(proc)
        except Exception:  # noqa: BLE001 - a bad callback must not stop the command
            pass

    out = bytearray()
    timed_out = False
    truncated = False
    flooded = False
    total_read = 0
    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    try:
        while True:
            if proc.poll() is not None:
                # The shell exited: drain what is already in the pipe, then stop.
                while True:
                    try:
                        data = os.read(fd, 65536)
                    except BlockingIOError:
                        break
                    if not data:
                        break
                    total_read += len(data)
                    room = max_output - len(out)
                    if room > 0:
                        out += data[:room]
                    if len(data) > room:
                        truncated = True
                    if total_read > max_total:
                        flooded = True
                        break
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                ready, _, _ = select.select([fd], [], [], min(0.2, remaining))
            except InterruptedError:
                continue
            if not ready:
                continue
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if not data:
                break
            total_read += len(data)
            room = max_output - len(out)
            if room > 0:
                out += data[:room]
            if len(data) > room:
                truncated = True
            if total_read > max_total:
                flooded = True
                break
    finally:
        if timed_out or flooded:
            kill_process_group(proc)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        try:
            proc.stdout.close()
        except OSError:
            pass

    text = clean_terminal_output(bytes(out)) or "(no output)"
    if truncated:
        text += f"\n[output truncated at {human_size(max_output)}]"
    if flooded:
        text += f"\n[output flood: {human_size(max_total)} read - command killed]"
    if timed_out:
        text += f"\n[timed out after {timeout}s - command killed]"
    return {
        "text": text,
        "exit": proc.returncode,
        "timed_out": timed_out,
        "truncated": truncated,
        "flooded": flooded,
    }


# --------------------------------------------------------------------------
# working directory shared by /sh-on and /ai
# --------------------------------------------------------------------------

WORKDIR_FILE = "workspace.json"


def default_workdir(cfg):
    home = os.path.expanduser("~")
    for path in (home, cfg.home, "/"):
        if path and os.path.isdir(path):
            return os.path.abspath(path)
    return "/"


def read_workdir(cfg):
    """Return the saved cd, falling back to the service home."""
    path = os.path.join(cfg.state_dir, WORKDIR_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        cwd = str((data or {}).get("cwd") or "").strip()
        if cwd and os.path.isdir(cwd):
            return os.path.abspath(cwd)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return default_workdir(cfg)


def write_workdir(cfg, cwd):
    """Remember a cd for the next /sh-on and /ai, even after /sh-off."""
    if not cwd or not os.path.isdir(cwd):
        return False
    path = os.path.join(cfg.state_dir, WORKDIR_FILE)
    data = {"cwd": os.path.abspath(cwd), "updated": time.time()}
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def find_pi(cfg):
    """Find the pi executable even when systemd has a small PATH."""
    candidates = []
    configured = (getattr(cfg, "pi_bin", "") or "").strip()
    if configured:
        expanded = os.path.expanduser(configured)
        candidates.append(expanded)
        if not os.path.isabs(expanded):
            found = shutil.which(expanded)
            if found:
                candidates.append(found)
    found = shutil.which("pi")
    if found:
        candidates.append(found)
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(home, ".pi", "agent", "bin", "pi"),
        "/root/.pi/agent/bin/pi",
        "/usr/local/bin/pi",
    ]
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def pi_missing_text(cfg):
    """Clear error for /ai when the pi executable is not available."""
    configured = (getattr(cfg, "pi_bin", "") or "").strip()
    if configured:
        first = (
            f"AI mode unavailable: TG_PI_BIN points to {configured}, "
            "but that is not an executable file."
        )
    else:
        first = "AI mode unavailable: pi is not installed or not on PATH."
    return "\n".join([
        first,
        "",
        "Install pi on the server:",
        "  curl -fsSL https://pi.dev/install.sh | sh",
        "",
        "Or set TG_PI_BIN in the .env file to the pi executable,",
        "then restart tgmessage and send /ai again.",
    ])


# --------------------------------------------------------------------------
# /ai - talk to the pi coding harness from a private Telegram chat
# --------------------------------------------------------------------------

AI_MAX_LINE = 4 * 1024 * 1024      # drop oversized protocol records
AI_COMMAND_TIMEOUT = 20
AI_START_TIMEOUT = 45
AI_MODEL_PAGE = 8
AI_SESSION_PAGE = 8
AI_SESSION_LIMIT = 120
AI_SESSION_HEAD = 512 * 1024
AI_SESSION_TAIL = 256 * 1024


def _session_message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "")
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


def format_duration(seconds):
    """Small human duration: 42s, 3m 10s, 1h 4m."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def pi_session_context(path, max_chars=12000, max_messages=24):
    """Recent conversation text for a /btw side question."""
    if not path or not os.path.isfile(path):
        return ""
    messages = []
    summary = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                kind = record.get("type")
                if kind == "compaction":
                    text = str(record.get("summary") or "").strip()
                    if text:
                        summary = text
                    continue
                if kind != "message":
                    continue
                message = record.get("message") or {}
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _session_message_text(message.get("content")).strip()
                if role == "assistant":
                    text = re.sub(
                        r"<tg>\s*.*?\s*</tg>", "", text,
                        flags=re.IGNORECASE | re.DOTALL).strip()
                if not text:
                    continue
                if len(text) > 4000:
                    text = text[:4000].rstrip() + "..."
                messages.append((role, text))
                if len(messages) > max_messages:
                    messages = messages[-max_messages:]
    except OSError:
        return ""

    chunks = []
    if summary:
        if len(summary) > 6000:
            summary = summary[:6000].rstrip() + "..."
        chunks.append("Summary of earlier conversation:\n" + summary)
    for role, text in messages:
        chunks.append(f"{'User' if role == 'user' else 'Assistant'}: {text}")
    context = "\n\n".join(chunks).strip()
    if len(context) > max_chars:
        context = "...(older context trimmed)\n\n" + context[-max_chars:]
    return context


PI_BTW_PACKAGE = "@narumitw/pi-btw"


def pi_btw_installed():
    """Check the normal pi package roots for @narumitw/pi-btw."""
    home = os.path.expanduser("~")
    agent_dir = (os.environ.get("PI_CODING_AGENT_DIR") or "").strip()
    if not agent_dir:
        agent_dir = os.path.join(home, ".pi", "agent")
    agent_dir = os.path.expanduser(agent_dir)
    roots = [
        os.path.join(agent_dir, "npm", "node_modules"),
        os.path.join(home, ".pi", "agent", "npm", "node_modules"),
        "/root/.pi/agent/npm/node_modules",
    ]
    for root in roots:
        if os.path.isdir(os.path.join(root, "@narumitw", "pi-btw")):
            return True
    settings_path = os.path.join(agent_dir, "settings.json")
    try:
        with open(settings_path, encoding="utf-8") as fh:
            packages = (json.load(fh) or {}).get("packages") or []
    except (OSError, ValueError, json.JSONDecodeError):
        packages = []
    return any(PI_BTW_PACKAGE in str(item) for item in packages)


def btw_missing_text():
    return "\n".join([
        "/btw is unavailable: @narumitw/pi-btw is not installed.",
        "",
        "Install it on the server:",
        "  pi install npm:@narumitw/pi-btw",
        "",
        "Then send /btw again.",
    ])


def btw_system_prompt_path():
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "btw-system-prompt.md")


def build_btw_prompt(question, context):
    return "\n".join([
        "Answer this side question without modifying the main conversation.",
        "",
        "<side_question>",
        question.strip(),
        "</side_question>",
        "",
        "<conversation_context>",
        context.strip() or "No prior conversation context was available.",
        "</conversation_context>",
    ])


def run_pi_side_question(pi, cwd, provider, model, thinking, question, context,
                         timeout=180):
    """Run a throwaway pi side question without touching the main session.

    pi-btw only opens its side UI in TUI mode, so this uses the same
    side-question system prompt and context in a separate print-mode process.
    """
    system_path = btw_system_prompt_path()
    if not os.path.isfile(system_path):
        return "", f"missing {system_path}"
    args = [
        pi, "-p", "--no-session", "-nt", "-ne", "-ns", "-np", "-nc",
        "--system-prompt", system_path,
    ]
    if provider and model:
        args += ["--provider", provider]
    if model:
        args += ["--model", model]
    if thinking:
        args += ["--thinking", thinking]
    args += ["--", build_btw_prompt(question, context)]

    env = os.environ.copy()
    pi_dir = os.path.dirname(pi)
    path = env.get("PATH") or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    if pi_dir and pi_dir not in path.split(os.pathsep):
        path = pi_dir + os.pathsep + path
    env["PATH"] = path
    env.setdefault("HOME", os.path.expanduser("~"))
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        return "", f"could not start pi: {exc}"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
        try:
            proc.communicate(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        return "", f"side question timed out after {int(timeout)}s"
    answer = (out or "").strip()
    if proc.returncode != 0:
        detail = (err or out or "").strip()
        if len(detail) > 800:
            detail = detail[-800:]
        return "", f"pi exited with {proc.returncode}: {detail or 'no details'}"
    if not answer:
        detail = (err or "").strip()
        if len(detail) > 400:
            detail = detail[-400:]
        return "", "pi returned no answer" + (f": {detail}" if detail else "")
    return answer, None


AI_LIVE_DIR = "live"
AI_LIVE_STALE = 15


def live_state_dir(cfg):
    return os.path.join(cfg.state_dir, AI_LIVE_DIR)


def read_live_states(cfg):
    """Read fresh heartbeat files written by tg-live.ts."""
    states = {}
    directory = live_state_dir(cfg)
    try:
        names = os.listdir(directory)
    except OSError:
        return states
    now = time.time()
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            pid = int(data.get("pid") or 0)
            updated = float(data.get("updatedAt") or 0)
        except (TypeError, ValueError):
            continue
        if updated > now * 10:
            updated /= 1000.0
        if pid <= 0 or now - updated > AI_LIVE_STALE:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        data["pid"] = pid
        data["updatedAt"] = updated
        states[pid] = data
    return states


def _process_elapsed(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
        end = raw.rfind(")")
        if end < 0:
            return None
        fields = raw[end + 2:].split()
        if not fields or fields[0] == "Z":
            return None
        start_ticks = int(fields[19])
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime = float(fh.read().split()[0])
        hz = float(os.sysconf("SC_CLK_TCK"))
        return max(0.0, uptime - start_ticks / hz)
    except (OSError, ValueError, IndexError):
        return None


def find_live_pi_processes(cwd):
    """Find pi processes whose working directory is exactly cwd."""
    target = os.path.realpath(cwd)
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        if not argv:
            continue
        base = os.path.basename(argv[0])
        if base not in ("pi", "pi.js") and not base.startswith("pi-"):
            if not any(os.path.basename(arg) in ("pi", "pi.js") for arg in argv[:2]):
                continue
        if "--no-session" in argv or "-p" in argv or "--print" in argv:
            continue
        try:
            proc_cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue
        if proc_cwd != target:
            continue
        elapsed = _process_elapsed(pid)
        if elapsed is None:
            continue
        found.append({"pid": pid, "elapsed": elapsed, "argv": argv})
    found.sort(key=lambda item: item["elapsed"], reverse=True)
    return found



def _one_line(text, limit):
    text = " ".join(str(text or "").split())
    return text[:limit]


def _short_session_id(path):
    base = os.path.basename(str(path or ""))
    if base.endswith(".jsonl"):
        base = base[:-6]
    if "_" in base:
        base = base.rsplit("_", 1)[-1]
    return ("session " + base[:8]) if base else "new session"


def _parse_session_blob(blob, meta):
    """Pull the latest name and an early user message out of one chunk."""
    for raw in blob.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, json.JSONDecodeError):
            continue
        kind = record.get("type")
        if kind == "session_info":
            meta["name"] = str(record.get("name") or "")
            continue
        if not meta["first_user"] and kind == "message":
            message = record.get("message") or {}
            if message.get("role") == "user":
                text = _session_message_text(message.get("content"))
                if text.strip():
                    meta["first_user"] = _one_line(text, 200)


def read_pi_session(path, mtime, want_cwd=None):
    """Read just enough of a pi session file to show it in the picker."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    meta = {"name": "", "first_user": ""}
    try:
        with open(path, "rb") as fh:
            header_raw = fh.readline(1024 * 1024)
            try:
                header = json.loads(header_raw.decode("utf-8", "replace"))
            except (ValueError, json.JSONDecodeError):
                return None
            if not isinstance(header, dict) or header.get("type") != "session":
                return None
            session_cwd = str(header.get("cwd") or "")
            if want_cwd and (
                not session_cwd
                or os.path.realpath(session_cwd) != os.path.realpath(want_cwd)
            ):
                return None
            _parse_session_blob(fh.read(AI_SESSION_HEAD), meta)
            start = max(0, size - AI_SESSION_TAIL)
            if start > fh.tell():
                fh.seek(start)
                _parse_session_blob(fh.read(), meta)
    except OSError:
        return None

    session_id = str(header.get("id") or "")
    title = meta["name"] or meta["first_user"] or session_id[:8] or "empty session"
    return {
        "path": os.path.abspath(path),
        "id": session_id,
        "cwd": session_cwd,
        "created": str(header.get("timestamp") or ""),
        "name": meta["name"],
        "title": _one_line(title, 100),
        "first_user": meta["first_user"],
        "mtime": float(mtime or 0),
    }


def discover_pi_sessions(roots, limit=AI_SESSION_LIMIT, cwd=None):
    """Return the newest pi session files found under the given roots."""
    found = []
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, filename)
                real = os.path.realpath(path)
                if real in seen:
                    continue
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                seen.add(real)
                found.append((stat.st_mtime, path))
    found.sort(reverse=True)
    sessions = []
    for mtime, path in found:
        record = read_pi_session(path, mtime, want_cwd=cwd)
        if record:
            sessions.append(record)
            if len(sessions) >= limit:
                break
    return sessions


def _protocol_lines(stream, limit=AI_MAX_LINE):
    """Yield complete JSONL lines, dropping records larger than limit."""
    while True:
        line = stream.readline(limit)
        if not line:
            return
        if not line.endswith(b"\n"):
            # An oversized record. Drain it so the next record starts cleanly.
            while True:
                rest = stream.readline(limit)
                if not rest:
                    return
                if rest.endswith(b"\n"):
                    break
            continue
        yield line.decode("utf-8", "replace")


TG_CONTROL_RE = re.compile(r"<tg>\s*(.*?)\s*</tg>", re.IGNORECASE | re.DOTALL)
TG_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
AI_CONTROL_LIMIT = 20
AI_BUTTON_LIMIT = 300


def tgmessage_prompt_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "tgmessage.md")


def ai_rules_paths(cfg, cid):
    base = os.path.join(cfg.state_dir, f"ai-agent-{cid}")
    return base + ".json", base + ".md"


def load_ai_rules(cfg, cid):
    json_path, _md_path = ai_rules_paths(cfg, cid)
    try:
        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def save_ai_rules(cfg, cid, rules):
    rules = [str(rule).strip() for rule in rules if str(rule).strip()]
    json_path, md_path = ai_rules_paths(cfg, cid)
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        tmp = json_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rules, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, json_path)
    except OSError:
        return False
    if not rules:
        try:
            os.unlink(md_path)
        except OSError:
            pass
        return True
    try:
        tmp = md_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("# Persistent AI agent rules for this Telegram chat\n\n")
            for rule in rules:
                fh.write(f"- {rule}\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, md_path)
        return True
    except OSError:
        return False


def ensure_ai_rules_file(cfg, cid):
    rules = load_ai_rules(cfg, cid)
    _json_path, md_path = ai_rules_paths(cfg, cid)
    if not rules:
        try:
            os.unlink(md_path)
        except OSError:
            pass
        return None
    if not os.path.isfile(md_path):
        save_ai_rules(cfg, cid, rules)
    return md_path if os.path.isfile(md_path) else None


def _plain_html(text):
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def parse_tg_controls(text):
    """Split assistant text into visible text and Telegram control actions."""
    visible = []
    actions = []
    errors = 0
    position = 0
    text = text or ""
    fenced = [(match.start(), match.end()) for match in TG_FENCE_RE.finditer(text)]
    for match in TG_CONTROL_RE.finditer(text):
        if any(start <= match.start() < end for start, end in fenced):
            # A control block inside a code fence is an example, not a command.
            continue
        visible.append(text[position:match.start()])
        position = match.end()
        payload = match.group(1).strip()
        try:
            obj = json.loads(payload)
        except (ValueError, json.JSONDecodeError):
            errors += 1
            continue
        if isinstance(obj, dict):
            raw = obj.get("actions") if isinstance(obj.get("actions"), list) else [obj]
        elif isinstance(obj, list):
            raw = obj
        else:
            errors += 1
            continue
        for item in raw:
            if not isinstance(item, dict) or not item.get("type"):
                errors += 1
                continue
            if len(actions) >= AI_CONTROL_LIMIT:
                errors += 1
                break
            actions.append(item)
    visible.append(text[position:])
    return "".join(visible).strip(), actions, errors


class AiSession:
    """One long-lived `pi --mode rpc` process for one Telegram chat."""

    def __init__(self, daemon, cid, cwd):
        self.daemon = daemon
        self.cfg = daemon.cfg
        self.cid = cid
        self.cwd = cwd
        self.started = time.time()
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = {}
        self.seq = 0
        self.closed = False
        self.dead = False
        self.busy = False
        self.thinking_shown = False
        self.extension_notice = False
        self.models = []
        self.model_page = 0
        self.model_message_id = None
        self.current_model = {}
        self.think_levels = []
        self.current_thinking = ""
        self.current_session_file = ""
        self.current_session_id = ""
        self.current_session_name = ""
        self.message_count = 0
        self.session_list = []
        self.session_page = 0
        self.session_message_id = None
        self.button_seq = 0
        self.buttons = {}
        self.text_seq = 0
        self.processed_text = set()
        self.visible_text_sent = False
        self.rules_paths = []
        self.proc = None

        pi = find_pi(self.cfg)
        if not pi:
            raise RuntimeError(pi_missing_text(self.cfg))
        self.pi = pi
        self._spawn()

    def _spawn(self):
        args = [
            self.pi,
            "--mode", "rpc",
        ]
        provider = (getattr(self.cfg, "ai_provider", "") or "").strip()
        model = (getattr(self.cfg, "ai_model", "") or "").strip()
        if model:
            if provider:
                args += ["--provider", provider]
            args += ["--model", model]

        default_rules = tgmessage_prompt_path()
        if os.path.isfile(default_rules):
            self.rules_paths.append(default_rules)
            args += ["--append-system-prompt", default_rules]
        else:
            log(self.cfg, f"ai: missing {default_rules} - Telegram rules not injected")
        custom_rules = ensure_ai_rules_file(self.cfg, self.cid)
        if custom_rules:
            self.rules_paths.append(custom_rules)
            args += ["--append-system-prompt", custom_rules]

        live_extension = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tg-live.ts")
        if os.path.isfile(live_extension):
            args += ["--extension", live_extension]
        else:
            log(self.cfg, f"ai: missing {live_extension} - /live subagent data off")

        env = os.environ.copy()
        env["TG_LIVE_STATE_DIR"] = live_state_dir(self.cfg)
        pi_dir = os.path.dirname(self.pi)
        path = env.get("PATH") or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        if pi_dir and pi_dir not in path.split(os.pathsep):
            path = pi_dir + os.pathsep + path
        env["PATH"] = path
        env.setdefault("HOME", os.path.expanduser("~"))
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"

        self.proc = subprocess.Popen(
            args,
            cwd=self.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()
        log(self.cfg, f"/ai started pi for chat {self.cid} in {self.cwd}")

    @property
    def alive(self):
        with self.lock:
            return not (self.closed or self.dead)

    @property
    def working(self):
        with self.lock:
            return self.busy and not self.closed and not self.dead

    def _write(self, payload):
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self.write_lock:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def command(self, cmd_type, timeout=AI_COMMAND_TIMEOUT, **fields):
        """Send one RPC command and wait for its matching response."""
        with self.lock:
            if self.closed or self.dead:
                return None, "AI mode is not running"
            self.seq += 1
            rid = f"tg{self.cid}-{self.seq}"
        item = {"event": threading.Event(), "response": None}
        with self.pending_lock:
            self.pending[rid] = item
        if not self._write({"id": rid, "type": cmd_type, **fields}):
            with self.pending_lock:
                self.pending.pop(rid, None)
            return None, "pi is not accepting input"
        if not item["event"].wait(timeout):
            with self.pending_lock:
                self.pending.pop(rid, None)
            return None, f"pi did not answer in {int(timeout)}s"
        response = item["response"] or {}
        if not response.get("success"):
            return None, response.get("error") or "pi command failed"
        return response.get("data"), None

    def prompt(self, text):
        with self.lock:
            busy = self.busy
            dead = self.closed or self.dead
        if dead:
            return None, "AI mode is not running"
        fields = {"message": text}
        if busy:
            fields["streamingBehavior"] = "followUp"
        return self.command("prompt", timeout=15, **fields)

    def apply_state(self, state):
        """Refresh model, thinking and session details from get_state."""
        if not state:
            return
        self.current_model = state.get("model") or self.current_model
        if state.get("thinkingLevel") is not None:
            self.current_thinking = str(state.get("thinkingLevel") or "")
        if state.get("sessionFile"):
            self.current_session_file = str(state["sessionFile"])
        if state.get("sessionId"):
            self.current_session_id = str(state["sessionId"])
        if "sessionName" in state:
            self.current_session_name = str(state.get("sessionName") or "")
        try:
            self.message_count = int(state.get("messageCount") or 0)
        except (TypeError, ValueError):
            self.message_count = 0

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            proc = self.proc
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                kill_process_group(proc)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        log(self.cfg, f"/ai stopped pi for chat {self.cid}")

    def _read_loop(self):
        try:
            for raw in _protocol_lines(self.proc.stdout):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._dispatch(record)
        except Exception as exc:  # noqa: BLE001 - the reader must not crash silently
            log(self.cfg, f"ai reader for {self.cid} failed: {exc!r}")
        finally:
            with self.lock:
                was_closed = self.closed
                self.dead = True
                self.busy = False
            with self.pending_lock:
                waiting = list(self.pending.values())
                self.pending.clear()
            for item in waiting:
                item["response"] = {"success": False, "error": "pi exited"}
                item["event"].set()
            if not was_closed:
                with self.lock:
                    notify = not self.closed
                if notify:
                    try:
                        self.daemon._record_out(
                            self.cid,
                            "AI stopped: the pi process exited.\n"
                            "Send /ai-exit, then /ai to start a new one.",
                        )
                    except Exception:  # noqa: BLE001
                        pass

    def _stderr_loop(self):
        count = 0
        try:
            for raw in self.proc.stderr:
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                count += 1
                if count <= 20 or count % 100 == 0:
                    log(self.cfg, f"ai[{self.cid}] stderr: {text[:500]}")
        except (OSError, ValueError):
            pass

    def _dispatch(self, record):
        kind = record.get("type")
        if kind == "response":
            rid = record.get("id")
            if not rid:
                return
            with self.pending_lock:
                item = self.pending.pop(rid, None)
            if item is not None:
                item["response"] = record
                item["event"].set()
            return
        if kind == "extension_ui_request":
            self._extension_ui(record)
            return
        if kind == "agent_start":
            with self.lock:
                self.busy = True
                self.thinking_shown = False
            self._show_thinking()
            return
        if kind == "message_start":
            message = record.get("message") or {}
            if message.get("role") == "assistant":
                self.text_seq += 1
                self.processed_text = set()
                self.visible_text_sent = False
            return
        if kind == "message_update":
            event = record.get("assistantMessageEvent") or {}
            event_type = event.get("type")
            if event_type in ("thinking_start", "toolcall_start"):
                self._show_thinking()
            elif event_type == "text_end":
                content = event.get("content")
                if isinstance(content, str):
                    key = (self.text_seq, event.get("contentIndex"))
                    if key not in self.processed_text:
                        self.processed_text.add(key)
                        self._handle_text(content)
            return
        if kind == "message_end":
            self._message_end(record.get("message") or {})
            return
        if kind == "agent_settled":
            with self.lock:
                self.busy = False

    def _extension_ui(self, record):
        method = record.get("method")
        if method in ("select", "confirm", "input", "editor"):
            self._write({
                "type": "extension_ui_response",
                "id": record.get("id"),
                "cancelled": True,
            })
            with self.lock:
                show = not self.extension_notice
                self.extension_notice = True
            if show:
                self.daemon._record_out(
                    self.cid,
                    "An extension asked for input. Dialogs are not available "
                    "in Telegram AI mode.",
                )
            return
        if method == "notify":
            message = str(record.get("message") or "").strip()
            if message:
                log(self.cfg, f"ai[{self.cid}] notify: {message[:500]}")

    def _show_thinking(self):
        with self.lock:
            if self.thinking_shown:
                return
            self.thinking_shown = True
        self.daemon._record_out(self.cid, "thinking....")

    def _message_end(self, message):
        if message.get("role") != "assistant":
            return
        for index, block in enumerate(message.get("content") or []):
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            key = (self.text_seq, index)
            if key in self.processed_text:
                continue
            self.processed_text.add(key)
            self._handle_text(text)
        if message.get("stopReason") == "error" and not self.visible_text_sent:
            error = str(message.get("errorMessage") or "provider error").strip()
            self.daemon._record_out(self.cid, f"AI error: {error}")
            self.visible_text_sent = True

    def _send_text(self, text):
        for chunk in split_message(text, 3500):
            self.daemon._record_out(self.cid, chunk)

    def _handle_text(self, text):
        clean, actions, errors = parse_tg_controls(text)
        if errors:
            log(self.cfg, f"ai[{self.cid}]: {errors} invalid Telegram control action(s)")
        visible = False
        for action in actions:
            try:
                if self._run_tg_action(action):
                    visible = True
            except Exception as exc:  # noqa: BLE001 - one bad action must not stop the reply
                log(self.cfg, f"ai[{self.cid}]: control action failed: {exc!r}")
        if clean:
            self._send_text(clean)
            visible = True
        if visible:
            self.visible_text_sent = True
        return visible

    def _run_tg_action(self, action):
        kind = str(action.get("type") or "").strip().lower()
        if kind in ("message", "status", "text"):
            text = str(action.get("text") or action.get("message") or "").strip()
            if not text:
                return False
            self._send_text(text)
            return True
        if kind == "code":
            text = str(action.get("text") or action.get("message") or "").strip()
            if not text:
                return False
            for chunk in split_message(text, 3500):
                self.daemon._record_out(
                    self.cid,
                    telegram_code_block(chunk),
                    parse_mode="HTML",
                    history_text=chunk,
                )
            return True
        if kind == "html":
            text = str(action.get("text") or action.get("html") or "").strip()
            if not text:
                return False
            for chunk in split_message(text, 3500):
                result = self.daemon._record_out(
                    self.cid,
                    chunk,
                    parse_mode="HTML",
                    history_text=_plain_html(chunk),
                )
                if result is None:
                    self.daemon._record_out(self.cid, _plain_html(chunk))
            return True
        if kind == "buttons":
            return self._send_tg_buttons(action)
        if kind == "typing":
            try:
                self.daemon.tg.send_chat_action(
                    self.cid, str(action.get("action") or "typing"))
            except TelegramError as exc:
                log(self.cfg, f"ai[{self.cid}]: sendChatAction failed: {exc}")
            return False
        log(self.cfg, f"ai[{self.cid}]: unknown control action {kind!r}")
        return False

    def _button_parts(self, item):
        if isinstance(item, dict):
            label = item.get("label") or item.get("text") or item.get("value") or ""
            value = item.get("value", label)
        elif isinstance(item, (list, tuple)):
            label = item[0] if item else ""
            value = item[1] if len(item) > 1 else label
        else:
            label = item
            value = item
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        label = _one_line(label, 60)
        return label, value

    def _send_tg_buttons(self, action):
        text = str(action.get("text") or action.get("message") or "Choose:").strip()
        raw_rows = action.get("rows")
        if not isinstance(raw_rows, list):
            raw_buttons = action.get("buttons")
            if isinstance(raw_buttons, list):
                if any(isinstance(item, (list, tuple)) for item in raw_buttons):
                    raw_rows = raw_buttons
                else:
                    raw_rows = [[item] for item in raw_buttons]
            else:
                raw_rows = []

        rows = []
        count = 0
        for raw_row in raw_rows[:8]:
            if not isinstance(raw_row, (list, tuple)):
                raw_row = [raw_row]
            row = []
            for item in raw_row[:3]:
                if count >= 24:
                    break
                label, value = self._button_parts(item)
                if not label:
                    continue
                key = f"b{self.button_seq}"
                self.button_seq += 1
                self.buttons[key] = {
                    "label": label,
                    "value": value,
                    "text": text[:3500],
                }
                row.append({"text": label, "callback_data": f"aitg:{key}"})
                count += 1
            if row:
                rows.append(row)
        while len(self.buttons) > AI_BUTTON_LIMIT:
            self.buttons.pop(next(iter(self.buttons)))

        chunks = split_message(text, 3500)
        markup = json.dumps({"inline_keyboard": rows}) if rows else None
        for index, chunk in enumerate(chunks):
            self.daemon._record_out(
                self.cid,
                chunk,
                reply_markup=markup if index == len(chunks) - 1 else None,
            )
        return True


# --------------------------------------------------------------------------
# live system status for the /status chat command
# --------------------------------------------------------------------------

_active_downloads = {}
_active_downloads_lock = threading.Lock()
_active_download_seq = 0
_cancel_lock = threading.Lock()
_local_api_restarting = threading.Event()


def start_download(name, chat_id=None, total=None):
    """Register a transfer so /status can report it while it runs."""
    global _active_download_seq
    with _active_downloads_lock:
        _active_download_seq += 1
        key = _active_download_seq
        now = time.time()
        _active_downloads[key] = {
            "id": key,
            "name": name,
            "chat_id": chat_id,
            "done": 0,
            "total": int(total or 0),
            "started": now,
            "updated": now,
            "speed": 0.0,
            "state": "queued",
            "cancelled": False,
            "temp_path": None,
        }
    return key


def begin_download(key):
    """Mark a queued transfer as the one currently downloading."""
    with _active_downloads_lock:
        rec = _active_downloads.get(key)
        if rec is None:
            return
        now = time.time()
        rec["state"] = "downloading"
        rec["started"] = now
        rec["updated"] = now
        rec["speed"] = 0.0


def update_download(key, done, total=None):
    """Update progress. done=None only fills in a newly learned total size."""
    with _active_downloads_lock:
        rec = _active_downloads.get(key)
        if rec is None:
            return
        now = time.time()
        prev_done = int(rec.get("done") or 0)
        prev_time = float(rec.get("updated") or rec.get("started") or now)
        if done is not None:
            new_done = max(0, int(done))
            rec["done"] = max(int(rec.get("done") or 0), new_done)
        if total:
            rec["total"] = int(total)
        if done is None:
            return
        dt = now - prev_time
        if dt >= 0.05:
            instant = max(0.0, (rec["done"] - prev_done) / dt)
            old = float(rec.get("speed") or 0.0)
            # Smooth bursts from TDLib's temp file and from HTTP chunks.
            rec["speed"] = instant if old <= 0 else old * 0.6 + instant * 0.4
            rec["updated"] = now


def set_download_temp(key, path):
    """Remember which local-server temp file belongs to this transfer."""
    if not path:
        return
    with _active_downloads_lock:
        rec = _active_downloads.get(key)
        if rec is not None:
            rec["temp_path"] = path


def finish_download(key):
    with _active_downloads_lock:
        _active_downloads.pop(key, None)


def cancel_download(key):
    """Mark a transfer as cancelled. Returns its record, or None if unknown."""
    with _active_downloads_lock:
        rec = _active_downloads.get(key)
        if rec is None:
            return None
        snapshot = dict(rec)
        if rec.get("state") == "queued":
            # The worker has not started it yet; drop it from the queue view now.
            _active_downloads.pop(key, None)
        else:
            rec["cancelled"] = True
        return snapshot


def download_cancelled(key):
    """True once cancel_download(key) was called (or the transfer is gone)."""
    with _active_downloads_lock:
        rec = _active_downloads.get(key)
        return rec is None or bool(rec.get("cancelled"))


def active_downloads():
    """Snapshot of transfers still in progress, oldest first."""
    with _active_downloads_lock:
        return [dict(rec) for rec in _active_downloads.values()]


def local_api_temp_dir(cfg):
    """The local Bot API server's per-bot temp directory, if it can be found."""
    token = getattr(cfg, "token", "") or ""
    if not token:
        return None
    return os.path.join(cfg.local_api_dir, token, "temp")


def _local_temp_sizes(cfg):
    sizes = {}
    temp_dir = local_api_temp_dir(cfg)
    if not temp_dir:
        return sizes
    try:
        with os.scandir(temp_dir) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        sizes[entry.path] = entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return sizes


def _monitor_local_download(cfg, key, total, baseline, stop):
    """Watch the local server's temp file while getFile() is blocking.

    The local Bot API fetches the whole file before getFile returns, so the
    normal download progress callback only starts afterwards. Reading the
    growing temp file gives /status real bytes and speed during that wait.
    """
    active = None
    while not stop.wait(0.75):
        sizes = _local_temp_sizes(cfg)
        if active is not None:
            if active in sizes:
                update_download(key, sizes[active], total)
                continue
            active = None
        candidates = []
        for path, size in sizes.items():
            old = baseline.get(path)
            if old is not None and size <= old:
                continue
            if total and size > total:
                continue
            candidates.append((size, path))
        if not candidates:
            continue
        active = max(candidates)[1]
        set_download_temp(key, active)
        update_download(key, sizes[active], total)


def downloads_status_lines():
    """Lines for /status describing active downloads ([] when idle)."""
    downloads = active_downloads()
    if not downloads:
        return []
    lines = [
        f"downloads: {len(downloads)} queued/active (cancel with /cancel_upload <id>)"
    ]
    now = time.time()
    for rec in downloads:
        done = rec.get("done") or 0
        total = rec.get("total") or 0
        speed = float(rec.get("speed") or 0.0)
        updated = float(rec.get("updated") or rec.get("started") or 0.0)
        if speed and now - updated > 5:
            speed = 0.0
        if rec.get("state") == "queued":
            detail = "queued"
            if total:
                detail += f" | {human_size(total)}"
        elif total:
            pct = max(0, min(100, int(done * 100 / total)))
            detail = f"{pct}% | {human_size(done)} of {human_size(total)}"
            detail += f" | {human_size(speed)}/s"
            if done == 0 and now - float(rec.get("started") or now) > 5:
                detail += " | contacting server"
        else:
            detail = f"{human_size(done)} downloaded"
            detail += f" | {human_size(speed)}/s"
            if done == 0 and now - float(rec.get("started") or now) > 5:
                detail += " | contacting server"
        if rec.get("cancelled"):
            detail += " | cancelling"
        lines.append(f"  #{rec.get('id')} {rec.get('name') or 'file'}: {detail}")
    return lines


def _read_int(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def status_uptime(sample=False):
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            secs = float(fh.read().split()[0])
    except (OSError, ValueError):
        return "?"
    mins = int(secs) // 60
    days, rem = divmod(mins, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def status_cpu_percent(sample=0.3):
    """CPU busy percentage, sampled over a short interval (no deps)."""

    def snap():
        try:
            with open("/proc/stat", encoding="utf-8") as fh:
                parts = fh.readline().split()[1:]
            vals = [int(p) for p in parts]
        except (OSError, ValueError):
            return None
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        return sum(vals), idle

    a = snap()
    time.sleep(sample)
    b = snap()
    if not a or not b or b[0] <= a[0]:
        return None
    busy = 1.0 - (b[1] - a[1]) / float(b[0] - a[0])
    return max(0.0, min(100.0, busy)) * 100.0


def status_cpu_temp():
    """Best available CPU temperature as (celsius, label) or None."""
    hwmon = "/sys/class/hwmon"
    try:
        chips = sorted(os.listdir(hwmon))
    except OSError:
        chips = []
    for chip in chips:
        base = os.path.join(hwmon, chip)
        name = ""
        try:
            with open(os.path.join(base, "name"), encoding="utf-8") as fh:
                name = fh.read().strip()
        except OSError:
            pass
        if name.lower() not in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            continue
        for entry in sorted(os.listdir(base)):
            if entry.startswith("temp") and entry.endswith("_input"):
                val = _read_int(os.path.join(base, entry))
                if val and 0 < val < 150000:
                    return val / 1000.0, (name or entry)
    try:
        zones = sorted(os.listdir("/sys/class/thermal"))
    except OSError:
        zones = []
    for zone in zones:
        if not zone.startswith("thermal_zone"):
            continue
        val = _read_int(os.path.join("/sys/class/thermal", zone, "temp"))
        if val and 0 < val < 150000:
            label = ""
            try:
                with open(os.path.join("/sys/class/thermal", zone, "type"), encoding="utf-8") as fh:
                    label = fh.read().strip()
            except OSError:
                pass
            return val / 1000.0, (label or zone)
    return None


FAN_NOT_REPORTED = 0xFFFF  # what a ThinkPad EC reports when it cannot read the tach


def status_thinkpad_fan():
    """Fan speed in RPM from /proc/acpi/ibm/fan, or None when absent."""
    try:
        with open("/proc/acpi/ibm/fan", encoding="utf-8") as fh:
            for line in fh:
                key, _, val = line.partition(":")
                if key.strip() == "speed" and val.strip().isdigit():
                    return int(val.strip())
    except OSError:
        pass
    return None


def status_fans(hwmon="/sys/class/hwmon"):
    """Readable fan tachometers as ([(rpm, label, percent), ...], not_reported).

    Every fan*_input in /sys/class/hwmon is used; the ThinkPad
    /proc/acpi/ibm/fan tachometer is a fallback for models without a hwmon
    fan attribute. percent is the RPM as a share of fan*_max when the
    sensor publishes its maximum, else None. not_reported is True when a
    sensor exists but only returns the 0xFFFF "no tach reading" sentinel.
    """
    readings = []
    not_reported = False
    try:
        chips = sorted(os.listdir(hwmon))
    except OSError:
        chips = []
    for chip in chips:
        base = os.path.join(hwmon, chip)
        try:
            with open(os.path.join(base, "name"), encoding="utf-8") as fh:
                name = fh.read().strip()
        except OSError:
            name = ""
        try:
            entries = [
                e
                for e in sorted(os.listdir(base))
                if e.startswith("fan") and e.endswith("_input")
            ]
        except OSError:
            entries = []
        for entry in entries:
            rpm = _read_int(os.path.join(base, entry))
            if rpm is None:
                continue
            if rpm == FAN_NOT_REPORTED:
                not_reported = True
                continue
            stem = entry[: -len("_input")]  # fan1_input -> fan1
            label = name or "fan"
            try:
                with open(
                    os.path.join(base, stem + "_label"), encoding="utf-8"
                ) as fh:
                    label = fh.read().strip() or label
            except OSError:
                pass
            percent = None
            max_rpm = _read_int(os.path.join(base, stem + "_max"))
            if max_rpm is not None and max_rpm > 0:
                percent = min(100, round(100.0 * rpm / max_rpm))
            readings.append((rpm, label, percent))
    if not readings:
        rpm = status_thinkpad_fan()
        if rpm is not None:
            if rpm == FAN_NOT_REPORTED:
                not_reported = True
            else:
                readings.append((rpm, "thinkpad", None))
    return readings, not_reported


def status_memory():
    """(used_percent, used_gib, total_gib) or None."""
    info = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key.strip()] = rest.strip()
    except OSError:
        return None

    def kb(key):
        val = info.get(key, "").split()
        return int(val[0]) if val and val[0].isdigit() else None

    total = kb("MemTotal")
    avail = kb("MemAvailable")
    if not total:
        return None
    avail = avail or 0
    used_pct = 100.0 * (1 - avail / float(total))
    return used_pct, (total - avail) / 1048576.0, total / 1048576.0


def status_disk():
    """(used_percent, free_gib) for the root filesystem or None."""
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return None
    if not usage.total:
        return None
    return round(100.0 * usage.used / usage.total), usage.free / 1073741824.0


def status_battery():
    """(capacity_percent, status_text) or None when there is no battery."""
    base = "/sys/class/power_supply"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    for entry in entries:
        if not entry.upper().startswith("BAT"):
            continue
        path = os.path.join(base, entry)
        cap = _read_int(os.path.join(path, "capacity"))
        state = ""
        try:
            with open(os.path.join(path, "status"), encoding="utf-8") as fh:
                state = fh.read().strip()
        except OSError:
            pass
        if cap is not None:
            return cap, state
    return None


def status_private_ip():
    """(ip, iface) of the interface that would carry the default route."""
    ip = None
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("1.1.1.1", 80))  # no packets sent, just route lookup
        ip = sock.getsockname()[0]
    except OSError:
        ip = None
    finally:
        if sock:
            sock.close()
    iface = None
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[3].split("/")[0] == ip:
                iface = parts[1]
                break
    except Exception:  # noqa: BLE001
        pass
    return ip, iface


def status_public_ip(timeout=7):
    """Public IP from Cloudflare trace, with fallbacks. None when offline."""
    try:
        req = urllib.request.Request(
            "https://cloudflare.com/cdn-cgi/trace", headers={"User-Agent": "tgmessage-status"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for line in resp.read(4096).decode("utf-8", "replace").splitlines():
                if line.startswith("ip="):
                    return line[3:].strip()
    except Exception:  # noqa: BLE001
        pass
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tgmessage-status"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                val = resp.read(128).decode("utf-8", "replace").strip()
            if val:
                return val
        except Exception:  # noqa: BLE001
            continue
    return None


def status_services(units):
    """[(label, state)] for the services added with `tgmessage service add`."""
    units = list(units or [])
    if not units:
        return []
    if not has_systemd():
        return [(u, "unavailable") for u in units]
    try:
        res = subprocess.run(
            ["systemctl", "is-active", *units], capture_output=True, text=True, timeout=15
        )
        lines = [l.strip() for l in res.stdout.splitlines()]
    except Exception:  # noqa: BLE001
        lines = []
    while len(lines) < len(units):
        lines.append("?")
    out = []
    for unit, state in zip(units, lines):
        label = unit[: -len(".service")] if unit.endswith(".service") else unit
        out.append((label, state))
    return out


def system_status_text(cfg):
    """The text the Telegram /status command replies with."""
    lines = [f"\U0001F4CA status - {socket.gethostname()}"]
    lines.append(f"cd: {read_workdir(cfg)}")
    line = f"uptime: {status_uptime()}"
    try:
        l1, l5, l15 = os.getloadavg()
        line += f"  |  load {l1:.2f} {l5:.2f} {l15:.2f}"
    except OSError:
        pass
    lines.append(line)

    if getattr(cfg, "is_local_api", False):
        lines.append(
            f"Bot API: local ({api_root(cfg)}) | files up to "
            f"{human_size(getattr(cfg, 'max_download', 0))}"
        )
    else:
        lines.append(
            f"Bot API: cloud | files up to {human_size(getattr(cfg, 'max_download', 20 * 1024 * 1024))}"
        )

    download_lines = downloads_status_lines()
    if download_lines:
        lines.append("")
        lines.extend(download_lines)

    services = status_services(cfg.watch_services)
    lines.append("")
    if not services:
        lines.append("services: none configured (tgmessage service add <name>)")
    else:
        up = sum(1 for _l, s in services if s == "active")
        lines.append(f"services: {up}/{len(services)} up")
        for label, state in services:
            mark = "OK" if state == "active" else state.upper()
            lines.append(f"  {label}: {mark}")

    cpu = status_cpu_percent()
    temp = status_cpu_temp()
    lines.append("")
    if cpu is not None:
        cpu_line = f"CPU: {cpu:.1f}%"
    else:
        cpu_line = "CPU: ?"
    if temp:
        cpu_line += f"  |  temp {temp[0]:.0f}°C ({temp[1]})"
    else:
        cpu_line += "  |  temp: no sensor"
    lines.append(cpu_line)

    fans, fan_not_reported = status_fans()
    if fans:
        parts = []
        for rpm, label, percent in fans:
            extra = f"{percent}% of max, {label}" if percent is not None else label
            parts.append(f"{rpm} RPM ({extra})")
        lines.append("fan: " + "  |  ".join(parts))
    elif fan_not_reported:
        lines.append("fan: n/a (fan speed sensor hardware fault - EC 0xFFFF)")
    else:
        lines.append("fan: no sensor")

    mem = status_memory()
    if mem:
        lines.append(f"RAM: {mem[0]:.0f}% used ({mem[1]:.1f}/{mem[2]:.1f} GiB)")
    disk = status_disk()
    if disk:
        lines.append(f"disk /: {disk[0]}% used  |  {disk[1]:.0f} GiB free")

    batt = status_battery()
    if batt:
        cap, state = batt
        lines.append(f"battery: {cap}% ({state or 'unknown'})")
    else:
        lines.append("battery: none (desktop / no battery present)")

    priv_ip, priv_iface = status_private_ip()
    lines.append(f"IP private: {priv_ip or 'none'}" + (f" ({priv_iface})" if priv_iface else ""))
    pub_ip = status_public_ip()
    lines.append(f"IP public: {pub_ip or 'unavailable (no internet?)'}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# /recover - try hard to bring watched services back to life
# --------------------------------------------------------------------------

RECOVER_ATTEMPTS = 3
RECOVER_START_TIMEOUT = 60        # systemctl start blocks while a unit activates
RECOVER_WAITS = (3, 6, 10)        # settle time after each start attempt
RECOVER_JOURNAL_LINES = 8         # journal lines shown for units that stay down
RECOVER_JOURNAL_MAX_CHARS = 600   # per unit, so the report stays Telegram-sized


def _run_quiet(cmd, timeout=15):
    """Run a command, never raise.  None on timeout or OSError."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _unit_state(verb, unit):
    """systemctl is-enabled / is-active answer: active, enabled, not-found, ?"""
    res = _run_quiet(["systemctl", verb, unit], timeout=10)
    if res is None:
        return "?"
    out = (res.stdout or "").strip()
    if out:
        return out
    if res.returncode != 0 and "no such file" in (res.stderr or "").lower():
        return "not-found"
    return "?"


def _unit_journal_tail(unit, n=RECOVER_JOURNAL_LINES, max_chars=RECOVER_JOURNAL_MAX_CHARS):
    """The newest journal lines of one unit, oldest first, trimmed to fit."""
    res = _run_quiet(["journalctl", "-u", unit, "-n", str(n), "--no-pager",
                      "-o", "cat"], timeout=20)
    if res is None:
        return []
    text = (res.stdout or res.stderr or "").strip()
    if not text:
        return []
    out = [line.rstrip()[:160] for line in text.splitlines()[-n:]]
    while out and sum(len(l) + 1 for l in out) > max_chars:
        out.pop(0)
    return out


def recover_services(cfg, target=None):
    """Try hard to start every down service in the watch list.

    This is the engine behind the Telegram /recover command.  For each watched
    unit that is not active it unmasks/enables it when needed, resets the
    failed state, starts it and re-checks, retrying a few times with a settling
    pause.  Report-only units (TG_SERVICE_NO_RESTART) are included because
    /recover is an explicit operator command - the watcher itself still never
    touches them.  Returns the Telegram-ready report text.
    """
    home = getattr(cfg, "home", None) or DEFAULT_HOME
    disk = read_env(home)
    watch = parse_units(disk.get("TG_WATCH_SERVICES"))
    no_restart = set(parse_units(disk.get("TG_SERVICE_NO_RESTART")))

    if target:
        want = normalize_unit(target)
        if not want:
            return f"recover: {target!r} is not a valid systemd unit name"
        if want not in watch:
            return (
                f"recover: {want} is not in the watch list.\n"
                "Add it with: tgmessage service add <unit>"
            )
        units = [want]
    else:
        units = watch

    if not units:
        return "recover: no services are configured - add one with: tgmessage service add <name>"

    if not has_systemd():
        return "recover: systemd is not available on this host - nothing to start"

    lines = [f"\U0001F504 recover - {socket.gethostname()}"]
    up = 0
    missing = []
    down = []
    for unit in units:
        label = unit[: -len(".service")] if unit.endswith(".service") else unit
        tag = " [notify-only]" if unit in no_restart else ""

        enabled = _unit_state("is-enabled", unit)
        if enabled == "not-found":
            lines.append(f"{label}: not installed - nothing to start")
            missing.append(label)
            continue
        if enabled in ("disabled", "masked", "bad"):
            if enabled == "masked":
                _run_quiet(["systemctl", "unmask", unit], timeout=20)
            _run_quiet(["systemctl", "enable", unit], timeout=30)

        active = _unit_state("is-active", unit)
        if active in ("active", "activating"):
            lines.append(f"{label}: OK ({active}){tag}")
            up += 1
            continue

        attempts = 0
        for attempt in range(RECOVER_ATTEMPTS):
            attempts = attempt + 1
            _run_quiet(["systemctl", "reset-failed", unit], timeout=20)
            _run_quiet(["systemctl", "start", unit], timeout=RECOVER_START_TIMEOUT)
            time.sleep(RECOVER_WAITS[min(attempt, len(RECOVER_WAITS) - 1)])
            active = _unit_state("is-active", unit)
            if active in ("active", "activating"):
                break
        plural = "" if attempts == 1 else "s"
        if active in ("active", "activating"):
            up += 1
            lines.append(f"{label}: STARTED after {attempts} attempt{plural}{tag} ✅")
        else:
            lines.append(f"{label}: STILL {active} after {attempts} attempt{plural}{tag} ❌")
            down.append((unit, label, active))

    lines.append("")
    lines.append(f"result: {up}/{len(units)} up")
    if missing:
        lines.append(f"not installed: {', '.join(missing)} - install them first")

    for unit, label, active in down:
        lines.append("")
        lines.append(f"{label} ({active}) - last journal lines:")
        tail = _unit_journal_tail(unit)
        if tail:
            lines += ["  " + l for l in tail]
        else:
            lines.append("  (no journal entries)")

    return "\n".join(lines)


def watcher_log_tail(cfg, lines=40, max_chars=3800):
    """The last `lines` lines of the watcher log, ready as a Telegram reply.

    Reads cfg.watcher_log (the watcher is part of this app: the file lives in
    <home>/watcher/logs/watcher.log). If that file is missing, it falls back
    to `journalctl -u watcher.service`. The result is trimmed to `max_chars`
    keeping the newest lines, because Telegram messages stop at 4096 chars.
    """
    try:
        lines = max(1, min(int(lines), 2000))
    except (TypeError, ValueError):
        lines = 40

    paths = [cfg.watcher_log, os.path.join(cfg.home, "watcher", "logs", "watcher.log")]
    text, source = "", None
    for path in dict.fromkeys(paths):  # dedupe, order kept
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                tail = fh.readlines()[-lines:]
            text = "".join(tail).rstrip()
            source = path
            break
        except OSError:
            continue

    if not text:
        try:
            res = subprocess.run(
                ["journalctl", "-u", "watcher.service", "-n", str(lines),
                 "--no-pager", "-o", "short-iso"],
                capture_output=True, text=True, timeout=20,
            )
            text = (res.stdout or "").rstrip()
            source = "journalctl -u watcher.service"
        except Exception:  # noqa: BLE001 - /logs must never crash the daemon
            text = ""

    if not text:
        return ("no watcher log found yet - the watcher service may not have "
                f"written anything.\nlooked for: {cfg.watcher_log}")

    header = f"\U0001F4DC watcher log (last {lines} lines)\n{source}"
    note = "(...trimmed, newest lines below...)\n"
    budget = max_chars - len(header) - 2
    if len(text) > budget:
        cut = text[-(budget - len(note)):]
        nl = cut.find("\n")
        if nl != -1:
            cut = cut[nl + 1:]
        text = note + cut
    return header + "\n\n" + text


# --------------------------------------------------------------------------
# incoming files: anything a user sends is saved into cfg.files_dir
# --------------------------------------------------------------------------


def safe_filename(name):
    """Make a received filename safe for the local filesystem."""
    name = os.path.basename((name or "").strip()) or "file"
    name = name.replace("\x00", "")
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .")
    if not name or name in (".", ".."):
        name = "file"
    return name[:180]


def unique_path(directory, name):
    """directory/name, or name-1, name-2 ... if it already exists."""
    root, ext = os.path.splitext(name)
    candidate = os.path.join(directory, name)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{root}-{i}{ext}")
        i += 1
    return candidate


def _ext_for(mime):
    ext = mimetypes.guess_extension(mime or "") or ""
    return ".jpg" if ext == ".jpe" else ext


def incoming_media(msg):
    """(file_id, suggested_name, file_size, kind) for media messages, else None."""
    mid = msg.get("message_id", "x")
    doc = msg.get("document")
    if doc:
        name = doc.get("file_name") or f"document_{mid}{_ext_for(doc.get('mime_type'))}"
        return doc.get("file_id"), name, doc.get("file_size"), "document"
    audio = msg.get("audio")
    if audio:
        name = audio.get("file_name") or f"audio_{mid}{_ext_for(audio.get('mime_type'))}"
        return audio.get("file_id"), name, audio.get("file_size"), "audio"
    video = msg.get("video")
    if video:
        name = video.get("file_name") or f"video_{mid}{_ext_for(video.get('mime_type'))}"
        return video.get("file_id"), name, video.get("file_size"), "video"
    voice = msg.get("voice")
    if voice:
        return voice.get("file_id"), f"voice_{mid}.ogg", voice.get("file_size"), "voice"
    photo = msg.get("photo")
    if photo and isinstance(photo, list):
        best = photo[-1]
        return best.get("file_id"), f"photo_{mid}.jpg", best.get("file_size"), "photo"
    return None


def chat_help_text(cfg, is_sys=False, is_mang=False):
    lines = [
        "tgmessage commands (from the Telegram chat):",
        "",
        "/help   - show this help",
    ]
    if is_sys:
        lines += [
            "/status - live server report: active file downloads, configured services,",
            "          CPU load + temperature, fan speed, RAM, disk, battery, IPs",
            "/logs   - last 40 lines of the watcher system log (/logs 100 for more)",
            "/cancel_upload <id> - stop an active upload listed by /status",
        ]
        if getattr(cfg, "ai_enabled", False):
            lines += [
                "/ai     - start AI mode: talk to the pi coding harness here",
                "/ai_exit - quit AI mode (/ai-exit also works)",
                "          (uses the saved cd; inside AI mode /model picks",
                "          a model with buttons, /thinking picks the level,",
                "          /session resumes a saved pi session, and /new,",
                "          /stop, /compact and /pi <text> also work)",
                "/ai-status - AI context, tokens out and total token usage",
                "/ai-agent  - show the rules injected into the AI",
                "/ai-agent-add <text> - add a persistent AI rule",
                "/ai-agent-remove <n|text> - remove an AI rule",
                "/btw <text> - quick side question in AI mode",
                "/live - running pi sessions and subagents in the saved cd",
            ]
        else:
            lines += [
                "/ai - AI mode is disabled on this host",
                "          owner can enable it with TG_AI_ENABLED=1",
            ]
        if getattr(cfg, "shell_enabled", False):
            lines += [
                "/sh_on  - open a root shell in this private chat (dangerous)",
                "/sh_off - close the root shell and kill a running command",
                "          /sh-on and /sh-off also work; while ON, send",
                "          commands without a leading slash",
            ]
        else:
            lines += [
                "/sh_on / /sh_off - root shell is disabled on this host",
                "          owner can enable it with: tgmessage shell on",
            ]
        if is_mang:
            lines += [
                "/recover [unit] - try hard to start a down watched service;",
                "                  without a unit it recovers every down watched service",
            ]
        else:
            lines += [
                "/recover needs mang access - ask the bot owner to run:",
                "  tgmessage allow mang <your-chat-id>",
            ]
    else:
        lines += [
            "/status, /logs, /cancel_upload, /ai and the /sh_on root shell need sys access -",
            "ask the bot owner to run: tgmessage allow sys <your-chat-id>",
        ]
    lines += [
        "",
        "Send a photo or any file (.sh, .txt, .zip, ...) and it is saved to:",
        f"  {cfg.files_dir}",
        "You get a confirmation with the saved path. Files up to "
        f"{human_size(getattr(cfg, 'max_download', 20 * 1024 * 1024))} can be downloaded "
        f"({'local Bot API server' if getattr(cfg, 'is_local_api', False) else 'Telegram cloud Bot API'}).",
    ]
    if not getattr(cfg, "is_local_api", False):
        lines.append("For larger files set up the local Bot API server: tgmessage local-api")
    lines += [
        "",
        "The terminal UI additionally has /session, /allow, /deny,",
        "/requests, /copy and /file <path>.",
    ]
    return "\n".join(lines)


# Telegram's command menu only accepts lowercase letters, digits and
# underscores, so /sh-on and /sh-off are registered as /sh_on and /sh_off.
# Both spellings work in the chat.
BOT_COMMANDS = [
    ("help", "Show the command list"),
    ("status", "Server status: services, CPU, RAM, disk, transfers"),
    ("logs", "Last lines of the watcher log"),
    ("cancel_upload", "Cancel an active upload by id"),
    ("recover", "Recover down watched services"),
    ("sh_on", "Open the root shell in this private chat"),
    ("sh_off", "Close the root shell"),
    ("ai", "Start AI mode (pi coding harness)"),
    ("ai_exit", "Stop AI mode"),
    ("model", "Pick an AI model (AI mode)"),
    ("thinking", "Pick a thinking level (AI mode)"),
    ("session", "Resume a pi session (AI mode)"),
    ("ai_status", "Show AI context and token usage (AI mode)"),
    ("ai_agent", "Show the AI agent rules"),
    ("ai_agent_add", "Add an AI agent rule"),
    ("ai_agent_remove", "Remove an AI agent rule"),
    ("btw", "Ask a quick side question (AI mode)"),
    ("live", "Show live pi sessions and subagents"),
]


def register_bot_commands(cfg, tg=None):
    """Publish the command menu shown when the user presses '/' in Telegram."""
    if not cfg.token:
        return False, "no bot token configured"
    commands = [{"command": name, "description": desc} for name, desc in BOT_COMMANDS]
    try:
        (tg or Telegram(cfg)).call(
            "setMyCommands", {"commands": json.dumps(commands)}, timeout=20)
    except TelegramError as exc:
        return False, str(exc)
    return True, ""


# --------------------------------------------------------------------------
# the daemon: polls Telegram, serves the socket
# --------------------------------------------------------------------------


class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tg = Telegram(cfg)
        self.history = History(cfg.state_dir, cfg.history)
        self.pending = Pending(os.path.join(cfg.state_dir, "pending.json"))
        self.registry = Registry(os.path.join(cfg.state_dir, "chats.json"))
        self.offset_path = os.path.join(cfg.state_dir, "offset.json")
        self.offset = self._load_offset()
        self.allowed = set(cfg.allowed)
        self.sys = set(cfg.sys)
        self.mang = set(cfg.mang)
        self.default_chat = cfg.default_chat
        self.bot = None
        self.clients = set()
        self.clients_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.seen = set()
        self.running = True
        self.download_queue = queue.Queue()
        # /sh-on state is memory only: daemon restart, /sh-off, timeout or a
        # permission change closes every session.
        self.shell_sessions = {}
        self.shell_lock = threading.Lock()
        self.shell_slots = threading.Semaphore(SHELL_MAX_CONCURRENT)
        # /ai processes are memory only: /ai-exit, a daemon restart or a
        # permission change stops them.
        self.ai_sessions = {}
        self.ai_lock = threading.Lock()
        self._load_shell_state()

    # ---- state -------------------------------------------------------
    def _offset_key(self):
        """Update ids are scoped to one Bot API server."""
        if not getattr(self.cfg, "is_local_api", False):
            return "cloud"
        return self.cfg.api_root or "local"

    def _read_offset_data(self):
        try:
            with open(self.offset_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _load_offset(self):
        data = self._read_offset_data()
        key = self._offset_key()
        offsets = data.get("offsets")
        if isinstance(offsets, dict):
            try:
                return max(0, int(offsets.get(key, 0)))
            except (TypeError, ValueError):
                return 0
        # Old single-server file (all previous releases were cloud-only).
        try:
            legacy = int(data.get("offset", 0))
        except (TypeError, ValueError):
            return 0
        try:
            tmp = self.offset_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"offsets": {"cloud": legacy}}, fh)
            os.replace(tmp, self.offset_path)
        except OSError:
            pass
        return legacy if key == "cloud" else 0

    def _save_offset(self):
        key = self._offset_key()
        data = self._read_offset_data()
        offsets = data.get("offsets")
        if not isinstance(offsets, dict):
            offsets = {}
        offsets[key] = max(0, int(self.offset))
        try:
            tmp = self.offset_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"offsets": offsets}, fh)
            os.replace(tmp, self.offset_path)
        except OSError:
            pass

    # ---- allow list --------------------------------------------------
    def reload_config(self):
        disk = read_env(self.cfg.home)
        allowed = set(parse_ids(disk.get("TG_ALLOWED_CHAT_IDS")))
        mang = set(parse_ids(disk.get("TG_MANG_CHAT_IDS")))
        sys_ids = set(parse_ids(disk.get("TG_SYS_CHAT_IDS"))) | mang
        allowed |= sys_ids
        legacy = (disk.get("TG_CHAT_ID") or "").strip()
        if legacy:
            allowed.add(legacy)
        self.allowed = allowed
        self.sys = sys_ids
        self.mang = mang
        default = (disk.get("TG_DEFAULT_CHAT") or "").strip() or legacy
        if default and default not in allowed:
            default = ""
        if not default and allowed:
            default = sorted(allowed, key=id_sort_key)[0]
        self.default_chat = default
        self.cfg.watch_services = parse_units(disk.get("TG_WATCH_SERVICES"))

        def setting(key, fallback=""):
            # The systemd unit loads .env via EnvironmentFile, so the daemon's
            # os.environ keeps the values from startup.  Prefer the file here
            # or `tgmessage shell on/off` would not take effect until a full
            # service restart.
            if key in disk:
                return disk[key]
            return os.environ.get(key) or fallback

        self.cfg.shell_enabled = parse_bool(setting("TG_SHELL_ENABLED", "0"))
        self.cfg.shell_idle = parse_seconds(
            setting("TG_SHELL_IDLE_SECONDS", "600"), 600, 60, 86400)
        self.cfg.shell_max = max(
            self.cfg.shell_idle,
            parse_seconds(setting("TG_SHELL_MAX_SECONDS", "1800"), 1800, 60, 604800),
        )
        self.cfg.shell_timeout = parse_seconds(
            setting("TG_SHELL_TIMEOUT", "120"), 120, 5, 3600)
        self.cfg.ai_enabled = parse_bool(setting("TG_AI_ENABLED", "1"))
        self.cfg.pi_bin = setting("TG_PI_BIN", "").strip()
        self.cfg.ai_provider = setting("TG_AI_PROVIDER", "").strip()
        self.cfg.ai_model = setting("TG_AI_MODEL", "").strip()

        # Turning AI mode off, or revoking sys from a chat, stops its pi process.
        with self.ai_lock:
            ai_dropped = []
            for cid, session in list(self.ai_sessions.items()):
                if not self.cfg.ai_enabled or cid not in self.sys:
                    ai_dropped.append(self.ai_sessions.pop(cid))
        for session in ai_dropped:
            session.close()
        if ai_dropped:
            log(self.cfg, f"AI mode: stopped {len(ai_dropped)} session(s) after a permission/config change")

        # Turning the feature off, or revoking sys from a chat, closes live
        # shells immediately (including any command still running).
        with self.shell_lock:
            dropped = []
            for cid, sess in list(self.shell_sessions.items()):
                if not self.cfg.shell_enabled or cid not in self.sys:
                    dropped.append(self.shell_sessions.pop(cid))
            self._persist_shell_locked()
            if dropped:
                log(self.cfg, f"shell mode: closed {len(dropped)} session(s) after a permission/config change")
        for sess in dropped:
            if sess.get("proc") is not None:
                kill_process_group(sess["proc"])

    def _persist(self):
        persist_permissions(self.cfg, self.allowed, self.sys, self.mang, self.default_chat)

    def can_sys(self, cid):
        return str(cid) in self.sys

    def can_mang(self, cid):
        return str(cid) in self.mang

    def do_allow(self, cid, level="chat"):
        cid = str(cid)
        if level == "sys":
            if cid in self.sys:
                return False
            self.sys.add(cid)
            self.allowed.add(cid)
        elif level == "mang":
            if cid in self.mang:
                return False
            self.mang.add(cid)
            self.sys.add(cid)
            self.allowed.add(cid)
        else:
            if cid in self.allowed:
                return False
            self.allowed.add(cid)
        if not self.default_chat:
            self.default_chat = cid
        self.pending.remove(cid)
        self._persist()
        log(self.cfg, f"allowed {level} for chat {cid}")
        self._announce()
        threading.Thread(target=self._refresh_then_announce, args=(cid,), daemon=True).start()
        return True

    def do_deny(self, cid, level="chat"):
        cid = str(cid)
        if level == "mang":
            if cid not in self.mang:
                return False
            self.mang.discard(cid)
        elif level == "sys":
            if cid not in self.sys:
                return False
            self.sys.discard(cid)
            self.mang.discard(cid)
        else:
            if cid not in self.allowed:
                return False
            self.allowed.discard(cid)
            self.sys.discard(cid)
            self.mang.discard(cid)
        if self.default_chat == cid and cid not in self.allowed:
            self.default_chat = sorted(self.allowed, key=id_sort_key)[0] if self.allowed else ""
        self._persist()
        log(self.cfg, f"denied {level} for chat {cid}")
        self._announce()
        return True

    def _announce(self):
        self.broadcast(
            {
                "type": "allowed",
                "allowed": sorted(self.allowed, key=id_sort_key),
                "sys": sorted(self.sys, key=id_sort_key),
                "mang": sorted(self.mang, key=id_sort_key),
                "default_chat": self.default_chat,
            }
        )
        self.broadcast({"type": "sessions", "sessions": self.sessions_payload()})

    # ---- chat metadata (names/usernames) -----------------------------
    def refresh_chat(self, cid):
        """Ask Telegram who a chat id is, so we can show @username not the id."""
        try:
            info = self.tg.get_chat(cid) or {}
        except TelegramError as exc:
            log(self.cfg, f"getChat {cid} failed: {exc}")
            return
        meta = chat_meta(info)
        if meta.get("name") or meta.get("username"):
            self.registry.touch(cid, meta, "", stamp=False)

    def refresh_allowed(self):
        for cid in sorted(self.allowed, key=id_sort_key):
            self.refresh_chat(cid)

    def _refresh_then_announce(self, cid):
        self.refresh_chat(cid)
        self._announce()

    # ---- fan-out -----------------------------------------------------
    def send(self, conn, obj):
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        with self.send_lock:
            try:
                conn.sendall(line)
            except OSError:
                pass

    def broadcast(self, obj):
        with self.clients_lock:
            conns = list(self.clients)
        for conn in conns:
            self.send(conn, obj)

    # ---- sessions ----------------------------------------------------
    def sessions_payload(self):
        pend = {e["chat_id"] for e in self.pending.all()}
        ids = set(self.allowed) | set(self.history.chats()) | set(self.registry.all()) | pend
        out = []
        for cid in ids:
            meta = self.registry.get(cid)
            allowed = cid in self.allowed
            out.append(
                {
                    "chat_id": cid,
                    "name": meta.get("name") or cid,
                    "username": meta.get("username") or "",
                    "type": meta.get("type") or "",
                    "allowed": allowed,
                    "sys": cid in self.sys,
                    "mang": cid in self.mang,
                    "pending": cid in pend,
                    "last_ts": meta.get("last_ts") or 0,
                    "count": self.history.count(cid) if allowed else 0,
                }
            )
        out.sort(key=lambda s: (not s["allowed"], -(s["last_ts"] or 0), id_sort_key(s["chat_id"])))
        return out

    def ready_payload(self):
        return {
            "type": "ready",
            "bot": self.bot,
            "port": self.cfg.port,
            "allowed": sorted(self.allowed, key=id_sort_key),
            "sys": sorted(self.sys, key=id_sort_key),
            "mang": sorted(self.mang, key=id_sort_key),
            "default_chat": self.default_chat,
            "pendings": self.pending.all(),
            "sessions": self.sessions_payload(),
        }

    # ---- Telegram ----------------------------------------------------
    def poll_loop(self):
        backoff = 2
        last_err = None
        while self.running:
            try:
                updates = self.tg.get_updates(self.offset)
                backoff = 2
                if last_err is not None:
                    log(self.cfg, "connection to Telegram restored")
                    last_err = None
                for update in updates:
                    self.offset = max(self.offset, update.get("update_id", 0) + 1)
                    self._save_offset()
                    try:
                        self.handle_update(update)
                    except Exception as exc:  # never let one bad update kill us
                        log(self.cfg, f"bad update skipped: {exc!r}")
            except TelegramError as exc:
                msg = str(exc)
                if msg != last_err:
                    log(self.cfg, f"getUpdates failed: {msg}")
                    last_err = msg
                self._sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as exc:
                log(self.cfg, f"poller error: {exc!r}")
                self._sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _sleep(self, seconds):
        end = time.time() + seconds
        while self.running and time.time() < end:
            time.sleep(0.2)

    def handle_update(self, update):
        callback = update.get("callback_query")
        if callback:
            self.handle_callback_query(callback)
            return
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat = msg.get("chat") or {}
        if chat.get("id") is None:
            return
        cid = str(chat["id"])
        text = message_text(msg)
        meta = chat_meta(chat)

        if cid not in self.allowed:
            if chat.get("type") != "private":
                log(self.cfg, f"ignoring non-private chat {cid} (only private, allowed chats are relayed)")
                return
            self.pending.add(cid, meta, text)
            self.registry.touch(cid, meta, "in")
            log(self.cfg, f"request from {cid} ({meta['name']}) - allow with: tgmessage allow {cid}")
            self.broadcast({"type": "request", "chat_id": cid, "text": text, **meta})
            return

        mid = msg.get("message_id")
        key = (cid, mid, text)
        if key in self.seen:
            return
        self.seen.add(key)
        if len(self.seen) > 5000:
            self.seen = set(list(self.seen)[-2000:])

        rec = {
            "ts": float(msg.get("date") or time.time()),
            "dir": "in",
            "id": mid,
            "chat_id": cid,
            "text": text,
        }
        self.history.append(cid, rec)
        self.registry.touch(cid, meta, "in")
        self.broadcast({"type": "msg", **rec})
        if msg.get("text"):
            edited = "message" not in update
            self.maybe_chat_command(
                cid, text, chat_type=chat.get("type") or "", edited=edited)
        self.maybe_save_media(cid, msg)

    # ---- chat commands coming from Telegram ---------------------------
    def maybe_chat_command(self, cid, text, chat_type="", edited=False):
        """Handle /commands typed in the Telegram chat itself.

        When a /sh-on session is active, plain messages are shell commands.
        When an /ai session is active, plain messages are prompts for pi.
        Bot commands keep working; /sh-off and /ai-exit always close their
        session.  Edited messages are never executed, so editing an old
        command cannot run it twice.
        """
        cmd = (text or "").strip()
        if not cmd:
            return
        if not cmd.startswith("/"):
            if self._ai_active(cid):
                self._ai_prompt_async(cid, cmd, edited)
                return
            self._maybe_shell_input(cid, cmd, chat_type, edited)
            return
        head = cmd.split()[0].lower()
        if "@" in head:  # /status@thebot in group chats
            head = head.split("@", 1)[0]
        if self._ai_mode_take(cid, cmd, head, chat_type, edited):
            return
        if self._shell_mode_take(cid, cmd, head, chat_type, edited):
            return
        if head == "/status":
            if not self.can_sys(cid):
                threading.Thread(target=self._chat_denied_reply, args=(cid, "/status", "sys"), daemon=True).start()
            else:
                threading.Thread(target=self._chat_status_reply, args=(cid,), daemon=True).start()
        elif head == "/logs":
            if not self.can_sys(cid):
                threading.Thread(target=self._chat_denied_reply, args=(cid, "/logs", "sys"), daemon=True).start()
                return
            n = 40
            parts = cmd.split()
            if len(parts) > 1:
                try:
                    n = max(1, min(2000, int(parts[1])))
                except ValueError:
                    pass
            threading.Thread(target=self._chat_logs_reply, args=(cid, n), daemon=True).start()
        elif head == "/cancel_upload":
            if not self.can_sys(cid):
                threading.Thread(target=self._chat_denied_reply, args=(cid, "/cancel_upload", "sys"), daemon=True).start()
                return
            upload_id = None
            parts = cmd.split()
            if len(parts) > 1:
                try:
                    upload_id = int(parts[1].lstrip("#"))
                except ValueError:
                    upload_id = None
                if upload_id is not None and upload_id <= 0:
                    upload_id = None
            threading.Thread(target=self._chat_cancel_upload_reply, args=(cid, upload_id), daemon=True).start()
        elif head == "/recover":
            if not self.can_mang(cid):
                threading.Thread(target=self._chat_denied_reply, args=(cid, "/recover", "mang"), daemon=True).start()
                return
            target = None
            parts = cmd.split()
            if len(parts) > 1:
                target = parts[1]
            threading.Thread(target=self._chat_recover_reply, args=(cid, target), daemon=True).start()
        elif head in ("/sh-on", "/sh_on"):
            threading.Thread(target=self._chat_shell_on_reply, args=(cid, chat_type), daemon=True).start()
        elif head in ("/sh-off", "/sh_off"):
            threading.Thread(target=self._chat_shell_off_reply, args=(cid,), daemon=True).start()
        elif head in ("/ai",):
            threading.Thread(target=self._chat_ai_on_reply, args=(cid, chat_type), daemon=True).start()
        elif head in ("/ai-exit", "/ai_exit"):
            self._record_out(cid, "AI mode is already OFF.")
        elif head == "/model":
            self._record_out(cid, "The model picker only works in AI mode. Send /ai first.")
        elif head in ("/session", "/sessions", "/resume"):
            self._record_out(cid, "The session picker only works in AI mode. Send /ai first.")
        elif head in ("/ai-status", "/ai_status"):
            self._record_out(cid, "AI mode is OFF - send /ai first.")
        elif head == "/btw":
            if not self.can_sys(cid):
                threading.Thread(target=self._chat_denied_reply, args=(cid, "/btw", "sys"), daemon=True).start()
            else:
                self._record_out(cid, "AI mode is OFF - send /ai first, then /btw <question>.")
        elif head == "/live":
            if not self.can_sys(cid):
                threading.Thread(target=self._chat_denied_reply, args=(cid, "/live", "sys"), daemon=True).start()
            else:
                threading.Thread(target=self._chat_ai_live_reply, args=(cid,), daemon=True).start()
        elif head in ("/ai-agent", "/ai_agent"):
            if not self.can_sys(cid):
                threading.Thread(target=self._chat_denied_reply, args=(cid, "/ai-agent", "sys"), daemon=True).start()
            else:
                threading.Thread(target=self._chat_ai_agent_reply, args=(cid,), daemon=True).start()
        elif head in ("/ai-agent-add", "/ai_agent_add"):
            if not self.can_sys(cid):
                threading.Thread(target=self._chat_denied_reply, args=(cid, "/ai-agent-add", "sys"), daemon=True).start()
                return
            parts = cmd.split(None, 1)
            rule = parts[1].strip() if len(parts) > 1 else ""
            threading.Thread(target=self._chat_ai_agent_add_reply, args=(cid, rule), daemon=True).start()
        elif head in ("/ai-agent-remove", "/ai_agent_remove"):
            if not self.can_sys(cid):
                threading.Thread(target=self._chat_denied_reply, args=(cid, "/ai-agent-remove", "sys"), daemon=True).start()
                return
            parts = cmd.split(None, 1)
            rule = parts[1].strip() if len(parts) > 1 else ""
            threading.Thread(target=self._chat_ai_agent_remove_reply, args=(cid, rule), daemon=True).start()
        elif head in ("/thinking", "/think"):
            self._record_out(cid, "The thinking picker only works in AI mode. Send /ai first.")
        elif head in ("/help", "/start", "/commands"):
            threading.Thread(target=self._chat_help_reply, args=(cid,), daemon=True).start()

    def _record_out(self, cid, text, send=True, parse_mode=None, history_text=None,
                    reply_markup=None):
        """Record an outgoing message; optionally deliver it to Telegram first.

        `text` is what Telegram receives (possibly HTML), while `history_text`
        keeps the terminal UI and chat history readable.
        """
        result = None
        if send:
            try:
                result = self.tg.send_message(
                    cid, text, parse_mode=parse_mode, reply_markup=reply_markup)
            except TelegramError as exc:
                log(self.cfg, f"reply to {cid} failed: {exc}")
        rec = {
            "ts": time.time(),
            "dir": "out",
            "id": (result or {}).get("message_id"),
            "chat_id": cid,
            "text": history_text if history_text is not None else text,
        }
        self.history.append(cid, rec)
        self.registry.touch(cid, {}, "out")
        self.broadcast({"type": "msg", **rec})
        return result

    def _chat_denied_reply(self, cid, command, level):
        log(self.cfg, f"{command} denied for chat {cid} (needs {level} access)")
        self._record_out(
            cid,
            f"permission denied: {command} needs {level} access.\n"
            f"Ask the bot owner to run: tgmessage allow {level} {cid}",
        )

    def _chat_status_reply(self, cid):
        log(self.cfg, f"/status requested by chat {cid}")
        try:
            text = system_status_text(self.cfg)
        except Exception as exc:  # noqa: BLE001
            log(self.cfg, f"/status failed: {exc!r}")
            text = f"status error: {exc!r}"
        session = self._ai_session(cid)
        if session is not None:
            model = session.current_model or {}
            name = model.get("id") or "default"
            provider = model.get("provider") or ""
            if provider:
                name = f"{provider}/{name}"
            state = "working" if session.working else "idle"
            text += f"\n\nAI: ON ({state})  |  model {name}"
        self._record_out(cid, text)

    def _chat_logs_reply(self, cid, lines=40):
        log(self.cfg, f"/logs requested by chat {cid} ({lines} lines)")
        try:
            text = watcher_log_tail(self.cfg, lines)
        except Exception as exc:  # noqa: BLE001
            log(self.cfg, f"/logs failed: {exc!r}")
            text = f"logs error: {exc!r}"
        self._record_out(cid, text)

    def _chat_cancel_upload_reply(self, cid, upload_id=None):
        if upload_id is None:
            self._record_out(
                cid,
                "usage: /cancel_upload <id>\n"
                "Use /status to see active uploads and their ids, e.g. /cancel_upload 21",
            )
            return
        with _cancel_lock:
            local = getattr(self.cfg, "is_local_api", False)
            paused = False
            if local:
                # Stop the worker from starting its next file while the local
                # server is restarting.  This also closes the race where a
                # finished transfer is cancelled just as the next one starts.
                _local_api_restarting.set()
                paused = True
            log(self.cfg, f"/cancel_upload {upload_id} requested by chat {cid}")
            rec = cancel_download(upload_id)
            if rec is None:
                if paused:
                    _local_api_restarting.clear()
                text = f"no active upload with id {upload_id} - use /status to list them"
            elif rec.get("state") == "queued":
                if paused:
                    _local_api_restarting.clear()
                text = f"cancelled queued upload #{upload_id}: {rec.get('name') or 'file'}"
            else:
                done = rec.get("done") or 0
                total = rec.get("total") or 0
                if total:
                    detail = f"{human_size(done)} / {human_size(total)} downloaded"
                else:
                    detail = f"{human_size(done)} downloaded"
                if local:
                    # The local server keeps fetching after our HTTP request is
                    # cancelled.  A restart is the only way to abort that fetch.
                    log(self.cfg, f"stopping local Bot API fetch for #{upload_id}")
                    try:
                        stopped, err = local_api_stop_fetches(self.cfg)
                        if stopped:
                            # Keep the queue paused until the old temp file is
                            # gone; the next file must not reuse that path.
                            remove_local_temp(self.cfg, rec.get("temp_path"))
                            finish_download(upload_id)
                            text = (
                                f"cancelled upload #{upload_id}: {rec.get('name') or 'file'} "
                                f"({detail}). Stopped the local server fetch."
                            )
                        else:
                            text = (
                                f"cancel requested for #{upload_id}: {rec.get('name') or 'file'} "
                                f"({detail}), but could not stop the local server: {err}"
                            )
                    finally:
                        _local_api_restarting.clear()
                else:
                    text = f"cancelled upload #{upload_id}: {rec.get('name') or 'file'} ({detail})"
        self._record_out(cid, text)

    def _chat_help_reply(self, cid):
        log(self.cfg, f"/help requested by chat {cid}")
        self._record_out(
            cid,
            chat_help_text(self.cfg, is_sys=self.can_sys(cid), is_mang=self.can_mang(cid)),
        )

    def _chat_recover_reply(self, cid, target=None):
        log(self.cfg, f"/recover requested by chat {cid} ({target or 'every down watched service'})")
        self._record_out(
            cid,
            "\U0001F504 recover: checking the watched services (this can take a minute)...",
        )
        try:
            text = recover_services(self.cfg, target=target)
        except Exception as exc:  # noqa: BLE001
            log(self.cfg, f"/recover failed: {exc!r}")
            text = f"recover error: {exc!r}"
        if len(text) > 3800:
            text = text[:3800].rstrip() + "\n… (report trimmed)"
        self._record_out(cid, text)

    # ---- /ai: private pi harness in a private chat --------------------
    def _ai_session(self, cid):
        with self.ai_lock:
            return self.ai_sessions.get(cid)

    def _ai_take(self, cid):
        with self.ai_lock:
            return self.ai_sessions.pop(cid, None)

    def _ai_active(self, cid):
        session = self._ai_session(cid)
        if session is None:
            return False
        if not session.alive:
            session = self._ai_take(cid)
            if session is not None:
                session.close()
            return False
        return True

    def _ai_mode_take(self, cid, cmd, head, chat_type, edited):
        """Return True when an active /ai session owns this message."""
        session = self._ai_session(cid)
        if session is None:
            return False
        if not session.alive:
            self._ai_take(cid)
            session.close()
            if head == "/ai":
                return False
            self._record_out(cid, "AI mode is OFF: pi stopped. Send /ai to start it again.")
            return True
        if edited:
            self._record_out(cid, "AI: edited messages are ignored - send the message again.")
            return True
        if head in ("/ai-exit", "/ai_exit"):
            threading.Thread(target=self._chat_ai_exit_reply, args=(cid,), daemon=True).start()
            return True
        if head == "/ai":
            self._record_out(
                cid,
                f"AI mode is already ON.\ncd: {session.cwd}\n"
                "Send /ai-exit to stop it.",
            )
            return True
        if head in ("/sh-on", "/sh_on", "/sh-off", "/sh_off"):
            self._record_out(
                cid, "AI mode is ON - send /ai-exit before using the root shell.")
            return True
        if head == "/model":
            threading.Thread(target=self._chat_ai_model_reply, args=(cid,), daemon=True).start()
            return True
        if head in ("/session", "/sessions", "/resume"):
            threading.Thread(target=self._chat_ai_sessions_reply, args=(cid,), daemon=True).start()
            return True
        if head in ("/ai-status", "/ai_status"):
            threading.Thread(target=self._chat_ai_status_reply, args=(cid,), daemon=True).start()
            return True
        if head in ("/ai-agent", "/ai_agent"):
            threading.Thread(target=self._chat_ai_agent_reply, args=(cid,), daemon=True).start()
            return True
        if head in ("/ai-agent-add", "/ai_agent_add"):
            parts = cmd.split(None, 1)
            rule = parts[1].strip() if len(parts) > 1 else ""
            threading.Thread(
                target=self._chat_ai_agent_add_reply, args=(cid, rule), daemon=True).start()
            return True
        if head in ("/ai-agent-remove", "/ai_agent_remove"):
            parts = cmd.split(None, 1)
            rule = parts[1].strip() if len(parts) > 1 else ""
            threading.Thread(
                target=self._chat_ai_agent_remove_reply, args=(cid, rule), daemon=True).start()
            return True
        if head in ("/thinking", "/think"):
            threading.Thread(target=self._chat_ai_thinking_reply, args=(cid,), daemon=True).start()
            return True
        if head in ("/new", "/new_session"):
            threading.Thread(target=self._chat_ai_new_reply, args=(cid,), daemon=True).start()
            return True
        if head in ("/stop", "/abort"):
            threading.Thread(target=self._chat_ai_stop_reply, args=(cid,), daemon=True).start()
            return True
        if head == "/compact":
            threading.Thread(target=self._chat_ai_compact_reply, args=(cid,), daemon=True).start()
            return True
        if head == "/btw":
            text = cmd[4:].strip()
            if not text:
                self._record_out(cid, "usage: /btw <side question>")
            else:
                threading.Thread(
                    target=self._chat_ai_btw_reply,
                    args=(cid, text), daemon=True).start()
            return True
        if head == "/live":
            threading.Thread(
                target=self._chat_ai_live_reply, args=(cid,), daemon=True).start()
            return True
        if head == "/pi":
            text = cmd[3:].strip()
            if not text:
                self._record_out(cid, "usage: /pi <message or pi slash command>")
            elif text.startswith("/"):
                sub = text.split()[0].lower()
                if "@" in sub:
                    sub = sub.split("@", 1)[0]
                if sub in ("/model", "/thinking", "/think", "/new",
                           "/new_session", "/session", "/sessions", "/resume",
                           "/ai-status", "/ai_status", "/ai-agent", "/ai_agent",
                           "/ai-agent-add", "/ai_agent_add",
                           "/ai-agent-remove", "/ai_agent_remove",
                           "/btw", "/live",
                           "/stop", "/abort", "/compact",
                           "/help", "/start", "/commands"):
                    self._ai_mode_take(cid, text, sub, chat_type, edited)
                else:
                    self._ai_prompt_async(cid, text, edited)
            else:
                self._ai_prompt_async(cid, text, edited)
            return True
        if head in ("/help", "/start", "/commands"):
            threading.Thread(target=self._chat_ai_help_reply, args=(cid,), daemon=True).start()
            return True
        if head in ("/status", "/logs", "/cancel_upload", "/recover"):
            return False
        # Every other slash command belongs to pi: extension commands,
        # prompt templates and skills all run through a normal prompt.
        self._ai_prompt_async(cid, cmd, edited)
        return True

    def _ai_prompt_async(self, cid, text, edited=False):
        if edited:
            self._record_out(cid, "AI: edited messages are ignored - send the message again.")
            return
        text = (text or "").strip()
        if not text:
            return
        if self._ai_session(cid) is None:
            return
        threading.Thread(target=self._chat_ai_prompt, args=(cid, text), daemon=True).start()

    def _chat_ai_prompt(self, cid, text):
        session = self._ai_session(cid)
        if session is None:
            self._record_out(cid, "AI mode is OFF - send /ai to start it.")
            return
        data, error = session.prompt(text)
        if error:
            self._record_out(cid, f"AI error: {error}")
            return
        if (data or {}).get("disposition") == "queued":
            self._record_out(cid, "queued - pi will read this after the current turn.")
        shown = text if len(text) <= 200 else text[:200] + "..."
        log(self.cfg, f"ai {cid}: {shown!r}")

    def _chat_ai_on_reply(self, cid, chat_type=""):
        if not self.cfg.ai_enabled:
            self._record_out(
                cid,
                "AI mode is disabled on this host.\n"
                "Bot owner can enable it with TG_AI_ENABLED=1 in the .env file.",
            )
            return
        if not self.can_sys(cid):
            self._chat_denied_reply(cid, "/ai", "sys")
            return
        if not self._shell_private(cid, chat_type):
            self._record_out(cid, "refused: AI mode only works in a private chat.")
            return
        with self.shell_lock:
            shell_on = cid in self.shell_sessions
        if shell_on:
            self._record_out(
                cid,
                "root shell is ON - send /sh-off before starting AI mode.",
            )
            return
        if self._ai_session(cid) is not None:
            self._record_out(cid, "AI mode is already ON.")
            return
        if not find_pi(self.cfg):
            log(self.cfg, f"/ai unavailable for {cid}: pi executable not found")
            self._record_out(cid, pi_missing_text(self.cfg))
            return

        cwd = read_workdir(self.cfg)
        if not os.path.isdir(cwd):
            cwd = default_workdir(self.cfg)
        try:
            session = AiSession(self, cid, cwd)
        except Exception as exc:  # noqa: BLE001
            log(self.cfg, f"/ai start failed for {cid}: {exc!r}")
            message = str(exc)
            if "AI mode unavailable" in message:
                self._record_out(cid, message)
            else:
                self._record_out(cid, f"could not start AI mode: {exc}")
            return
        with self.ai_lock:
            if cid in self.ai_sessions:
                session.close()
                self._record_out(cid, "AI mode is already ON.")
                return
            self.ai_sessions[cid] = session

        state, _error = session.command("get_state", timeout=AI_START_TIMEOUT)
        session.apply_state(state)
        model = session.current_model or {}
        model_name = model.get("id") or "default"
        if model.get("provider"):
            model_name = f"{model.get('provider')}/{model_name}"
        thinking = session.current_thinking
        lines = [
            "\U0001F916 AI mode ON - pi harness connected.",
            f"cd: {cwd}",
            f"pi: {session.pi}",
            f"model: {model_name}" + (f"  |  thinking {thinking}" if thinking else ""),
            "",
            "Send normal messages here. Thinking and tool calls stay hidden.",
            "/model - pick a model with buttons",
            "/thinking - pick the reasoning level with buttons",
            "/session - resume a saved pi session",
            "/ai-status - token and context usage | /ai-agent - injected rules",
            "/btw <text> - quick side question without touching this session",
            "/live - running pi sessions and subagents in this path",
            "/new - start a fresh pi session | /stop - abort | /compact - compact context",
            "/pi <text> - send text or a pi command explicitly",
            "/status, /logs, /recover still run the server commands",
            "/ai-exit stops AI mode (also /ai_exit).",
        ]
        self._record_out(cid, "\n".join(lines))
        log(self.cfg, f"/ai ON for chat {cid} in {cwd} (model {model_name})")

    def _chat_ai_exit_reply(self, cid):
        session = self._ai_take(cid)
        if session is None:
            self._record_out(cid, "AI mode is already OFF.")
            return
        session.close()
        self._record_out(
            cid,
            f"AI mode OFF. pi stopped.\ncd saved: {read_workdir(self.cfg)}",
        )

    def _chat_ai_help_reply(self, cid):
        self._record_out(
            cid,
            "AI mode (pi harness):\n"
            "  /model       pick a model from buttons\n"
            "  /thinking    pick the reasoning level from buttons\n"
            "  /session     resume a saved pi session from buttons\n"
            "  /ai-status   show context usage and token counts\n"
            "  /ai-agent    show the injected AI rules\n"
            "  /ai-agent-add <text> - add a persistent rule\n"
            "  /ai-agent-remove <n|text> - remove a rule\n"
            "  /btw <text> - quick side question, main session untouched\n"
            "  /live       show running pi sessions and subagents\n"
            "  /new         start a fresh pi session\n"
            "  /stop        abort the current run\n"
            "  /compact     compact the conversation context\n"
            "  /pi <text>   send text or a pi slash command explicitly\n"
            "  /status, /logs, /recover, /cancel_upload - server commands\n"
            "  /ai-exit     leave AI mode (also /ai_exit)\n"
            "Plain messages go to pi. Thinking text and tool calls are hidden.",
        )

    def _chat_ai_new_reply(self, cid):
        session = self._ai_session(cid)
        if session is None:
            self._record_out(cid, "AI mode is OFF - send /ai to start it.")
            return
        data, error = session.command("new_session", timeout=30)
        if error:
            self._record_out(cid, f"AI error: {error}")
            return
        if (data or {}).get("cancelled"):
            self._record_out(cid, "the new session was cancelled by an extension.")
            return
        state, _error = session.command("get_state", timeout=15)
        session.apply_state(state)
        session.buttons.clear()
        self._record_out(cid, "new pi session started.")
        log(self.cfg, f"ai {cid}: new session")

    def _chat_ai_stop_reply(self, cid):
        session = self._ai_session(cid)
        if session is None:
            self._record_out(cid, "AI mode is OFF - send /ai to start it.")
            return
        self._record_out(cid, "stopping....")
        _data, error = session.command("abort", timeout=30)
        if error:
            self._record_out(cid, f"AI error: {error}")

    def _chat_ai_compact_reply(self, cid):
        session = self._ai_session(cid)
        if session is None:
            self._record_out(cid, "AI mode is OFF - send /ai to start it.")
            return
        self._record_out(cid, "compacting context....")
        data, error = session.command("compact", timeout=600)
        if error:
            self._record_out(cid, f"AI error: {error}")
            return
        before = (data or {}).get("tokensBefore")
        after = (data or {}).get("estimatedTokensAfter")
        note = "context compacted."
        if before or after:
            note += f"\ntokens: {before or '?'} -> {after or '?'}"
        self._record_out(cid, note)

    def _pi_session_roots(self, session):
        roots = []
        env = (os.environ.get("PI_CODING_AGENT_SESSION_DIR") or "").strip()
        if env:
            roots.append(os.path.expanduser(env))
        else:
            home = os.path.expanduser("~")
            settings_path = os.path.join(home, ".pi", "agent", "settings.json")
            configured = ""
            try:
                with open(settings_path, encoding="utf-8") as fh:
                    configured = str((json.load(fh) or {}).get("sessionDir") or "").strip()
            except (OSError, ValueError, json.JSONDecodeError):
                configured = ""
            if configured:
                configured = os.path.expanduser(configured)
                if not os.path.isabs(configured):
                    configured = os.path.abspath(os.path.join(session.cwd, configured))
                roots.append(configured)
            roots.append(os.path.join(home, ".pi", "agent", "sessions"))

        current_dir = os.path.dirname(session.current_session_file or "")
        if current_dir and os.path.isdir(current_dir):
            real_current = os.path.realpath(current_dir)
            covered = False
            for root in roots:
                real_root = os.path.realpath(root)
                if real_current == real_root or real_current.startswith(real_root + os.sep):
                    covered = True
                    break
            if not covered:
                roots.append(current_dir)

        out = []
        seen = set()
        for root in roots:
            real = os.path.realpath(root)
            if real in seen:
                continue
            seen.add(real)
            out.append(root)
        return out

    def _chat_ai_sessions_reply(self, cid):
        session = self._ai_session(cid)
        if session is None:
            self._record_out(cid, "AI mode is OFF - send /ai to start it.")
            return
        log(self.cfg, f"ai {cid}: /session picker")
        try:
            sessions = discover_pi_sessions(
                self._pi_session_roots(session), cwd=session.cwd)
        except Exception as exc:  # noqa: BLE001
            log(self.cfg, f"ai {cid}: session scan failed: {exc!r}")
            self._record_out(cid, f"could not read pi sessions: {exc}")
            return
        session.session_list = sessions
        session.session_page = 0
        self._ai_send_session_page(session, 0)

    def _ai_session_label(self, record):
        title = _one_line(record.get("title") or "empty session", 42)
        if len(record.get("title") or "") > 42:
            title = title[:41].rstrip() + "…"
        when = time.strftime("%m-%d %H:%M", time.localtime(record.get("mtime") or 0))
        return f"{title} · {when}"[:58]

    def _ai_current_session_title(self, session):
        current = session.current_session_file or ""
        if current:
            real_current = os.path.realpath(current)
            for record in session.session_list:
                if os.path.realpath(record["path"]) == real_current:
                    return record["title"]
        return session.current_session_name or "new session"

    def _ai_send_session_page(self, session, page, edit_id=None):
        cid = session.cid
        sessions = session.session_list
        total = max(1, (len(sessions) + AI_SESSION_PAGE - 1) // AI_SESSION_PAGE)
        page = max(0, min(int(page), total - 1))
        session.session_page = page
        start = page * AI_SESSION_PAGE
        current = session.current_session_file or ""
        real_current = os.path.realpath(current) if current else ""
        rows = []
        for idx in range(start, min(start + AI_SESSION_PAGE, len(sessions))):
            record = sessions[idx]
            label = self._ai_session_label(record)
            if real_current and os.path.realpath(record["path"]) == real_current:
                label = "\u2713 " + label
            rows.append([{"text": label, "callback_data": f"ais:s:{idx}"}])

        nav = [{"text": "New session", "callback_data": "ais:n"}]
        if page > 0:
            nav.append({"text": "\u2039 Prev", "callback_data": f"ais:p:{page - 1}"})
        nav.append({"text": f"{page + 1}/{total}", "callback_data": "ais:x"})
        if page < total - 1:
            nav.append({"text": "Next \u203a", "callback_data": f"ais:p:{page + 1}"})
        rows.append(nav)

        if sessions:
            text = (
                f"Pi sessions for {session.cwd} - page {page + 1}/{total}\n"
                f"Current: {self._ai_current_session_title(session)}\n"
                "Tap a session to resume it, or start a new one."
            )
        else:
            text = (
                f"No saved pi sessions for {session.cwd}.\n"
                "Send a message or use New session to start one."
            )
        markup = json.dumps({"inline_keyboard": rows})
        if edit_id:
            self._ai_edit(cid, edit_id, text, markup)
        else:
            result = self._record_out(cid, text, reply_markup=markup)
            session.session_message_id = (result or {}).get("message_id")

    def _ai_callback_new_session(self, session, message_id):
        cid = session.cid
        if session.working:
            self._ai_edit(
                cid, message_id,
                "pi is still working - send /stop before starting a new session.")
            return
        _data, error = session.command("new_session", timeout=30)
        if error:
            self._ai_edit(cid, message_id, f"Could not start a new session: {error}",
                          json.dumps({"inline_keyboard": []}))
            return
        if (_data or {}).get("cancelled"):
            self._ai_edit(cid, message_id, "The new session was cancelled by an extension.",
                          json.dumps({"inline_keyboard": []}))
            return
        state, _error = session.command("get_state", timeout=15)
        session.apply_state(state)
        session.buttons.clear()
        self._ai_edit(
            cid, message_id,
            f"New pi session started.\ncd: {session.cwd}",
            json.dumps({"inline_keyboard": []}))
        log(self.cfg, f"ai {cid}: new session from picker")

    def _ai_session_in_use(self, cid, path):
        """Return the other chat id already using this session file."""
        real = os.path.realpath(path)
        with self.ai_lock:
            sessions = list(self.ai_sessions.items())
        for other_cid, other in sessions:
            if other_cid == cid:
                continue
            if not other.alive:
                continue
            current = getattr(other, "current_session_file", "")
            if current and os.path.realpath(current) == real:
                return other_cid
        return None

    def _ai_callback_resume(self, session, index, message_id):
        cid = session.cid
        try:
            record = session.session_list[index]
        except (IndexError, TypeError):
            self._ai_edit(
                cid, message_id,
                "That session is no longer in the list - send /session again.")
            return
        if session.working:
            self._ai_edit(
                cid, message_id,
                "pi is still working - send /stop before switching sessions.")
            return
        path = record["path"]
        if not os.path.isfile(path):
            self._ai_edit(cid, message_id, "That session file is gone - send /session again.")
            return
        cwd = record.get("cwd") or ""
        if cwd and not os.path.isdir(cwd):
            self._ai_edit(
                cid, message_id,
                f"That session's cd no longer exists: {cwd}")
            return
        other = self._ai_session_in_use(cid, path)
        if other:
            self._ai_edit(
                cid, message_id,
                f"That session is already open in chat {other}.\n"
                "Close it there first.")
            return
        if session.current_session_file and \
                os.path.realpath(path) == os.path.realpath(session.current_session_file):
            self._ai_edit(
                cid, message_id,
                f"That is already the current session.\ncd: {session.cwd}")
            return

        result, error = session.command(
            "switch_session", timeout=30, sessionPath=path)
        if error:
            self._ai_edit(
                cid, message_id, f"Could not resume that session: {error}",
                json.dumps({"inline_keyboard": []}))
            return
        if (result or {}).get("cancelled"):
            self._ai_edit(
                cid, message_id, "The session switch was cancelled by an extension.",
                json.dumps({"inline_keyboard": []}))
            return

        state, _error = session.command("get_state", timeout=15)
        session.apply_state(state)
        session.buttons.clear()
        if cwd and os.path.isdir(cwd):
            session.cwd = cwd
            write_workdir(self.cfg, cwd)
        model = session.current_model or {}
        model_name = model.get("id") or "default"
        if model.get("provider"):
            model_name = f"{model.get('provider')}/{model_name}"
        text = (
            f"Session resumed: {record.get('title') or 'session'}\n"
            f"cd: {session.cwd}\n"
            f"model: {model_name}\n"
            f"messages: {session.message_count}"
        )
        self._ai_edit(cid, message_id, text, json.dumps({"inline_keyboard": []}))
        log(self.cfg, f"ai {cid}: resumed session {record.get('id') or path}")

    def _chat_ai_btw_reply(self, cid, question):
        session = self._ai_session(cid)
        if session is None:
            self._record_out(cid, "AI mode is OFF - send /ai to start it.")
            return
        question = (question or "").strip()
        if not question:
            self._record_out(cid, "usage: /btw <side question>")
            return
        if not pi_btw_installed():
            self._record_out(cid, btw_missing_text())
            return

        if not session.current_session_file:
            state, _error = session.command("get_state", timeout=15)
            session.apply_state(state)
        context = pi_session_context(session.current_session_file)
        model = session.current_model or {}
        provider = str(model.get("provider") or "")
        model_id = str(model.get("id") or "")
        self._record_out(cid, "btw: thinking....")
        answer, error = run_pi_side_question(
            session.pi, session.cwd, provider, model_id,
            session.current_thinking or "", question, context,
        )
        if error:
            self._record_out(cid, f"btw error: {error}")
            return
        for chunk in split_message(f"btw:\n{answer}", 3800):
            self._record_out(cid, chunk)
        log(self.cfg, f"ai {cid}: /btw {question[:160]!r}")

    def _chat_ai_live_reply(self, cid):
        session = self._ai_session(cid)
        if session is not None:
            cwd = session.cwd
        else:
            cwd = read_workdir(self.cfg)
            if not os.path.isdir(cwd):
                cwd = default_workdir(self.cfg)

        states = read_live_states(self.cfg)
        processes = find_live_pi_processes(cwd)
        current_pid = None
        if session is not None and session.proc is not None and session.proc.poll() is None:
            current_pid = session.proc.pid
            if not any(item["pid"] == current_pid for item in processes):
                processes.append({
                    "pid": current_pid,
                    "elapsed": max(0.0, time.time() - session.started),
                    "argv": [],
                })
        processes.sort(key=lambda item: (
            0 if current_pid is not None and item["pid"] == current_pid else 1,
            -item["elapsed"],
        ))
        if not processes:
            self._record_out(cid, f"No live pi sessions in {cwd}.")
            return

        lines = [f"Live pi sessions in {cwd}", ""]
        for item in processes[:8]:
            pid = item["pid"]
            state = states.get(pid) or {}
            is_current = current_pid is not None and pid == current_pid
            label = "AI session" if is_current else "pi session"
            lines.append(
                f"\u25cf {label} - {format_duration(item['elapsed'])} (pid {pid})")

            meta = []
            if is_current:
                model = session.current_model or {}
                if model.get("provider"):
                    meta.append(f"{model.get('provider')}/{model.get('id') or 'default'}")
                elif model.get("id"):
                    meta.append(str(model["id"]))
                name = session.current_session_name
                path = session.current_session_file
            else:
                if state.get("model"):
                    meta.append(str(state["model"]))
                name = str(state.get("sessionName") or "")
                path = str(state.get("sessionFile") or "")
            if name:
                meta.append(name)
            elif path:
                meta.append(_short_session_id(path))
            if meta:
                lines.append("  " + " | ".join(meta))

            agents = state.get("agents") or []
            if not isinstance(agents, list):
                agents = []
            agents = [agent for agent in agents if isinstance(agent, dict)]
            agents.sort(key=lambda agent: (
                0 if str(agent.get("status")) in ("running", "queued") else 1,
                float(agent.get("startedAt") or 0),
            ))
            if agents:
                lines.append("  subagents:")
                for agent in agents[:10]:
                    status = str(agent.get("status") or "queued")
                    agent_type = _one_line(agent.get("type") or "agent", 24)
                    started = float(agent.get("startedAt") or 0) / 1000.0
                    completed = float(agent.get("completedAt") or 0) / 1000.0
                    duration = float(agent.get("durationMs") or 0) / 1000.0
                    if status in ("running", "queued"):
                        elapsed = max(0.0, time.time() - started) if started else 0.0
                        mark = "\u25cf" if status == "running" else "\u25cb"
                        detail = f"    {mark} {agent_type} - {status} {format_duration(elapsed)}"
                    else:
                        if not duration and started and completed:
                            duration = max(0.0, completed - started)
                        if status in ("completed", "steered"):
                            detail = f"    \u2713 {agent_type} - done in {format_duration(duration)}"
                        else:
                            detail = (
                                f"    \u2717 {agent_type} - {status} "
                                f"in {format_duration(duration)}")
                    description = _one_line(agent.get("description") or "", 70)
                    if description:
                        detail += f" - {description}"
                    lines.append(detail)
                if len(agents) > 10:
                    lines.append(f"    ... and {len(agents) - 10} more")
            lines.append("")
        if len(processes) > 8:
            lines.append(f"... and {len(processes) - 8} more live sessions")

        for chunk in split_message("\n".join(lines).rstrip(), 3800):
            self._record_out(cid, chunk)

    def _chat_ai_status_reply(self, cid):
        session = self._ai_session(cid)
        if session is None:
            self._record_out(cid, "AI mode is OFF - send /ai to start it.")
            return
        stats, error = session.command("get_session_stats", timeout=20)
        if error:
            self._record_out(cid, f"AI error: {error}")
            return
        state, _error = session.command("get_state", timeout=15)
        session.apply_state(state)
        stats = stats or {}
        tokens = stats.get("tokens") or {}
        context = stats.get("contextUsage") or {}

        def number(value):
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        model = session.current_model or {}
        model_name = model.get("id") or "default"
        if model.get("provider"):
            model_name = f"{model.get('provider')}/{model_name}"
        context_tokens = number(context.get("tokens"))
        context_window = number(context.get("contextWindow"))
        percent = context.get("percent")
        if percent is None and context_window:
            percent = context_tokens * 100.0 / context_window
        if isinstance(percent, (int, float)):
            percent_text = f"{percent:.1f}%"
        else:
            percent_text = "?"
        session_label = session.current_session_id[:8] or "new"
        if session.current_session_name:
            session_label += f" ({session.current_session_name})"
        lines = [
            "AI status",
            f"cd: {session.cwd}",
            f"model: {model_name}",
            f"thinking: {session.current_thinking or 'default'}",
            f"session: {session_label}",
            f"messages: {number(stats.get('userMessages'))} user, "
            f"{number(stats.get('assistantMessages'))} assistant, "
            f"{number(stats.get('toolCalls'))} tool calls",
            f"tokens out: {number(tokens.get('output')):,}  |  "
            f"total: {number(tokens.get('total')):,}",
            f"tokens in: {number(tokens.get('input')):,}  |  "
            f"cache read: {number(tokens.get('cacheRead')):,}  |  "
            f"cache write: {number(tokens.get('cacheWrite')):,}",
            f"context: {context_tokens:,} / {context_window:,} ({percent_text})",
            f"cost: ${float(stats.get('cost') or 0):.4f}",
        ]
        session_file = stats.get("sessionFile") or session.current_session_file
        if session_file:
            lines.append(f"file: {session_file}")
        text = "\n".join(lines)
        self._record_out(
            cid, telegram_code_block(text), parse_mode="HTML", history_text=text)

    def _chat_ai_agent_reply(self, cid):
        default_path = tgmessage_prompt_path()
        try:
            with open(default_path, encoding="utf-8") as fh:
                default_text = fh.read().strip()
        except OSError:
            default_text = f"(missing default rules file: {default_path})"
        rules = load_ai_rules(self.cfg, cid)
        lines = [
            "AI agent rules",
            "",
            f"Default rules: {default_path}",
            default_text,
            "",
            f"Custom rules for this chat: {len(rules)}",
        ]
        if rules:
            for index, rule in enumerate(rules, 1):
                lines.append(f"{index}. {rule}")
        else:
            lines.append("(none)")
        lines += [
            "",
            "Add with /ai-agent-add <rule-text>",
            "Remove with /ai-agent-remove <number|text>",
            "Rules are injected into every new pi session for this chat.",
        ]
        for chunk in split_message("\n".join(lines), 3500):
            self._record_out(cid, chunk)

    def _ai_agent_rule_update(self, cid, note):
        session = self._ai_session(cid)
        if session is None or not session.alive:
            return
        rules = load_ai_rules(self.cfg, cid)
        text = note + "\n\nCurrent custom rules:\n"
        if rules:
            text += "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, 1))
        else:
            text += "(none)"
        self._ai_prompt_async(cid, text)

    def _chat_ai_agent_add_reply(self, cid, rule):
        rule = " ".join(str(rule or "").split())
        if not rule:
            self._record_out(cid, "usage: /ai-agent-add <rule-text>")
            return
        if len(rule) > 2000:
            self._record_out(cid, "that rule is too long (max 2000 characters).")
            return
        rules = load_ai_rules(self.cfg, cid)
        if len(rules) >= 50:
            self._record_out(cid, "this chat already has 50 custom rules - remove one first.")
            return
        if rule in rules:
            self._record_out(cid, "that rule is already in the list.")
            return
        rules.append(rule)
        if not save_ai_rules(self.cfg, cid, rules):
            self._record_out(cid, "could not save the rule - check the state directory.")
            return
        self._record_out(
            cid,
            f"AI agent rule #{len(rules)} added:\n{rule}\n\n"
            "It applies to this session now and is injected into new sessions.",
        )
        self._ai_agent_rule_update(
            cid, f"The operator added persistent AI agent rule #{len(rules)}:\n{rule}")
        log(self.cfg, f"ai {cid}: agent rule added")

    def _chat_ai_agent_remove_reply(self, cid, rule):
        rule = " ".join(str(rule or "").split())
        if not rule:
            self._record_out(cid, "usage: /ai-agent-remove <number|rule-text>")
            return
        rules = load_ai_rules(self.cfg, cid)
        if not rules:
            self._record_out(cid, "this chat has no custom AI agent rules.")
            return
        removed = []
        if rule.isdigit():
            index = int(rule) - 1
            if index < 0 or index >= len(rules):
                self._record_out(cid, f"no rule number {rule} - use /ai-agent to list them.")
                return
            removed = [rules.pop(index)]
        else:
            exact = [item for item in rules if item.lower() == rule.lower()]
            if exact:
                removed = exact
                rules = [item for item in rules if item not in exact]
            else:
                removed = [item for item in rules if rule.lower() in item.lower()]
                rules = [item for item in rules if item not in removed]
        if not removed:
            self._record_out(cid, "no matching rule found - use /ai-agent to list them.")
            return
        if not save_ai_rules(self.cfg, cid, rules):
            self._record_out(cid, "could not save the rules - check the state directory.")
            return
        listing = "\n".join(f"- {item}" for item in removed)
        self._record_out(
            cid,
            f"Removed {len(removed)} AI agent rule(s):\n{listing}\n\n"
            "The change applies now and to new sessions.",
        )
        self._ai_agent_rule_update(
            cid, "The operator removed these persistent AI agent rules:\n" + listing)
        log(self.cfg, f"ai {cid}: {len(removed)} agent rule(s) removed")

    def _chat_ai_model_reply(self, cid):
        session = self._ai_session(cid)
        if session is None:
            self._record_out(cid, "AI mode is OFF - send /ai to start it.")
            return
        data, error = session.command("get_available_models", timeout=30)
        if error:
            self._record_out(cid, f"AI error: {error}")
            return
        models = (data or {}).get("models") or []
        if not models:
            self._record_out(cid, "pi reported no available models.")
            return
        state, _error = session.command("get_state", timeout=15)
        if state:
            session.apply_state(state)
        current_provider = (session.current_model or {}).get("provider")
        session.models = sorted(
            models,
            key=lambda m: (
                0 if m.get("provider") == current_provider else 1,
                str(m.get("provider") or ""),
                str(m.get("name") or m.get("id") or ""),
            ),
        )
        session.model_page = 0
        self._ai_send_model_page(session, 0)

    def _ai_model_label(self, model):
        name = str(model.get("name") or model.get("id") or "model")
        provider = str(model.get("provider") or "")
        label = f"{name} [{provider}]" if provider else name
        return label[:58]

    def _ai_send_model_page(self, session, page, edit_id=None):
        cid = session.cid
        models = session.models
        total = max(1, (len(models) + AI_MODEL_PAGE - 1) // AI_MODEL_PAGE)
        page = max(0, min(int(page), total - 1))
        session.model_page = page
        start = page * AI_MODEL_PAGE
        current = session.current_model or {}
        current_key = (current.get("provider"), current.get("id"))
        rows = []
        for idx in range(start, min(start + AI_MODEL_PAGE, len(models))):
            model = models[idx]
            key = (model.get("provider"), model.get("id"))
            label = ("\u2713 " if key == current_key else "") + self._ai_model_label(model)
            rows.append([{"text": label, "callback_data": f"aim:s:{idx}"}])
        nav = []
        if page > 0:
            nav.append({"text": "\u2039 Prev", "callback_data": f"aim:p:{page - 1}"})
        nav.append({"text": f"{page + 1}/{total}", "callback_data": "aim:x"})
        if page < total - 1:
            nav.append({"text": "Next \u203a", "callback_data": f"aim:p:{page + 1}"})
        rows.append(nav)
        if current.get("id"):
            current_text = f"{current.get('provider')}/{current.get('id')}"
        else:
            current_text = "default"
        text = (
            f"Model picker - page {page + 1}/{total}\n"
            f"Current: {current_text}\n"
            "Tap a model to switch."
        )
        markup = json.dumps({"inline_keyboard": rows})
        if edit_id:
            self._ai_edit(cid, edit_id, text, markup)
        else:
            result = self._record_out(cid, text, reply_markup=markup)
            session.model_message_id = (result or {}).get("message_id")

    def _ai_edit(self, cid, message_id, text, markup=None):
        if not message_id:
            self._record_out(cid, text)
            return
        try:
            self.tg.edit_message_text(
                cid, message_id, text, reply_markup=markup)
        except TelegramError as exc:
            if "not modified" in str(exc).lower():
                return
            log(self.cfg, f"editMessageText {cid} failed: {exc}")
            self._record_out(cid, text)

    def _chat_ai_thinking_reply(self, cid):
        session = self._ai_session(cid)
        if session is None:
            self._record_out(cid, "AI mode is OFF - send /ai to start it.")
            return
        data, error = session.command("get_available_thinking_levels", timeout=20)
        if error:
            self._record_out(cid, f"AI error: {error}")
            return
        levels = (data or {}).get("levels") or []
        if not levels:
            self._record_out(cid, "this model has no thinking levels.")
            return
        state, _error = session.command("get_state", timeout=15)
        if state:
            session.apply_state(state)
        session.think_levels = [str(level) for level in levels]
        rows = []
        row = []
        for idx, level in enumerate(session.think_levels):
            label = ("\u2713 " if level == session.current_thinking else "") + level
            row.append({"text": label, "callback_data": f"aith:s:{idx}"})
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        text = (
            "Thinking level\n"
            f"Current: {session.current_thinking or 'default'}\n"
            "Tap a level to switch."
        )
        self._record_out(
            cid, text, reply_markup=json.dumps({"inline_keyboard": rows}))

    def handle_callback_query(self, callback):
        cb_id = callback.get("id")
        if cb_id:
            try:
                self.tg.answer_callback_query(cb_id)
            except TelegramError as exc:
                log(self.cfg, f"answerCallbackQuery failed: {exc}")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        cid = str(chat.get("id") or (callback.get("from") or {}).get("id") or "")
        data = str(callback.get("data") or "")
        if not cid or not data.startswith("ai"):
            return
        if cid not in self.allowed or not self.can_sys(cid):
            return
        threading.Thread(
            target=self._handle_ai_callback,
            args=(cid, data, message.get("message_id")),
            daemon=True,
        ).start()

    def _ai_control_button(self, session, key, message_id):
        cid = session.cid
        item = session.buttons.pop(key, None)
        if item is None:
            self._record_out(
                cid, "That button has expired. Ask the AI to send a new one.")
            return
        label = str(item.get("label") or "")
        value = str(item.get("value") or label)
        text = str(item.get("text") or "Selection")
        shown = f"{text}\n\nSelected: {label}" if label else text
        if message_id:
            self._ai_edit(
                cid, message_id, shown[:4000],
                json.dumps({"inline_keyboard": []}))
        else:
            self._record_out(cid, shown[:4000])
        if value.strip():
            self._ai_prompt_async(cid, value)
        else:
            self._record_out(cid, "that button had no value.")

    def _handle_ai_callback(self, cid, data, message_id):
        session = self._ai_session(cid)
        if session is None or not session.alive:
            self._record_out(cid, "AI mode is OFF - send /ai to start it.")
            return
        if data.startswith("aitg:"):
            self._ai_control_button(session, data.split(":", 1)[1], message_id)
            return
        if data == "aim:x":
            return
        if data == "ais:x":
            return
        if data.startswith("ais:p:"):
            try:
                page = int(data.split(":", 2)[2])
            except (IndexError, ValueError):
                return
            self._ai_send_session_page(session, page, edit_id=message_id)
            return
        if data == "ais:n":
            self._ai_callback_new_session(session, message_id)
            return
        if data.startswith("ais:s:"):
            try:
                idx = int(data.split(":", 2)[2])
            except (IndexError, ValueError):
                return
            self._ai_callback_resume(session, idx, message_id)
            return
        if data.startswith("aim:p:"):
            try:
                page = int(data.split(":", 2)[2])
            except (IndexError, ValueError):
                return
            self._ai_send_model_page(session, page, edit_id=message_id)
            return
        if data.startswith("aim:s:"):
            try:
                idx = int(data.split(":", 2)[2])
                model = session.models[idx]
            except (IndexError, ValueError):
                self._record_out(cid, "that model is no longer in the list - send /model again.")
                return
            provider = str(model.get("provider") or "")
            model_id = str(model.get("id") or "")
            chosen, error = session.command(
                "set_model", timeout=30, provider=provider, modelId=model_id)
            if error:
                self._ai_edit(
                    cid, message_id, f"Could not switch model: {error}",
                    json.dumps({"inline_keyboard": []}))
                return
            session.current_model = chosen or model
            self._ai_edit(
                cid, message_id, f"Model set: {provider}/{model_id}",
                json.dumps({"inline_keyboard": []}))
            log(self.cfg, f"ai {cid}: model set to {provider}/{model_id}")
            return
        if data.startswith("aith:s:"):
            try:
                idx = int(data.split(":", 2)[2])
                level = session.think_levels[idx]
            except (IndexError, ValueError):
                self._record_out(cid, "that thinking level is gone - send /thinking again.")
                return
            _data, error = session.command(
                "set_thinking_level", timeout=30, level=level)
            if error:
                self._ai_edit(
                    cid, message_id, f"Could not set thinking: {error}",
                    json.dumps({"inline_keyboard": []}))
                return
            session.current_thinking = level
            self._ai_edit(
                cid, message_id, f"Thinking set: {level}",
                json.dumps({"inline_keyboard": []}))
            log(self.cfg, f"ai {cid}: thinking set to {level}")

    # ---- /sh-on: sys-only root shell in a private chat ----------------
    @staticmethod
    def _shell_private(cid, chat_type):
        """Root shell is only allowed in a real private chat, never a group."""
        return chat_type == "private" and looks_like_id(cid) and int(cid) > 0

    def _shell_home(self):
        fallback = os.path.expanduser("~")
        if not os.path.isdir(fallback):
            fallback = self.cfg.home
        return read_workdir(self.cfg) or fallback

    @property
    def _shell_state_path(self):
        return os.path.join(self.cfg.state_dir, SHELL_STATE_FILE)

    def _persist_shell_locked(self):
        """Write active shell sessions to disk; caller holds shell_lock.

        Only the session metadata is stored - never a process handle or a
        command.  The boot id means a service restart keeps the shell on, but
        a full machine reboot does not.
        """
        sessions = {}
        for cid, sess in self.shell_sessions.items():
            if not (looks_like_id(cid) and int(cid) > 0):
                continue
            sessions[cid] = {
                "started": float(sess.get("started") or time.time()),
                "last": float(sess.get("last") or time.time()),
                "cwd": sess.get("cwd") or self._shell_home(),
            }

        path = self._shell_state_path
        if not sessions:
            try:
                os.unlink(path)
            except OSError:
                pass
            return
        data = {"version": 1, "boot_id": current_boot_id(), "sessions": sessions}
        try:
            os.makedirs(self.cfg.state_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
                fh.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            pass

    def _load_shell_state(self):
        """Restore /sh-on sessions after a service restart (same boot only)."""
        path = self._shell_state_path
        if not self.cfg.shell_enabled:
            try:
                os.unlink(path)
            except OSError:
                pass
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, json.JSONDecodeError):
            try:
                os.unlink(path)
            except OSError:
                pass
            return
        if not isinstance(data, dict) or data.get("boot_id") != current_boot_id():
            try:
                os.unlink(path)
            except OSError:
                pass
            return
        stored = data.get("sessions")
        if not isinstance(stored, dict):
            stored = {}

        now = time.time()
        restored = {}
        for cid, rec in stored.items():
            if cid not in self.sys or not (looks_like_id(cid) and int(cid) > 0):
                continue
            if not isinstance(rec, dict):
                continue
            try:
                started = float(rec.get("started") or now)
                last = float(rec.get("last") or started)
            except (TypeError, ValueError):
                continue
            if now - started >= self.cfg.shell_max or now - last >= self.cfg.shell_idle:
                continue
            cwd = str(rec.get("cwd") or "")
            if not os.path.isdir(cwd):
                cwd = self._shell_home()
            restored[cid] = {
                "started": started,
                "last": last,
                "busy": False,
                "proc": None,
                "times": [],
                "cwd": cwd,
            }

        with self.shell_lock:
            self.shell_sessions = restored
            self._persist_shell_locked()
        if restored:
            log(self.cfg, f"restored {len(restored)} active shell session(s) after restart")

    def _stop_shell_commands(self):
        """Kill running commands on shutdown but keep the session state."""
        with self.shell_lock:
            procs = [
                sess.get("proc") for sess in self.shell_sessions.values()
                if sess.get("proc") is not None
            ]
            self._persist_shell_locked()
        for proc in procs:
            kill_process_group(proc)

    def _stop_ai_sessions(self):
        """Stop every pi process when the daemon shuts down."""
        with self.ai_lock:
            sessions = list(self.ai_sessions.values())
            self.ai_sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001
                log(self.cfg, f"closing ai session {session.cid} failed: {exc!r}")

    def _shell_left(self, sess):
        now = time.time()
        return max(0, min(
            sess["last"] + self.cfg.shell_idle - now,
            sess["started"] + self.cfg.shell_max - now,
        ))

    def _shell_close(self, cid):
        """Forget a session and kill the command it is running, if any."""
        with self.shell_lock:
            sess = self.shell_sessions.pop(cid, None)
            proc = sess.get("proc") if sess else None
            self._persist_shell_locked()
        killed = proc is not None and proc.poll() is None
        if proc is not None:
            kill_process_group(proc)
        return sess, killed

    def _shell_mode_take(self, cid, cmd, head, chat_type, edited):
        """Return True when an active shell session owns this message."""
        with self.shell_lock:
            active = cid in self.shell_sessions
        if not active:
            return False
        if head in ("/sh-off", "/sh_off"):
            threading.Thread(target=self._chat_shell_off_reply, args=(cid,), daemon=True).start()
            return True
        if head in ("/sh-on", "/sh_on"):
            threading.Thread(target=self._chat_shell_on_reply, args=(cid, chat_type), daemon=True).start()
            return True
        if head in ("/help", "/start", "/commands",
                    "/status", "/logs", "/cancel_upload", "/recover",
                    "/ai-status", "/ai_status", "/ai-agent", "/ai_agent",
                    "/ai-agent-add", "/ai_agent_add",
                    "/ai-agent-remove", "/ai_agent_remove",
                    "/btw", "/live"):
            return False
        if head in ("/ai", "/ai-exit", "/ai_exit"):
            threading.Thread(
                target=self._record_out,
                args=(cid, "root shell is ON - send /sh-off before using AI mode."),
                daemon=True,
            ).start()
            return True
        # Other slash text is a shell command too, so /bin/ls keeps working.
        self._maybe_shell_input(cid, cmd, chat_type, edited)
        return True

    def _maybe_shell_input(self, cid, text, chat_type="", edited=False):
        with self.shell_lock:
            active = cid in self.shell_sessions
        if not active:
            return
        if edited:
            threading.Thread(
                target=self._record_out,
                args=(cid, "shell: edited messages are ignored - send the command again"),
                daemon=True,
            ).start()
            return
        self._chat_shell_run_async(cid, text, chat_type)

    def _chat_shell_on_reply(self, cid, chat_type=""):
        if not self.cfg.shell_enabled:
            self._shell_close(cid)
            self._record_out(
                cid,
                "root shell is disabled on this host.\n"
                "Bot owner can enable it with: tgmessage shell on",
            )
            return
        if not self.can_sys(cid):
            self._shell_close(cid)
            self._chat_denied_reply(cid, "/sh-on", "sys")
            return
        if not self._shell_private(cid, chat_type):
            self._shell_close(cid)
            self._record_out(cid, "refused: the root shell only works in a private chat.")
            return

        now = time.time()
        created = False
        with self.shell_lock:
            sess = self.shell_sessions.get(cid)
            if sess is not None and self._shell_left(sess) <= 0:
                self.shell_sessions.pop(cid, None)
                sess = None
            if sess is None:
                sess = {
                    "started": now,
                    "last": now,
                    "busy": False,
                    "proc": None,
                    "times": [],
                    "cwd": self._shell_home(),
                }
                self.shell_sessions[cid] = sess
                created = True
            left = self._shell_left(sess)
            busy = sess.get("busy")
            self._persist_shell_locked()

        if not created:
            state = "a command is still running" if busy else "ready"
            self._record_out(
                cid,
                f"root shell is already ON ({state}, {human_duration(left)} left).\n"
                "/sh-off closes it.",
            )
            return

        log(self.cfg, f"/sh-on by chat {cid}")
        host = socket.gethostname()
        user = getpass.getuser()
        uid = os.geteuid()
        who = "root" if uid == 0 else f"{user} (uid {uid}, not root)"
        text = "\n".join([
            f"\U0001F510 root shell ON - {who} @ {host}",
            "Every message in this private chat now runs as a shell command.",
            f"Limits: {human_duration(self.cfg.shell_idle)} idle, "
            f"{human_duration(self.cfg.shell_max)} max, "
            f"{human_duration(self.cfg.shell_timeout)} per command.",
            "Send commands without a leading slash; /status, /logs, /recover and /help still work.",
            "cd is remembered between commands; exported variables are not.",
            "\u26a0 This is full control of the server. Output is stored by Telegram "
            "and in this bot's history.",
            "/sh-off closes the shell and kills a running command.",
        ])
        self._record_out(cid, text)

    def _chat_shell_off_reply(self, cid):
        sess, killed = self._shell_close(cid)
        if sess is None:
            self._record_out(cid, "root shell is already OFF.")
            return
        log(self.cfg, f"/sh-off by chat {cid}" + (" (killed a running command)" if killed else ""))
        note = " - running command killed" if killed else ""
        self._record_out(cid, f"root shell OFF{note}.")

    def _shell_try_begin(self, cid, chat_type):
        """Atomically validate a session and reserve a command slot."""
        with self.shell_lock:
            sess = self.shell_sessions.get(cid)
            if sess is None:
                return None, "root shell is off - send /sh-on to start it."
            if not self.cfg.shell_enabled:
                self.shell_sessions.pop(cid, None)
                self._persist_shell_locked()
                return None, "root shell was disabled by the owner."
            if not self.can_sys(cid):
                self.shell_sessions.pop(cid, None)
                self._persist_shell_locked()
                return None, "permission revoked - root shell closed."
            if not self._shell_private(cid, chat_type):
                self.shell_sessions.pop(cid, None)
                self._persist_shell_locked()
                return None, "root shell closed: it only works in a private chat."
            if sess.get("busy"):
                return None, "a command is still running - wait, or /sh-off to kill it."

            now = time.time()
            if now - sess["started"] >= self.cfg.shell_max:
                self.shell_sessions.pop(cid, None)
                self._persist_shell_locked()
                return None, (
                    f"root shell reached its {human_duration(self.cfg.shell_max)} limit "
                    "- send /sh-on again."
                )
            if now - sess["last"] >= self.cfg.shell_idle:
                self.shell_sessions.pop(cid, None)
                self._persist_shell_locked()
                return None, (
                    f"root shell closed after {human_duration(self.cfg.shell_idle)} idle "
                    "- send /sh-on again."
                )
            times = [t for t in sess["times"] if now - t < SHELL_RATE_WINDOW]
            sess["times"] = times
            if len(times) >= SHELL_RATE_LIMIT:
                return None, (
                    f"shell rate limit: {SHELL_RATE_LIMIT} commands per "
                    f"{int(SHELL_RATE_WINDOW)}s - wait a moment."
                )
            if not self.shell_slots.acquire(blocking=False):
                return None, "too many shell commands are running - try again in a moment."

            sess["busy"] = True
            sess["last"] = now
            times.append(now)
            self._persist_shell_locked()
            return sess, None

    def _chat_shell_run_async(self, cid, command, chat_type=""):
        if len(command) > SHELL_MAX_COMMAND:
            threading.Thread(
                target=self._record_out,
                args=(cid, f"shell: command too long (max {SHELL_MAX_COMMAND} characters)."),
                daemon=True,
            ).start()
            return
        sess, error = self._shell_try_begin(cid, chat_type)
        if error is not None:
            threading.Thread(target=self._record_out, args=(cid, error), daemon=True).start()
            return
        threading.Thread(
            target=self._chat_shell_exec, args=(cid, command, sess), daemon=True).start()

    def _chat_shell_exec(self, cid, command, sess):
        started = time.monotonic()
        cwd = sess.get("cwd") or self._shell_home()
        if not os.path.isdir(cwd):
            cwd = self._shell_home()
            sess["cwd"] = cwd

        def register(proc):
            with self.shell_lock:
                keep = self.shell_sessions.get(cid) is sess
                if keep:
                    sess["proc"] = proc
            if not keep:
                kill_process_group(proc)

        try:
            fd, cwd_file = tempfile.mkstemp(prefix=".shell-cwd-", dir=self.cfg.state_dir)
            os.close(fd)
        except OSError:
            cwd_file = None

        try:
            result = run_shell_command(
                command, cwd, self.cfg.shell_timeout,
                on_start=register, cwd_file=cwd_file)
        except Exception as exc:  # noqa: BLE001
            log(self.cfg, f"shell command crashed: {exc!r}")
            result = {
                "text": f"shell error: {exc!r}",
                "exit": None,
                "timed_out": False,
                "truncated": False,
                "flooded": False,
            }

        new_cwd = None
        if cwd_file:
            try:
                with open(cwd_file, encoding="utf-8", errors="replace") as fh:
                    candidate = fh.read(4096).strip()
                if candidate and os.path.isdir(candidate):
                    new_cwd = candidate
            except OSError:
                pass
            try:
                os.unlink(cwd_file)
            except OSError:
                pass

        elapsed = time.monotonic() - started
        if result.get("flooded"):
            status = f"output flood killed after {human_size(SHELL_MAX_READ)}"
        elif result.get("timed_out"):
            status = f"timed out after {self.cfg.shell_timeout}s"
        elif result.get("exit") is None:
            status = "failed to start"
        else:
            status = f"exit {result['exit']}"

        try:
            with self.shell_lock:
                keep = self.shell_sessions.get(cid) is sess
                if keep and new_cwd:
                    sess["cwd"] = new_cwd
                expired = bool(keep and time.time() - sess["started"] >= self.cfg.shell_max)
            if new_cwd and os.path.isdir(new_cwd):
                # Keep the cd even after /sh-off, so /ai starts there too.
                write_workdir(self.cfg, new_cwd)
            if keep:
                shown = command if len(command) <= 500 else command[:500] + "…"
                text = f"$ {shown}\n{result['text']}\n\n[{status} · {elapsed:.1f}s]"
                if expired:
                    text += (
                        f"\n[shell session reached its {human_duration(self.cfg.shell_max)} "
                        "limit - send /sh-on again]"
                    )
                for chunk in split_message(text):
                    self._record_out(
                        cid,
                        telegram_code_block(chunk),
                        parse_mode="HTML",
                        history_text=chunk,
                    )
            log_cmd = command if len(command) <= 300 else command[:300] + "…"
            log(
                self.cfg,
                f"shell {cid}: {log_cmd!r} -> {status} in {elapsed:.1f}s"
                + ("" if keep else " (session closed, output dropped)"),
            )
        except Exception as exc:  # noqa: BLE001 - never kill the worker thread
            log(self.cfg, f"shell reply for {cid} failed: {exc!r}")
        finally:
            with self.shell_lock:
                if self.shell_sessions.get(cid) is sess:
                    sess["busy"] = False
                    sess["proc"] = None
                    sess["last"] = time.time()
                    if time.time() - sess["started"] >= self.cfg.shell_max:
                        self.shell_sessions.pop(cid, None)
                self._persist_shell_locked()
                self.shell_slots.release()

    # ---- incoming files: saved into <home>/files ----------------------
    def maybe_save_media(self, cid, msg):
        """Queue any photo/file/audio/video for download to files_dir."""
        info = incoming_media(msg)
        if not info:
            return
        file_id, name, size, kind = info
        name = safe_filename(name)
        key = start_download(name, cid, size)
        self.download_queue.put((cid, file_id, name, size, kind, key))

    def _download_worker(self):
        """Download one file at a time, in arrival order (low-end friendly)."""
        try:
            os.nice(10)  # keep large transfers from starving the rest of the box
        except (AttributeError, OSError):
            pass
        while self.running:
            try:
                item = self.download_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            # A cancel restarts the local server; do not start the next file
            # until that restart has finished.
            if getattr(self.cfg, "is_local_api", False):
                while self.running and _local_api_restarting.is_set():
                    time.sleep(0.2)
            try:
                self._save_media(*item)
            except Exception as exc:  # noqa: BLE001 - one file must not kill the queue
                log(self.cfg, f"download worker error: {exc!r}")
            finally:
                self.download_queue.task_done()

    def _save_media(self, cid, file_id, name, size, kind, key=None):
        name = safe_filename(name)
        if key is None:
            key = start_download(name, cid, size)
        dest = None
        try:
            if download_cancelled(key):
                log(self.cfg, f"cancelled queued upload #{key} {name}")
                return
            begin_download(key)
            dest_dir = self.cfg.files_dir
            try:
                os.makedirs(dest_dir, exist_ok=True)
            except OSError as exc:
                log(self.cfg, f"cannot create {dest_dir}: {exc}")
                return
            limit = getattr(self.cfg, "max_download", 20 * 1024 * 1024)
            if size and size > limit:
                if getattr(self.cfg, "is_local_api", False):
                    note = (f"not saved: {name} is {human_size(size)} - the local Bot API "
                            f"limit is {human_size(limit)} per file")
                else:
                    note = (f"not saved: {name} is {human_size(size)} - Telegram's cloud Bot API "
                            f"only lets bots download files up to {human_size(limit)}. "
                            f"Set up the local Bot API server with: tgmessage local-api")
                log(self.cfg, note)
                self._record_out(cid, note)
                return
            monitor_stop = None
            try:
                if download_cancelled(key):
                    raise DownloadCancelled()
                if getattr(self.cfg, "is_local_api", False):
                    baseline = _local_temp_sizes(self.cfg)
                    monitor_stop = threading.Event()
                    threading.Thread(
                        target=_monitor_local_download,
                        args=(self.cfg, key, size or 0, baseline, monitor_stop),
                        daemon=True,
                    ).start()
                try:
                    meta = self.tg.get_file(file_id)
                finally:
                    if monitor_stop is not None:
                        monitor_stop.set()
                server_path = (meta or {}).get("file_path")
                if not server_path:
                    raise TelegramError("getFile returned no file_path")
                if not size:
                    size = (meta or {}).get("file_size")
                    if size:
                        update_download(key, None, size)
                if getattr(self.cfg, "is_local_api", False) and size:
                    # getFile returned, so the local server has the whole file.
                    # The remaining work is only a local link/copy.
                    update_download(key, size, size)
                dest = unique_path(dest_dir, name)
                self.tg.download_file(
                    server_path,
                    dest,
                    progress=lambda done, total: update_download(key, done, total),
                    cancelled=lambda: download_cancelled(key),
                )
                if download_cancelled(key):
                    raise DownloadCancelled()
            except DownloadCancelled:
                try:
                    if dest:
                        os.remove(dest)
                except OSError:
                    pass
                log(self.cfg, f"cancelled upload #{key} {name}")
                return
            except (TelegramError, OSError) as exc:
                if download_cancelled(key):
                    log(self.cfg, f"cancelled upload #{key} {name}")
                    return
                note = f"not saved: {name} ({exc})"
                log(self.cfg, note)
                self._record_out(cid, note)
                return
            saved = os.path.getsize(dest)
            log(self.cfg, f"saved {kind} {name} ({human_size(saved)}) -> {dest}")
            self._record_out(cid, f"saved: {dest} ({human_size(saved)})")
        finally:
            finish_download(key)

    # ---- socket server -----------------------------------------------
    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.cfg.host, self.cfg.port))
        srv.listen(16)
        srv.settimeout(1.0)
        log(self.cfg, f"daemon listening on {self.cfg.host}:{self.cfg.port}")
        while self.running:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.client_loop, args=(conn,), daemon=True).start()
        srv.close()

    def client_loop(self, conn):
        conn.settimeout(None)
        fh = conn.makefile("rb")
        try:
            with self.clients_lock:
                self.clients.add(conn)
            self.send(conn, self.ready_payload())
            if self.default_chat:
                self.send_history(conn, self.default_chat, min(self.cfg.history, 60))
            else:
                self.send(conn, {"type": "history_end"})
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    cmd = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.handle_cmd(conn, cmd)
        except (OSError, ValueError):
            pass
        finally:
            with self.clients_lock:
                self.clients.discard(conn)
            try:
                fh.close()
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def send_history(self, conn, cid, limit):
        self.send(conn, {"type": "history_start", "chat_id": cid})
        for rec in self.history.tail(cid, int(limit)):
            self.send(conn, {"type": "msg", **rec})
        self.send(conn, {"type": "history_end", "chat_id": cid})

    def handle_cmd(self, conn, cmd):
        name = cmd.get("cmd")
        if name == "ping":
            self.send(conn, {"type": "pong"})
            return
        if name == "sessions":
            self.send(conn, {"type": "sessions", "sessions": self.sessions_payload()})
            return
        if name == "requests":
            self.send(conn, {"type": "requests", "requests": self.pending.all()})
            return
        if name == "reload":
            self.reload_config()
            self._announce()
            threading.Thread(target=self.refresh_allowed, daemon=True).start()
            self.send(conn, {"type": "ack", "ok": True})
            return
        if name == "allow":
            cid = str(cmd.get("chat_id") or "").strip()
            if not cid:
                self.send(conn, {"type": "ack", "ok": False, "error": "no chat id given"})
                return
            level = str(cmd.get("level") or "chat").lower()
            if level not in ("chat", "sys", "mang"):
                level = "chat"
            self.do_allow(cid, level)
            self.send(conn, {"type": "ack", "ok": True, "chat_id": cid, "level": level})
            return
        if name == "deny":
            cid = str(cmd.get("chat_id") or "").strip()
            if not cid:
                self.send(conn, {"type": "ack", "ok": False, "error": "no chat id given"})
                return
            level = str(cmd.get("level") or "chat").lower()
            if level not in ("chat", "sys", "mang"):
                level = "chat"
            self.do_deny(cid, level)
            self.send(conn, {"type": "ack", "ok": True, "chat_id": cid, "level": level})
            return
        if name == "history":
            cid = str(cmd.get("chat_id") or self.default_chat or "")
            if not cid:
                self.send(conn, {"type": "history_end"})
                return
            self.send_history(conn, cid, int(cmd.get("limit") or 30))
            return
        if name == "send_file":
            self.handle_send_file(conn, cmd)
            return
        if name != "send":
            return

        text = (cmd.get("text") or "").strip()
        if not text:
            return
        cid = str(cmd.get("chat_id") or self.default_chat or "")
        if not cid:
            self.send(conn, {"type": "ack", "ok": False, "error": "no target chat - use --to, set TG_DEFAULT_CHAT, or allow a chat"})
            return
        if cid not in self.allowed:
            self.send(conn, {"type": "ack", "ok": False, "error": f"chat {cid} is not allowed - allow it with: tgmessage allow {cid}"})
            return
        try:
            result = self.tg.send_message(cid, text)
        except TelegramError as exc:
            log(self.cfg, f"send to {cid} failed: {exc}")
            self.send(conn, {"type": "ack", "ok": False, "error": str(exc)})
            return
        rec = {
            "ts": time.time(),
            "dir": "out",
            "id": (result or {}).get("message_id"),
            "chat_id": cid,
            "text": text,
        }
        self.history.append(cid, rec)
        self.registry.touch(cid, {}, "out")
        self.broadcast({"type": "msg", **rec})
        self.send(conn, {"type": "ack", "ok": True})

    def handle_send_file(self, conn, cmd):
        path = os.path.expanduser((cmd.get("path") or "").strip())
        if not path:
            self.send(conn, {"type": "ack", "ok": False, "error": "no file path given"})
            return
        if not os.path.isfile(path):
            self.send(conn, {"type": "ack", "ok": False, "error": f"no such file: {path}"})
            return
        cid = str(cmd.get("chat_id") or self.default_chat or "")
        if not cid:
            self.send(conn, {"type": "ack", "ok": False, "error": "no target chat - use --to, set TG_DEFAULT_CHAT, or allow a chat"})
            return
        if cid not in self.allowed:
            self.send(conn, {"type": "ack", "ok": False, "error": f"chat {cid} is not allowed - allow it with: tgmessage allow {cid}"})
            return
        size = os.path.getsize(path)
        limit = getattr(self.cfg, "max_upload", MAX_FILE)
        if size > limit:
            self.send(
                conn,
                {
                    "type": "ack",
                    "ok": False,
                    "error": f"file is {human_size(size)}; the Bot API in use allows up to {human_size(limit)}",
                },
            )
            return

        caption = (cmd.get("caption") or "").strip() or None
        try:
            result = self.tg.send_document(cid, path, caption)
        except TelegramError as exc:
            log(self.cfg, f"file send to {cid} failed: {exc}")
            self.send(conn, {"type": "ack", "ok": False, "error": str(exc)})
            return
        except OSError as exc:
            log(self.cfg, f"file read failed: {exc}")
            self.send(conn, {"type": "ack", "ok": False, "error": f"cannot read file: {exc}"})
            return

        name = os.path.basename(path)
        label = f"[file] {name} ({human_size(size)})"
        if caption:
            label += f" - {caption}"
        rec = {
            "ts": time.time(),
            "dir": "out",
            "id": (result or {}).get("message_id"),
            "chat_id": cid,
            "text": label,
            "file": name,
        }
        self.history.append(cid, rec)
        self.registry.touch(cid, {}, "out")
        log(self.cfg, f"sent file {name} ({human_size(size)}) to {cid}")
        self.broadcast({"type": "msg", **rec})
        self.send(conn, {"type": "ack", "ok": True, "file": name})

    def start(self):
        try:
            self.bot = (self.tg.get_me() or {}).get("username")
        except TelegramError as exc:
            log(self.cfg, f"getMe failed yet (will keep trying): {exc}")
        log(
            self.cfg,
            "starting daemon (home={}{}{})".format(
                self.cfg.home,
                f", bot=@{self.bot}" if self.bot else "",
                f", allowed={','.join(sorted(self.allowed, key=id_sort_key))}" if self.allowed else ", no chats allowed yet",
            ),
        )
        self._write_pid()
        threading.Thread(target=self._register_commands, daemon=True).start()
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self._download_worker, daemon=True).start()
        threading.Thread(target=self.refresh_allowed, daemon=True).start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._stop)
        try:
            self.serve()
        finally:
            self._stop_shell_commands()
            self._stop_ai_sessions()
            self._clear_pid()

    def _register_commands(self):
        """Keep the Telegram '/' menu in sync with the commands we support."""
        ok, err = register_bot_commands(self.cfg, self.tg)
        if ok:
            log(self.cfg, f"registered {len(BOT_COMMANDS)} Telegram bot commands")
        else:
            log(self.cfg, f"could not register Telegram bot commands: {err}")

    def _stop(self, *_):
        self.running = False

    def _write_pid(self):
        try:
            with open(self.cfg.pid_path, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass

    def _clear_pid(self):
        try:
            os.unlink(self.cfg.pid_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# terminal line editor (only the standard library)
# --------------------------------------------------------------------------


class Editor:
    """A small raw-mode line editor that can repaint itself when a message
    arrives while you are still typing."""

    def __init__(self, prompt="Me> ", colours=None):
        self.c = colours or C
        self.prompt = prompt
        self.buf = []
        self.cursor = 0
        self.prev_rows = 0
        self.history = []
        self.hidx = None
        self.stash = ""
        self.state = 0  # 0 normal, 1 saw ESC, 2 CSI params, 3 SS3
        self.params = ""
        self.paste = None  # not None while inside a bracketed paste

    # ---- low level ----
    @staticmethod
    def width():
        return shutil.get_terminal_size((80, 24)).columns or 80

    @staticmethod
    def _w(text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def _clear(self):
        if self.prev_rows > 1:
            self._w(f"\033[{self.prev_rows - 1}A")
        self._w("\r\033[J")
        self.prev_rows = 0

    # ---- painting ----
    def render(self):
        self._clear()
        text = "".join(self.buf)
        full = self.prompt + text
        w = self.width()
        rows = max(1, -(-len(full) // w))
        self._w(f"{self.c['cyan']}{self.c['b']}{self.prompt}{self.c['rst']}{text}")

        abs_col = len(self.prompt) + self.cursor
        target_row, target_col = abs_col // w, abs_col % w
        move = ""
        down = (rows - 1) - target_row
        if down > 0:
            move += f"\033[{down}A"
        move += "\r"
        if target_col:
            move += f"\033[{target_col}C"
        self._w(move)
        self.prev_rows = rows

    def print_msg(self, tag, colour, text, ts=None):
        self._clear()
        w = self.width()
        body = str(text).replace("\r", "")
        lines = []
        for para in body.split("\n"):
            lines.extend(textwrap.wrap(para, max(10, w - len(tag) - 1)) or [""])
        for i, line in enumerate(lines):
            arrow = tag if i == 0 else " " * len(tag)
            stamp = ""
            if i == 0 and ts:
                stamp = f"{self.c['dim']}[{time.strftime('%H:%M', time.localtime(ts))}]{self.c['rst']} "
            self._w(f"{colour}{self.c['b']}{arrow}{self.c['rst']}{stamp}{line}\n")
        self.render()

    def print_note(self, text, colour=None):
        self._clear()
        self._w(f"{(colour or self.c['yellow'])}{text}{self.c['rst']}\n")
        self.render()

    # ---- editing ----
    def backspace(self):
        if self.cursor > 0:
            del self.buf[self.cursor - 1]
            self.cursor -= 1
            self.render()

    def delete(self):
        if self.cursor < len(self.buf):
            del self.buf[self.cursor]
            self.render()

    def kill_word(self):
        while self.cursor > 0 and self.buf[self.cursor - 1] == " ":
            del self.buf[self.cursor - 1]
            self.cursor -= 1
        while self.cursor > 0 and self.buf[self.cursor - 1] != " ":
            del self.buf[self.cursor - 1]
            self.cursor -= 1
        self.render()

    def history_move(self, step):
        if not self.history:
            return
        if self.hidx is None:
            self.stash = "".join(self.buf)
            self.hidx = len(self.history)
        self.hidx = max(0, min(len(self.history), self.hidx + step))
        if self.hidx == len(self.history):
            new = self.stash
        else:
            new = self.history[self.hidx]
        self.buf = list(new)
        self.cursor = len(self.buf)
        self.render()

    def insert_text(self, text):
        """Insert a chunk (a paste) at the cursor. The line editor is
        single-line, so newlines become spaces. For a multi-line message,
        use:  tgmessage -f file.txt   or   cat file.txt | tgmessage"""
        text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        for ch in text:
            if ch >= " ":
                self.buf.insert(self.cursor, ch)
                self.cursor += 1
        self.render()

    def _csi(self, params, final):
        """Handle a completed CSI sequence, e.g. ESC [ A or ESC [ 2 0 0 ~."""
        if final == "~":
            if params == "200":  # bracketed paste starts
                self.paste = ""
            elif params == "201":  # ...and ends (see feed)
                self.paste = None
            elif params == "3":
                self.delete()
            return
        if final == "A":
            self.history_move(-1)
        elif final == "B":
            self.history_move(1)
        elif final == "C":
            self.cursor = min(len(self.buf), self.cursor + 1)
            self.render()
        elif final == "D":
            self.cursor = max(0, self.cursor - 1)
            self.render()
        elif final == "H":
            self.cursor = 0
            self.render()
        elif final == "F":
            self.cursor = len(self.buf)
            self.render()

    # ---- key feed ----
    def feed(self, data):
        """Return ('submit', text) / ('quit', None) / ('copy', None), else None."""
        text = data.decode("utf-8", "replace")
        for ch in text:
            if self.paste is not None:
                self.paste += ch
                if self.paste.endswith("\x1b[201~"):
                    body = self.paste[:-6]
                    self.paste = None
                    self.insert_text(body)
                continue

            if self.state == 1:  # just saw ESC
                if ch == "[":
                    self.state, self.params = 2, ""
                elif ch == "O":
                    self.state, self.params = 3, ""
                else:
                    self.state = 0
                continue
            if self.state == 2:  # inside a CSI: collect params until a final byte
                if "@" <= ch <= "~":
                    self.state = 0
                    self._csi(self.params, ch)
                else:
                    self.params += ch
                continue
            if self.state == 3:  # SS3, e.g. ESC O H
                self.state = 0
                self._csi("", ch)
                continue

            if ch == "\x1b":
                self.state = 1
            elif ch in ("\r", "\n"):
                line = "".join(self.buf)
                self._clear()
                self.buf, self.cursor, self.hidx = [], 0, None
                if line.strip():
                    self.history.append(line)
                    self.history = self.history[-200:]
                return ("submit", line)
            elif ch == "\x03":  # Ctrl+C
                return ("quit", None)
            elif ch == "\x19":  # Ctrl+Y
                return ("copy", None)
            elif ch == "\x04":  # Ctrl+D
                if not self.buf:
                    return ("quit", None)
                self.delete()
            elif ch in ("\x7f", "\x08"):
                self.backspace()
            elif ch == "\x01":
                self.cursor = 0
                self.render()
            elif ch == "\x05":
                self.cursor = len(self.buf)
                self.render()
            elif ch == "\x15":
                self.buf, self.cursor = [], 0
                self.render()
            elif ch == "\x0b":
                del self.buf[self.cursor :]
                self.render()
            elif ch == "\x17":
                self.kill_word()
            elif ch == "\t":
                continue
            elif ch < " ":
                continue
            else:
                self.buf.insert(self.cursor, ch)
                self.cursor += 1
                self.render()
        return None


class RawTerminal:
    """Put stdin in raw mode; always restore it on the way out."""

    def __init__(self):
        self.fd = None
        self.saved = None

    def __enter__(self):
        import termios

        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        raw = termios.tcgetattr(self.fd)
        raw[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
        raw[6][termios.VMIN] = 0
        raw[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSADRAIN, raw)
        # ask the terminal to wrap pastes in ESC[200~ .. ESC[201~ so a
        # multi-line paste does not submit a message per line
        sys.stdout.write("\033[?2004h")
        sys.stdout.flush()
        return self

    def __exit__(self, *_):
        import termios

        sys.stdout.write("\033[?2004l")
        sys.stdout.flush()
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        sys.stdout.write("\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------
# the interactive client
# --------------------------------------------------------------------------


class ChatClient:
    def __init__(self, cfg, colours=None, chat=None):
        self.cfg = cfg
        self.c = colours or C
        self.sock = None
        self.recv_buf = b""
        self.editor = Editor("Me> ", self.c)
        self.bot = None
        self.target = str(chat or cfg.default_chat or "")
        self.active = self.target
        self.allowed = set(cfg.allowed)
        self.names = {}
        self.pending = []
        self.sessions = []
        self.messages = {}  # chat_id -> [ {dir,text}, ... ] for /copy
        self.last_list = []
        self.replaying = None
        self.expect = None
        self._sync_names()
        self._set_prompt()

    # ---- helpers ----
    def label(self, cid):
        if not cid:
            return ""
        meta = self.names.get(str(cid)) or {}
        if meta.get("username"):
            return "@" + meta["username"]
        return meta.get("name") or str(cid)

    def _sync_names(self):
        for sess in self.sessions:
            cid = str(sess.get("chat_id"))
            self.names[cid] = {"name": sess.get("name") or cid, "username": sess.get("username") or ""}
        for req in self.pending:
            cid = str(req.get("chat_id"))
            self.names[cid] = {"name": req.get("name") or cid, "username": req.get("username") or ""}

    def _set_prompt(self):
        if self.active:
            self.editor.prompt = f"Me[{self.label(self.active)}]> "
        else:
            self.editor.prompt = "Me> "

    def remember(self, obj):
        cid = str(obj.get("chat_id") or self.active or "")
        buf = self.messages.setdefault(cid, [])
        buf.append({"dir": obj.get("dir"), "text": obj.get("text", "")})
        if len(buf) > 500:
            del buf[:-500]

    # ---- connection ----
    def connect(self):
        try:
            self.sock = socket.create_connection((self.cfg.host, self.cfg.port), timeout=5)
        except OSError as exc:
            return f"daemon not reachable on {self.cfg.host}:{self.cfg.port} ({exc})"
        self.sock.sendall(b'{"cmd":"hello"}\n')
        return None

    def send_json(self, obj):
        try:
            self.sock.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass

    def _readline(self, timeout):
        self.sock.settimeout(timeout)
        try:
            while b"\n" not in self.recv_buf:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return None
                self.recv_buf += chunk
        except (socket.timeout, TimeoutError):
            return "\x00timeout"
        except OSError:
            return None
        line, _, self.recv_buf = self.recv_buf.partition(b"\n")
        return line

    def prime(self):
        """Read the ready + backlog burst before taking over the screen."""
        backlog = []
        server_default = ""
        deadline = time.time() + 3
        while time.time() < deadline:
            line = self._readline(max(0.2, deadline - time.time()))
            if line is None or line == "\x00timeout":
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = obj.get("type")
            if kind == "ready":
                self.bot = obj.get("bot")
                self.allowed = set(obj.get("allowed") or self.allowed)
                server_default = str(obj.get("default_chat") or "")
                self.sessions = obj.get("sessions") or []
                self.pending = obj.get("pendings") or []
                self._sync_names()
                if not self.active:
                    self.active = server_default
                self._set_prompt()
            elif kind == "msg":
                backlog.append(obj)
            elif kind == "history_end":
                break
        # if we are pointed at a non-default chat, pull its backlog explicitly
        if self.active and server_default and self.active != server_default:
            self.send_json({"cmd": "history", "chat_id": self.active, "limit": min(self.cfg.history, 60)})
            while time.time() < deadline + 2:
                line = self._readline(max(0.2, deadline + 2 - time.time()))
                if line is None or line == "\x00timeout":
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "msg":
                    backlog.append(obj)
                elif obj.get("type") == "history_end":
                    break
        return backlog

    def banner(self):
        c = self.c
        who = f"@{self.bot}" if self.bot else "bot"
        chat = self.label(self.active) if self.active else "no session yet"
        width = max(52, min(self.editor.width(), 72))
        inner = width - 4
        rows = [
            f"tgmessage  {who}",
            f"session {chat}   port {self.cfg.port}",
            f"sessions {len([s for s in self.sessions if s.get('allowed')])}   allowed"
            f"{'  requests ' + str(len(self.pending)) if self.pending else ''}",
            "Enter sends | Ctrl+C quits | Ctrl+Y copies last reply",
            "/session picks a chat | /copy [n|all] | /file <path>",
            "// sends a literal / | /allow <id> | /requests",
        ]
        out = [f"{c['dim']}\u256d\u2500" + "\u2500" * inner + f"\u2500\u256e{c['rst']}"]
        for row in rows:
            out.append(f"{c['dim']}\u2502{c['rst']} {row[:inner].ljust(inner)} {c['dim']}\u2502{c['rst']}")
        out.append(f"{c['dim']}\u2570\u2500" + "\u2500" * inner + f"\u2500\u256f{c['rst']}")
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()

    def print_message(self, obj, replay=False):
        c = self.c
        direction = obj.get("dir")
        cid = str(obj.get("chat_id") or self.active or "")
        tag = ""
        if not replay and cid and cid != self.active:
            tag = f"[{self.label(cid)}] "
        if direction == "in":
            self.editor.print_msg(f"{tag}Bot> ", c["green"], obj.get("text", ""), obj.get("ts"))
        else:
            self.editor.print_msg(f"{tag}Me> ", c["cyan"], obj.get("text", ""), obj.get("ts"))

    def handle_server_obj(self, obj):
        kind = obj.get("type")
        c = self.c
        if kind == "msg":
            self.remember(obj)
            cid = str(obj.get("chat_id") or self.active or "")
            replay = self.replaying is not None and cid == self.replaying
            self.print_message(obj, replay=replay)
        elif kind == "ack":
            if not obj.get("ok"):
                self.editor.print_note(f"! {obj.get('error', 'send failed')}", c["red"])
        elif kind == "request":
            cid = str(obj.get("chat_id"))
            self.pending = [p for p in self.pending if str(p.get("chat_id")) != cid]
            self.pending.append(
                {"chat_id": cid, "name": obj.get("name"), "username": obj.get("username"), "last_text": obj.get("text")}
            )
            self._sync_names()
            self.editor.print_note(
                f"* request from {self.label(cid)} ({cid}) - /allow {cid}", c["yellow"]
            )
        elif kind == "sessions":
            self.sessions = obj.get("sessions") or []
            self._sync_names()
            if self.expect == "sessions":
                self.expect = None
                self.print_sessions()
            self._set_prompt()
        elif kind == "requests":
            self.pending = obj.get("requests") or []
            self._sync_names()
            if self.expect == "requests":
                self.expect = None
                self.print_requests()
        elif kind == "allowed":
            self.allowed = set(obj.get("allowed") or [])
            if self.active and self.active not in self.allowed and self.allowed:
                self.switch(sorted(self.allowed, key=id_sort_key)[0])
        elif kind == "history_start":
            cid = str(obj.get("chat_id"))
            self.replaying = cid
            self.messages[cid] = []
            if cid != self.active:
                self.active = cid
                self._set_prompt()
            self.editor.print_note(f"--- session {self.label(cid)} ({cid}) ---", c["dim"])
        elif kind == "history_end":
            self.replaying = None

    # ---- session list ----
    def print_sessions(self):
        c = self.c
        allowed = [s for s in self.sessions if s.get("allowed")]
        others = [s for s in self.sessions if not s.get("allowed")]
        self.last_list = [str(s.get("chat_id")) for s in allowed]
        lines = [f"{c['b']}sessions{c['rst']}"]
        if not allowed:
            lines.append(f"  {c['yellow']}(none yet - allow a chat with /allow <id>){c['rst']}")
        for i, sess in enumerate(allowed, 1):
            cid = str(sess.get("chat_id"))
            mark = "*" if cid == self.active else " "
            label = f"@{sess['username']}" if sess.get("username") else (sess.get("name") or cid)
            real = sess.get("name") or ""
            extra = f"  {c['dim']}{real}{c['rst']}" if real and real != label.lstrip("@") else ""
            tags = ""
            if sess.get("mang"):
                tags = f"  {c['yellow']}[mang]{c['rst']}"
            elif sess.get("sys"):
                tags = f"  {c['yellow']}[sys]{c['rst']}"
            lines.append(
                f" {mark} {i:>2}. {label}{extra}{tags}  {c['dim']}{sess.get('count', 0)} msgs{c['rst']}"
            )
        pending = [s for s in others if s.get("pending")] or [
            {"chat_id": p.get("chat_id"), "name": p.get("name"), "username": p.get("username")}
            for p in self.pending
        ]
        if pending:
            lines.append(f"{c['yellow']}requests (not allowed yet){c['rst']}")
            for req in pending:
                cid = str(req.get("chat_id"))
                label = f"@{req['username']}" if req.get("username") else (req.get("name") or cid)
                lines.append(f"      {label}  {c['dim']}{cid}{c['rst']}  /allow {cid}")
        lines.append(f"{c['dim']}switch: /session <n|chatid>{c['rst']}")
        self.editor.print_note("\n".join(lines), c["cyan"])

    def print_requests(self):
        c = self.c
        if not self.pending:
            self.editor.print_note("no pending requests", c["yellow"])
            return
        lines = [f"{c['b']}requests{c['rst']}"]
        for req in self.pending:
            cid = str(req.get("chat_id"))
            label = f"@{req['username']}" if req.get("username") else (req.get("name") or cid)
            preview = (req.get("last_text") or "")[:50]
            lines.append(f"  {label}  {c['dim']}{cid}{c['rst']}  {preview}  /allow {cid}")
        self.editor.print_note("\n".join(lines), c["cyan"])

    def switch(self, cid):
        cid = str(cid)
        if cid not in self.allowed:
            self.editor.print_note(f"! {cid} is not allowed - /allow {cid} first", self.c["yellow"])
            return
        self.active = cid
        self._set_prompt()
        self.send_json({"cmd": "history", "chat_id": cid, "limit": min(self.cfg.history, 60)})

    # ---- copy ----
    def do_copy(self, arg):
        c = self.c
        msgs = self.messages.get(self.active, [])
        if arg == "all":
            if not msgs:
                self.editor.print_note("! nothing copied yet", c["yellow"])
                return
            body = "\n".join(
                f"{'Bot' if m['dir'] == 'in' else 'Me'}> {m['text']}" for m in msgs
            )
        elif arg.isdigit():
            n = int(arg)
            if n < 1 or n > len(msgs):
                self.editor.print_note(f"! only {len(msgs)} messages in the buffer", c["yellow"])
                return
            body = msgs[-n]["text"]
        else:  # "last" / default: newest message from the bot, else newest overall
            picks = [m for m in msgs if m["dir"] == "in"] or msgs
            if not picks:
                self.editor.print_note("! nothing copied yet", c["yellow"])
                return
            body = picks[-1]["text"]
        if not body:
            self.editor.print_note("! that message is empty", c["yellow"])
            return
        note = osc52(body)
        preview = body if len(body) <= 40 else body[:37] + "..."
        self.editor.print_note(f"* copied {len(body)} chars{note}: {preview}", c["green"])
        return True

    def handle_submit(self, line):
        """Returns True when the line was a local command, not a message."""
        if line.startswith("//"):  # escape: send a literal leading slash
            self.send_json({"cmd": "send", "chat_id": self.active, "text": line[1:]})
            return True
        parts = line.split()
        if not parts:
            return True
        head = parts[0]
        if head in ("/session", "/sessions", "/s", "/switch"):
            if len(parts) == 1:
                self.expect = "sessions"
                self.send_json({"cmd": "sessions"})
            elif len(parts) == 2:
                arg = parts[1]
                if arg.isdigit() and self.last_list and 1 <= int(arg) <= len(self.last_list):
                    self.switch(self.last_list[int(arg) - 1])
                elif arg.isdigit():
                    self.editor.print_note("! no session list yet - run /session first", self.c["yellow"])
                else:
                    self.switch(arg)
            return True
        if head == "/requests":
            self.expect = "requests"
            self.send_json({"cmd": "requests"})
            return True
        if head in ("/allow", "/deny"):
            args = parts[1:]
            level = "chat"
            if args and args[0].lower() in ("sys", "mang", "manage"):
                level = "mang" if args[0].lower() in ("mang", "manage") else "sys"
                args = args[1:]
            if len(args) == 1:
                verb = "allow" if head == "/allow" else "deny"
                self.send_json({"cmd": verb, "chat_id": args[0], "level": level})
                self.editor.print_note(f"* {verb} {level} {args[0]}", self.c["yellow"])
                return True
        if (
            head == "/copy"
            and len(parts) <= 2
            and (len(parts) == 1 or parts[1].isdigit() or parts[1] == "all")
        ):
            self.do_copy(parts[1] if len(parts) > 1 else "last")
            return True
        if head in ("/file", "/sendfile"):
            try:
                toks = shlex.split(line)[1:]
            except ValueError:
                toks = parts[1:]
            if not toks:
                self.editor.print_note("! usage: /file <path> [caption]", self.c["yellow"])
                return True
            if not self.active:
                self.editor.print_note("! no session yet - /session to pick one", self.c["yellow"])
                return True
            path = os.path.abspath(os.path.expanduser(toks[0]))
            self.send_json(
                {"cmd": "send_file", "chat_id": self.active, "path": path, "caption": " ".join(toks[1:])}
            )
            self.editor.print_note(f"* sending {os.path.basename(path)} to {self.label(self.active)} ...", self.c["yellow"])
            return True
        return False

    # ---- interactive ----
    def run(self):
        err = self.connect()
        if err:
            print(f"{self.c['red']}! {err}{self.c['rst']}")
            print(self.daemon_hint())
            return 1
        backlog = self.prime()
        self.banner()
        for rec in backlog:
            self.remember(rec)
            self.print_message(rec)
        if not self.active:
            self.editor.print_note(
                "* no session yet - ask someone to message the bot, then /requests and /allow <id>",
                self.c["yellow"],
            )

        with RawTerminal() as term:
            self.editor.render()
            while True:
                try:
                    ready, _, _ = select.select([term.fd, self.sock], [], [])
                except (OSError, ValueError):
                    break
                if term.fd in ready:
                    data = os.read(term.fd, 4096)
                    if not data:
                        break
                    result = self.editor.feed(data)
                    if result and result[0] == "quit":
                        break
                    if result and result[0] == "copy":
                        self.do_copy("last")
                    if result and result[0] == "submit":
                        text = result[1].strip()
                        if text and not self.handle_submit(text):
                            self.send_json({"cmd": "send", "chat_id": self.active, "text": text})
                if self.sock in ready:
                    try:
                        chunk = self.sock.recv(65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        self.editor.print_note("! daemon closed the connection", self.c["red"])
                        time.sleep(0.4)
                        break
                    self.recv_buf += chunk
                    while b"\n" in self.recv_buf:
                        line, _, self.recv_buf = self.recv_buf.partition(b"\n")
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self.handle_server_obj(obj)
        print(f"{self.c['dim']}tgmessage: bye{C['rst']}")
        return 0

    def daemon_hint(self):
        c = self.c
        return (
            f"{c['dim']}start it with:{c['rst']}\n"
            f"  sudo systemctl start {SERVICE_NAME}        {c['dim']}# if installed as a service{c['rst']}\n"
            f"  tgmessage up                       {c['dim']}# otherwise{c['rst']}"
        )

    # ---- piped / non-tty ----
    def run_pipe(self):
        if not self.target:
            print("! no target chat - set TG_DEFAULT_CHAT, allow a chat, or pass --to <chatid>", file=sys.stderr)
            return 1
        err = self.connect()
        if err:
            print(f"! {err}", file=sys.stderr)
            return 1
        self.prime()
        self.active = self.target
        rc = 0
        for raw in sys.stdin:
            text = raw.rstrip("\n")
            if not text.strip():
                continue
            self.send_json({"cmd": "send", "chat_id": self.target, "text": text})
            ack = None
            deadline = time.time() + 30
            while time.time() < deadline:
                line = self._readline(max(0.5, deadline - time.time()))
                if line is None or line == "\x00timeout":
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "ack":
                    ack = obj
                    break
            if ack is None:
                print("! no answer from daemon", file=sys.stderr)
                rc = 1
            elif not ack.get("ok"):
                print(f"! {ack.get('error')}", file=sys.stderr)
                rc = 1
            else:
                print("sent")
        return rc

    # ---- file send (used by `--FILE` and the `file` command) ----
    def send_file(self, path, caption=None):
        if not self.target:
            print("! no target chat - set TG_DEFAULT_CHAT, allow a chat, or pass --to <chatid>", file=sys.stderr)
            return 1
        err = self.connect()
        if err:
            print(f"! {err}", file=sys.stderr)
            print(self.daemon_hint())
            return 1
        self.prime()
        self.send_json(
            {"cmd": "send_file", "chat_id": self.target, "path": path, "caption": caption or ""}
        )
        ack = None
        deadline = time.time() + 300
        while time.time() < deadline:
            line = self._readline(max(0.5, deadline - time.time()))
            if line is None or line == "\x00timeout":
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "ack":
                ack = obj
                break
        if ack is None:
            print("! no answer from daemon", file=sys.stderr)
            return 1
        if not ack.get("ok"):
            print(f"! {ack.get('error')}", file=sys.stderr)
            return 1
        print(f"sent {os.path.basename(path)}")
        return 0


# --------------------------------------------------------------------------
# service helpers
# --------------------------------------------------------------------------


def systemctl(*args, check=False):
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, check=check
    )


def has_systemd():
    return bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")


def unit_installed():
    if not has_systemd():
        return False
    return systemctl("list-unit-files", f"{SERVICE_NAME}.service").stdout.find(SERVICE_NAME) >= 0


def daemon_pid(cfg):
    try:
        with open(cfg.pid_path, encoding="utf-8") as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def reachable(cfg, timeout=1.5):
    try:
        with socket.create_connection((cfg.host, cfg.port), timeout=timeout):
            return True
    except OSError:
        return False


def notify_daemon(cfg, obj, timeout=2.0):
    """Fire-and-forget a command at a running daemon. True if it was sent."""
    if not reachable(cfg, timeout=timeout):
        return False
    try:
        with socket.create_connection((cfg.host, cfg.port), timeout=timeout) as sock:
            sock.sendall((json.dumps(obj) + "\n").encode())
            sock.settimeout(timeout)
            try:
                sock.recv(65536)  # drain the ready burst, best effort
            except OSError:
                pass
        return True
    except OSError:
        return False


def service_start(cfg):
    if unit_installed():
        systemctl("start", f"{SERVICE_NAME}.service")
        return "systemd"
    if daemon_pid(cfg):
        return "already running"
    python = sys.executable or "python3"
    console = open(os.path.join(cfg.state_dir, "daemon.console.log"), "ab")
    child_env = {
        **os.environ,
        "TG_HOME": cfg.home,
        "TG_HOST": cfg.host,
        "TG_PORT": str(cfg.port),
    }
    if cfg.token:
        child_env["TG_BOT_TOKEN"] = cfg.token
    subprocess.Popen(
        [python, os.path.abspath(__file__), "daemon"],
        stdout=console,
        stderr=console,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=cfg.home,
        env=child_env,
    )
    for _ in range(40):
        time.sleep(0.1)
        if reachable(cfg):
            break
    return "nohup"


def service_stop(cfg):
    if unit_installed():
        systemctl("stop", f"{SERVICE_NAME}.service")
        return "systemd"
    pid = daemon_pid(cfg)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(30):
            time.sleep(0.1)
            if daemon_pid(cfg) is None:
                break
        return "killed"
    return "not running"


def service_restart(cfg):
    if unit_installed():
        systemctl("daemon-reload")
        systemctl("restart", f"{SERVICE_NAME}.service")
        return "systemd"
    service_stop(cfg)
    return service_start(cfg)


# --------------------------------------------------------------------------
# local Bot API server (lifts the 20 MB download limit)
# --------------------------------------------------------------------------


def local_api_installed():
    """Path to a usable telegram-bot-api binary, or None."""
    binary = shutil.which("telegram-bot-api")
    if binary:
        return binary
    if os.path.isfile(LOCAL_API_BIN) and os.access(LOCAL_API_BIN, os.X_OK):
        return LOCAL_API_BIN
    return None


def local_api_bin():
    return local_api_installed() or LOCAL_API_BIN


def _print_log_tail(path, count=12):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-count:]
    except OSError:
        return
    if not lines:
        return
    print("  last build output:", file=sys.stderr)
    for line in lines:
        print(f"    {line.rstrip()[:220]}", file=sys.stderr)


def _run_stream(cmd, log_fh, title="", progress=False):
    """Run a command, stream filtered progress, and keep a full log."""
    if title:
        print(title, flush=True)
    try:
        log_fh.write(f"\n$ {' '.join(shlex.quote(str(x)) for x in cmd)}\n")
        log_fh.flush()
    except OSError:
        pass
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
    except OSError as exc:
        print(f"! cannot run {cmd[0]}: {exc}", file=sys.stderr)
        return 127
    started = time.time()
    last_pct = -1
    last_shown = 0.0
    line_q: queue.Queue = queue.Queue()

    def _read_lines():
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line_q.put(line)
        finally:
            line_q.put(None)

    threading.Thread(target=_read_lines, daemon=True).start()
    try:
        while True:
            try:
                raw = line_q.get(timeout=5)
            except queue.Empty:
                if progress and time.time() - last_shown >= 30:
                    print(f"  ... still building ({int(time.time() - started)}s)", flush=True)
                    last_shown = time.time()
                continue
            if raw is None:
                break
            try:
                log_fh.write(raw)
                log_fh.flush()
            except OSError:
                pass
            line = raw.rstrip("\n")
            if not progress:
                if line:
                    print(line, flush=True)
                continue
            pct = None
            text = ""
            match = re.match(r"^\[\s*(\d+)%\]\s*(.*)$", line)
            if match:
                pct = int(match.group(1))
                text = match.group(2)
            else:
                match = re.match(r"^\[\s*(\d+)/(\d+)\]\s*(.*)$", line)
                if match:
                    total = int(match.group(2)) or 1
                    pct = int(match.group(1)) * 100 // total
                    text = match.group(3)
            if pct is not None:
                now = time.time()
                if pct != last_pct and (pct >= 100 or pct % 5 == 0 or now - last_shown >= 30):
                    elapsed = int(now - started)
                    print(f"  {pct:3d}%  {elapsed:4d}s  {text[:60]}", flush=True)
                    last_pct = pct
                    last_shown = now
            elif "error" in line.lower() or "warning" in line.lower():
                print(f"  {line[:200]}", flush=True)
            elif "installing:" in line.lower() or "built target" in line.lower():
                print(f"  {line[:120]}", flush=True)
        return proc.wait()
    except KeyboardInterrupt:
        print("\n! interrupted; stopping the build ...", file=sys.stderr, flush=True)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        return 130


def _local_api_missing_deps():
    missing = []
    for tool, package in (("cmake", "cmake"), ("g++", "g++"),
                          ("gperf", "gperf"), ("git", "git")):
        if not shutil.which(tool):
            missing.append(package)
    if not os.path.exists("/usr/include/openssl/ssl.h"):
        missing.append("libssl-dev")
    if not os.path.exists("/usr/include/zlib.h"):
        missing.append("zlib1g-dev")
    return missing


def _local_api_install_deps(log_fh):
    missing = _local_api_missing_deps()
    if not missing:
        return True
    print("Installing build dependencies: " + " ".join(missing), flush=True)
    if not shutil.which("apt-get"):
        print("! install these packages first: " + " ".join(missing), file=sys.stderr)
        return False
    cmd = ["apt-get", "install", "-y"] + missing
    rc = _run_stream(cmd, log_fh)
    if rc == 130:
        return False
    if rc != 0:
        print("! apt-get failed; refreshing package lists and retrying ...", file=sys.stderr)
        rc = _run_stream(["apt-get", "update"], log_fh)
        if rc == 130:
            return False
        if rc == 0:
            rc = _run_stream(cmd, log_fh)
    if rc != 0:
        print(
            "! cannot install build dependencies; run manually:\n"
            "  sudo apt-get update && sudo apt-get install -y " + " ".join(missing),
            file=sys.stderr,
        )
        return False
    return True


def _local_api_source(cfg, log_fh):
    src = cfg.local_api_src
    if os.path.isfile(os.path.join(src, "CMakeLists.txt")):
        return src
    if os.path.exists(src):
        print(
            f"! {src} exists but is not a telegram-bot-api source tree.\n"
            "  Remove it or set TG_LOCAL_API_SRC to the correct directory.",
            file=sys.stderr,
        )
        return None
    try:
        os.makedirs(os.path.dirname(src) or ".", exist_ok=True)
    except OSError as exc:
        print(f"! cannot create {os.path.dirname(src)}: {exc}", file=sys.stderr)
        return None
    print(f"Cloning Telegram Bot API source into {src} ...", flush=True)
    print("  first run only - this can take a few minutes", flush=True)
    rc = _run_stream(
        ["git", "clone", "--recursive", "--progress", LOCAL_API_REPO, src],
        log_fh,
    )
    if rc != 0 or not os.path.isfile(os.path.join(src, "CMakeLists.txt")):
        if rc == 130:
            print("! source clone interrupted; rerun the command to retry", file=sys.stderr)
        else:
            print("! source clone failed; see the build log", file=sys.stderr)
        shutil.rmtree(src, ignore_errors=True)
        return None
    return src


def _local_api_build_running():
    """PID of an existing telegram-bot-api compile, if one is running."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "telegram-bot-api" in cmd and ("cmake --build" in cmd or "gmake" in cmd):
            return int(entry)
    return None


def _local_api_build_lock(cfg):
    path = os.path.join(cfg.state_dir, "local-api-build.lock")
    fd = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return None


def _local_api_build_unlock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def local_api_build(cfg, colours):
    """Build + install telegram-bot-api with visible progress."""
    if local_api_installed():
        return True
    running = _local_api_build_running()
    if running:
        print(
            f"! a Telegram Bot API build is already running (pid {running}).\n"
            "  Wait for it to finish, then rerun this command.",
            file=sys.stderr,
        )
        return False
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
    except OSError as exc:
        print(f"! cannot create {cfg.state_dir}: {exc}", file=sys.stderr)
        return False
    lock_fd = _local_api_build_lock(cfg)
    if lock_fd is None:
        print("! another local Bot API build is already running", file=sys.stderr)
        return False
    try:
        return _local_api_build_inner(cfg, colours)
    finally:
        _local_api_build_unlock(lock_fd)


def _local_api_build_inner(cfg, colours):
    """The actual build; the public wrapper holds the process lock."""
    c = colours
    if local_api_installed():
        return True
    if os.geteuid() != 0:
        print("! building the local Bot API server needs root - run this with sudo",
              file=sys.stderr)
        return False
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
    except OSError as exc:
        print(f"! cannot create {cfg.state_dir}: {exc}", file=sys.stderr)
        return False
    log_path = os.path.join(cfg.state_dir, "local-api-build.log")
    try:
        log_fh = open(log_path, "a", encoding="utf-8")
    except OSError as exc:
        print(f"! cannot write {log_path}: {exc}", file=sys.stderr)
        return False
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass
    try:
        try:
            log_fh.write(
                f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} local Bot API build =====\n"
            )
            log_fh.flush()
        except OSError:
            pass
        print(f"{c['b']}local Bot API server is not installed yet{c['rst']}")
        print(f"  source   {cfg.local_api_src}")
        print(f"  jobs     {cfg.local_api_jobs} (nice 19, low IO priority)")
        print(f"  log      {log_path}")
        print("  tgmessage stays on the Telegram cloud API while this runs.")
        if not _local_api_install_deps(log_fh):
            return False
        src = _local_api_source(cfg, log_fh)
        if not src:
            return False
        build_dir = os.path.join(src, "build")
        print("Configuring the build ...", flush=True)
        rc = _run_stream(
            [
                "cmake", "-S", src, "-B", build_dir,
                "-DCMAKE_BUILD_TYPE=Release",
                "-DCMAKE_INSTALL_PREFIX=/usr/local",
            ],
            log_fh,
        )
        if rc == 130:
            print("! configure interrupted; rerun the command to resume", file=sys.stderr)
            return False
        if rc != 0:
            print(f"! configure failed (exit {rc})", file=sys.stderr)
            _print_log_tail(log_path)
            return False
        jobs = max(1, int(getattr(cfg, "local_api_jobs", 1)))
        print(
            f"Building and installing ({jobs} job(s); the long part) ...",
            flush=True,
        )
        cmd = [
            "cmake", "--build", build_dir, "--target", "install", "--", f"-j{jobs}"
        ]
        prefix = []
        if shutil.which("stdbuf"):
            prefix += ["stdbuf", "-oL", "-eL"]
        if shutil.which("nice"):
            prefix += ["nice", "-n", "19"]
        if shutil.which("ionice"):
            prefix += ["ionice", "-c3"]
        rc = _run_stream(prefix + cmd, log_fh, progress=True)
        if rc == 130:
            print("! build interrupted; rerun the command to resume", file=sys.stderr)
            return False
        if rc != 0:
            print(f"! build failed (exit {rc})", file=sys.stderr)
            _print_log_tail(log_path)
            return False
        binary = local_api_installed()
        if not binary:
            print(
                f"! build finished but {LOCAL_API_BIN} was not installed",
                file=sys.stderr,
            )
            _print_log_tail(log_path)
            return False
        print(f"{c['green']}installed{c['rst']} {binary}")
        return True
    except Exception as exc:  # noqa: BLE001 - show a useful failure instead of a traceback
        print(f"! local Bot API build error: {exc!r}", file=sys.stderr)
        _print_log_tail(log_path)
        return False
    finally:
        log_fh.close()


def local_api_unit_text(cfg):
    binary = local_api_bin()
    return f"""\
[Unit]
Description=Local Telegram Bot API server for tgmessage
Documentation=https://github.com/tdlib/telegram-bot-api
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-{env_file(cfg.home)}
ExecStart={binary} --api-id=${{TG_API_ID}} --api-hash=${{TG_API_HASH}} --local --http-ip-address=127.0.0.1 --http-port={cfg.local_api_port} --dir={cfg.local_api_dir}
Restart=always
RestartSec=10
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""


def local_api_service_state():
    if not has_systemd() or not os.path.exists(LOCAL_API_UNIT):
        return "not installed"
    state = systemctl("is-active", f"{LOCAL_API_SERVICE}.service").stdout.strip()
    return state or "unknown"


def local_api_restart_service():
    """Restart the local server; this aborts its blocking getFile downloads."""
    if not has_systemd() or not os.path.exists(LOCAL_API_UNIT):
        return False, "local Bot API service is not installed"
    try:
        result = subprocess.run(
            ["systemctl", "restart", f"{LOCAL_API_SERVICE}.service"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or f"systemctl exited {result.returncode}"
    return True, ""


def local_api_wait_ready(cfg, timeout=15):
    """Wait until the restarted local server answers getMe again."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if local_api_ping(cfg, timeout=1):
            return True
        time.sleep(0.25)
    return False


def local_api_stop_fetches(cfg):
    """Stop the local server's in-flight fetch by restarting it.

    Leaves the download queue paused; the caller must clear
    _local_api_restarting after it has also removed the old temp file.
    """
    _local_api_restarting.set()
    ok, err = local_api_restart_service()
    if not ok:
        return False, err
    local_api_wait_ready(cfg)
    return True, ""


def remove_local_temp(cfg, path):
    """Remove one orphaned file from the local server's temp directory."""
    if not path:
        return
    temp_dir = local_api_temp_dir(cfg)
    if not temp_dir:
        return
    try:
        temp_dir = os.path.realpath(temp_dir)
        real = os.path.realpath(path)
        if os.path.commonpath([temp_dir, real]) != temp_dir:
            return
        os.remove(real)
    except (OSError, ValueError):
        pass


def local_api_ping(cfg, timeout=5):
    """True when the local server answers getMe for our token."""
    if not cfg.token:
        return False
    url = f"http://127.0.0.1:{cfg.local_api_port}/bot{cfg.token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.load(resp)
        return bool(payload.get("ok"))
    except (OSError, ValueError):
        return False


def local_api_logout(cfg, label):
    """Log the bot out of the server currently serving `cfg`.

    Telegram requires this before moving a bot between the cloud Bot API and a
    local server; otherwise updates may be split between the two.
    """
    if not cfg.token:
        print("! no bot token configured; cannot log out", file=sys.stderr)
        return False
    try:
        result = Telegram(cfg).call("logOut", timeout=20)
    except (TelegramError, OSError, ValueError) as exc:
        print(f"! could not log out from {label}: {exc}", file=sys.stderr)
        return False
    if not result:
        print(f"! {label} did not confirm the log out", file=sys.stderr)
        return False
    print(f"logged the bot out from {label}")
    return True


def local_api_disable_service():
    if has_systemd() and os.path.exists(LOCAL_API_UNIT):
        systemctl("disable", "--now", f"{LOCAL_API_SERVICE}.service")


def local_api_install_service(cfg, colours):
    """Write + start telegram-bot-api.service. True when it is running."""
    c = colours
    if os.geteuid() != 0:
        print("! the local Bot API service needs root - run this with sudo", file=sys.stderr)
        return False
    if not has_systemd():
        print("! systemd is required to run the local Bot API server", file=sys.stderr)
        return False
    binary = local_api_installed()
    if not binary:
        print(
            f"! {LOCAL_API_BIN} is missing.\n"
            "  Run: sudo tgmessage local-api install",
            file=sys.stderr,
        )
        return False
    try:
        os.makedirs(cfg.local_api_dir, exist_ok=True)
        os.chmod(cfg.local_api_dir, 0o700)
        tmp = LOCAL_API_UNIT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(local_api_unit_text(cfg))
        os.chmod(tmp, 0o600)
        os.replace(tmp, LOCAL_API_UNIT)
    except OSError as exc:
        print(f"! cannot write {LOCAL_API_UNIT}: {exc}", file=sys.stderr)
        return False
    systemctl("daemon-reload")
    systemctl("enable", f"{LOCAL_API_SERVICE}.service")
    systemctl("restart", f"{LOCAL_API_SERVICE}.service")
    for _ in range(60):
        time.sleep(0.5)
        if local_api_service_state() == "active" and local_api_ping(cfg, timeout=2):
            print(f"{c['green']}local Bot API server is running{c['rst']} ({binary})")
            return True
        if local_api_service_state() in ("failed", "inactive"):
            break
    print(
        f"! {LOCAL_API_SERVICE}.service did not become ready.\n"
        f"  see: journalctl -u {LOCAL_API_SERVICE} -n 50",
        file=sys.stderr,
    )
    return False


def cmd_local_api(cfg, colours, rest):
    c = colours
    action = (rest[0].strip().lower() if rest else "")

    if action in ("status", "show"):
        binary = local_api_installed()
        print(f"{c['b']}local Bot API{c['rst']}")
        print(f"  binary        {binary or 'missing - run: sudo tgmessage local-api install'}")
        print(f"  service       {local_api_service_state()}")
        print(f"  api id        {cfg.api_id or '(not set)'}")
        print(f"  api hash      {'***' + cfg.api_hash[-4:] if cfg.api_hash else '(not set)'}")
        print(f"  api root      {cfg.api_root or 'Telegram cloud (default)'}")
        print(f"  storage       {cfg.local_api_dir}")
        print(f"  source        {cfg.local_api_src}")
        print(f"  build jobs    {cfg.local_api_jobs}")
        print(f"  downloads     up to {human_size(cfg.max_download)}")
        print(f"  uploads       up to {human_size(cfg.max_upload)}")
        return 0

    if os.geteuid() != 0:
        print("! run this command with sudo (it writes .env and systemd units)", file=sys.stderr)
        return 2

    if action in ("off", "disable", "cloud", "stop", "down"):
        if cfg.is_local_api:
            service_stop(cfg)
            if not local_api_logout(cfg, "the local Bot API server"):
                print("! warning: could not log out from the local server", file=sys.stderr)
        local_api_disable_service()
        try:
            write_env(cfg.home, {
                "TG_API_ROOT": "",
                "TG_MAX_DOWNLOAD_MB": None,
                "TG_MAX_UPLOAD_MB": None,
            })
        except OSError as exc:
            print(f"! cannot update {env_file(cfg.home)}: {exc}", file=sys.stderr)
            return 1
        print("local Bot API disabled - using Telegram cloud (20 MB downloads)")
        cloud_cfg = Config(cfg.home)
        if unit_installed() or daemon_pid(cloud_cfg):
            print(f"daemon restarted ({service_restart(cloud_cfg)})")
        return 0

    api_id, api_hash = "", ""
    if action in ("set", "save") and len(rest) >= 3:
        api_id, api_hash = rest[1].strip(), rest[2].strip()
    elif len(rest) >= 2:
        api_id, api_hash = rest[0].strip(), rest[1].strip()
    elif not rest and sys.stdin.isatty():
        print("Telegram API credentials: https://my.telegram.org -> API development tools")
        api_id = input("  api_id: ").strip()
        api_hash = getpass.getpass("  api_hash (hidden): ").strip()
    elif action in ("install", "setup", "start", "on"):
        api_id, api_hash = cfg.api_id, cfg.api_hash
        if not api_id or not api_hash:
            print(
                "no API credentials saved yet - run: tgmessage local-api <api_id> <api_hash>",
                file=sys.stderr,
            )
            return 2
    else:
        print(
            "usage: tgmessage local-api <api_id> <api_hash>\n"
            "       tgmessage local-api status|off|stop",
            file=sys.stderr,
        )
        return 2

    if not looks_like_id(api_id):
        print("! api_id must be a number - copy it from https://my.telegram.org", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash or ""):
        print("! api_hash must be the 32-character value from https://my.telegram.org", file=sys.stderr)
        return 2

    # Save the keys first. TG_API_ROOT is switched on only after the server
    # is confirmed reachable, so a failed setup leaves the cloud API working.
    write_env(cfg.home, {"TG_API_ID": api_id, "TG_API_HASH": api_hash})
    try:
        os.chmod(env_file(cfg.home), 0o600)
    except OSError:
        pass
    cfg.api_id, cfg.api_hash = api_id, api_hash
    print(f"saved api_id {api_id} and api_hash ***{api_hash[-4:]} to {env_file(cfg.home)}")

    # Build before touching the running daemon, so the bot stays online on the
    # cloud API during the one-time compile. The build streams its progress.
    if not local_api_installed():
        if not local_api_build(cfg, colours):
            print("credentials saved; tgmessage still uses the Telegram cloud API",
                  file=sys.stderr)
            return 1

    was_local = cfg.is_local_api
    if not was_local:
        # Stop our cloud poller before starting the local server, then bring it
        # back if setup fails. This avoids two tgmessage consumers racing.
        service_stop(cfg)
    if not local_api_install_service(cfg, colours):
        if not was_local:
            # Never leave a second poller behind if setup failed.
            local_api_disable_service()
            service_restart(Config(cfg.home))
            print("credentials saved; tgmessage is back on the Telegram cloud API", file=sys.stderr)
        else:
            print("credentials saved, but the local Bot API is not ready", file=sys.stderr)
        return 1

    # The local server is up: migrate the bot. If the cloud logout fails, the
    # cloud config is restored and the local service is disabled again.
    if not was_local:
        local_root = f"http://127.0.0.1:{cfg.local_api_port}"
        try:
            write_env(cfg.home, {
                "TG_API_ROOT": local_root,
                "TG_MAX_DOWNLOAD_MB": "2000",
                "TG_MAX_UPLOAD_MB": "2000",
            })
        except OSError as exc:
            print(f"! cannot update {env_file(cfg.home)}: {exc}", file=sys.stderr)
            local_api_disable_service()
            service_restart(Config(cfg.home))
            return 1
        if not local_api_logout(cfg, "Telegram cloud"):
            # Put the cloud config back; no daemon has been restarted yet.
            write_env(cfg.home, {
                "TG_API_ROOT": "",
                "TG_MAX_DOWNLOAD_MB": None,
                "TG_MAX_UPLOAD_MB": None,
            })
            local_api_disable_service()
            print("! still using the Telegram cloud API", file=sys.stderr)
            service_restart(Config(cfg.home))
            return 1

    cfg.api_root = f"http://127.0.0.1:{cfg.local_api_port}"
    cfg.is_local_api = True
    cfg.max_download = 2000 * 1024 * 1024
    cfg.max_upload = 2000 * 1024 * 1024
    print(f"{c['green']}large files enabled{c['rst']}: up to {human_size(cfg.max_download)}")
    if unit_installed() or daemon_pid(cfg):
        print(f"daemon restarted ({service_restart(Config(cfg.home))})")
    return 0


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


HELP = """\
tgmessage - Telegram from any terminal, one session per chat.

  tgmessage                      open the chat UI on the default session
  tgmessage chat <id>            open the UI on a specific chat
  tgmessage <text>               send one message to the default chat
  tgmessage --to <id> <text>     send one message to a specific chat
  echo hi | tgmessage            read lines from stdin and send them

  tgmessage --FILE <path>        send a file as a Telegram document
  tgmessage --to <id> --FILE <path> --caption "hi"
  tgmessage file <path>          same as --FILE

  tgmessage sessions             list chats, permissions and history size
  tgmessage requests, --request  list chats waiting to be allowed

  tgmessage allow <id> [...]     allow a chat to talk to the bot
  tgmessage allow sys <id> ...   + /status, /logs, /cancel_upload and root shell
  tgmessage allow mang <id> ...  + watcher reports (manage)
  tgmessage deny <id> [...]      revoke everything for a chat
  tgmessage deny sys|mang <id>   revoke only that level

  tgmessage shell on|off|status  enable/disable the /sh-on root shell (for sys chats)
  tgmessage commands [register|show]  sync the Telegram '/' command menu

  tgmessage service add <unit> [...]      watch a systemd service
  tgmessage service remove|rem <unit> [...]  stop watching it
  tgmessage service <unit> stop           report only - never auto-restart it
  tgmessage service <unit> restart        back to auto-restart (default)
  tgmessage service list                  show watched services

  tgmessage log [n] [--chat id]  recent messages (all chats unless --chat)
  tgmessage status               daemon + token + sessions health check
  tgmessage up | down | restart  control the daemon by hand

  tgmessage --token <token>      save a (new) bot token, restart the daemon
  tgmessage --port <port>        change the daemon port, restart the daemon

  tgmessage local-api <id> <hash>  set up local Bot API (files up to 2 GB, sudo)
  tgmessage local-api status|off   show or disable the local Bot API

  tgmessage install [--token T]  install the systemd service + /usr/local/bin link
  tgmessage uninstall            remove service + link
  tgmessage version | help

In the UI: /session [n|id] pick a chat, /allow [sys|mang] <id>, /deny <id>,
/requests, /copy [n|all], /file <path> [caption], //text for a literal slash.

From the Telegram chat (sys access needed for the first four):
/status replies with a live server report (active uploads with ids, the services
you added with `tgmessage service add`, CPU + temp, fan speed, RAM, disk,
battery, IPs);
/logs replies with the tail of the watcher system log
(/root/tgmessage/watcher/logs/watcher.log; /logs 100 for more lines);
/cancel_upload <id> stops an active upload listed by /status and deletes its
partial file;
/sh_on opens a root shell in a private chat (disabled by default; enable with
`tgmessage shell on`), then every plain message runs as a root command;
/sh_off closes it (/sh-on and /sh-off are accepted too); /ai starts AI mode in
the saved cd and /ai-exit stops it.  While /sh_on is active, /ai is refused.
Inside AI mode /model and /thinking show Telegram buttons, /session resumes
a saved pi session for the current cd, /ai-status shows token and context
usage, /ai-agent lists the injected rules, and /ai-agent-add,
/ai-agent-remove manage custom rules.  /btw <text> asks a side question
without adding it to the main session, /live lists running pi sessions and
subagents in the saved cd.  /pi sends text or a pi command
explicitly, and thinking text plus tool calls stay hidden.  tgmessage.md is
injected into every new AI session so the AI can send code blocks, HTML,
buttons, typing actions and short progress status messages.
/help lists these commands; every photo/file you send is saved into files/
(see TG_FILES_DIR).  The Telegram '/' menu is registered automatically; sync
it with `tgmessage commands`.
"""


def cmd_chat(cfg, colours, rest):
    if rest:
        first = rest[0]
        if looks_like_id(first):
            if sys.stdin.isatty() and sys.stdout.isatty():
                return ChatClient(cfg, colours, chat=first).run()
            return cmd_send(cfg, colours, rest)
        return cmd_send(cfg, colours, rest)
    if sys.stdin.isatty() and sys.stdout.isatty():
        return ChatClient(cfg, colours).run()
    return ChatClient(cfg, colours).run_pipe()


def cmd_send(cfg, colours, rest):
    text = " ".join(rest).strip()
    if not text:
        print("nothing to send", file=sys.stderr)
        return 2
    import io

    stdin_backup = sys.stdin
    sys.stdin = io.StringIO(text + "\n")
    try:
        return ChatClient(cfg, colours).run_pipe()
    finally:
        sys.stdin = stdin_backup


def cmd_daemon(cfg, colours, rest):
    cfg.ensure()
    migrate_legacy(cfg)
    Daemon(cfg).start()
    return 0


def cmd_file(cfg, colours, rest):
    if not rest:
        print("usage: tgmessage --FILE '/path/to/file' [--caption 'text'] [--to <chatid>]", file=sys.stderr)
        return 2
    # Resolve here, in the shell's own directory: the daemon runs elsewhere and
    # would otherwise resolve a relative path against /root/tgmessage.
    path = os.path.abspath(os.path.expanduser(rest[0]))
    caption = " ".join(rest[1:]).strip() or None
    if not os.path.exists(path):
        print(f"! no such file: {path}", file=sys.stderr)
        return 1
    if os.path.isdir(path):
        print(f"! {path} is a directory", file=sys.stderr)
        return 1
    return ChatClient(cfg, colours).send_file(path, caption)


def cmd_token(cfg, colours, rest):
    if rest:
        token = rest[0].strip()
        if ":" not in token:
            print("that does not look like a bot token (expected 123456:AA...)", file=sys.stderr)
            return 2
        write_env(cfg.home, {"TG_BOT_TOKEN": token})
        print(f"saved token ***{token[-6:]} to {env_file(cfg.home)}")
        if unit_installed() or daemon_pid(cfg):
            how = service_restart(cfg)
            print(f"daemon restarted ({how})")
        return 0
    shown = cfg.token
    if not shown:
        print("no token configured - run: tgmessage --token '<token>'")
        return 1
    print(f"{env_file(cfg.home)}")
    print(f"  TG_BOT_TOKEN=***{shown[-6:]}")
    print(f"  TG_ALLOWED_CHAT_IDS={','.join(cfg.allowed) or '(none)'}")
    print(f"  TG_SYS_CHAT_IDS={','.join(cfg.sys) or '(none)'}")
    print(f"  TG_MANG_CHAT_IDS={','.join(cfg.mang) or '(none)'}")
    print(f"  TG_SHELL_ENABLED={'1' if cfg.shell_enabled else '0'}")
    print(f"  TG_WATCH_SERVICES={','.join(cfg.watch_services) or '(none)'}")
    print(f"  TG_SERVICE_NO_RESTART={','.join(cfg.service_no_restart) or '(none)'}")
    print(f"  TG_DEFAULT_CHAT={cfg.default_chat or '(none)'}")
    print(f"  TG_HOST={cfg.host}")
    print(f"  TG_PORT={cfg.port}")
    print(f"  TG_API_ROOT={cfg.api_root or '(Telegram cloud)'}")
    print(f"  TG_MAX_DOWNLOAD_MB={cfg.max_download // (1024 * 1024)}")
    print(f"  TG_MAX_UPLOAD_MB={cfg.max_upload // (1024 * 1024)}")
    return 0


def _print_session_table(cfg, colours, rows, title):
    c = colours
    if not rows:
        print(f"{title}: none")
        return
    print(f"{c['b']}{title}{c['rst']}")
    for row in rows:
        mark = "*" if row.get("allowed") else " "
        label = f"@{row['username']}" if row.get("username") else (row.get("name") or row.get("chat_id"))
        real = row.get("name") or ""
        extra = f"  {real}" if real and real != label.lstrip("@") else ""
        state = "allowed" if row.get("allowed") else ("request" if row.get("pending") else "seen")
        print(f" {mark} {label}{extra}  {c['dim']}{row.get('chat_id')}{c['rst']}  {state}  {row.get('count', 0)} msgs")


def _merged_sessions(cfg):
    hist = History(cfg.state_dir, cfg.history)
    pending = Pending(os.path.join(cfg.state_dir, "pending.json")).all()
    registry = Registry(os.path.join(cfg.state_dir, "chats.json")).all()
    pend_ids = {str(p.get("chat_id")) for p in pending}
    ids = set(cfg.allowed) | set(hist.chats()) | set(registry) | pend_ids
    rows = []
    for cid in ids:
        meta = registry.get(cid, {})
        allowed = cid in cfg.allowed
        rows.append(
            {
                "chat_id": cid,
                "name": meta.get("name") or cid,
                "username": meta.get("username") or "",
                "allowed": allowed,
                "pending": cid in pend_ids,
                "count": hist.count(cid) if allowed else 0,
                "last_ts": meta.get("last_ts") or 0,
            }
        )
    rows.sort(key=lambda r: (not r["allowed"], -(r["last_ts"] or 0), id_sort_key(r["chat_id"])))
    return rows


def cmd_sessions(cfg, colours, rest):
    _print_session_table(cfg, colours, _merged_sessions(cfg), "sessions")
    return 0


def cmd_requests(cfg, colours, rest):
    pending = Pending(os.path.join(cfg.state_dir, "pending.json")).all()
    c = colours
    if not pending:
        print("no pending requests")
        return 0
    print(f"{c['b']}requests (not allowed yet){c['rst']}")
    for req in pending:
        name = req.get("name") or req.get("chat_id")
        user = f" @{req['username']}" if req.get("username") else ""
        preview = (req.get("last_text") or "")[:60]
        when = time.strftime("%m-%d %H:%M", time.localtime(req.get("last_ts") or 0))
        print(
            f"  {name}{user}  {c['dim']}{req.get('chat_id')}{c['rst']}  "
            f"x{req.get('count', 1)}  {when}  {preview}"
        )
    print(f"{c['dim']}allow with: tgmessage allow <chatid>{c['rst']}")
    return 0


def _apply_allow(cfg, colours, ids, allow=True, level="chat"):
    ids = [str(i).strip() for i in ids if str(i).strip()]
    bad = [i for i in ids if not looks_like_id(i)]
    if bad:
        print(f"! not a chat id: {', '.join(bad)}", file=sys.stderr)
        return 2
    allowed = set(cfg.allowed)
    sys_ids = set(cfg.sys)
    mang_ids = set(cfg.mang)
    if allow:
        if level == "sys":
            sys_ids |= set(ids)
            allowed |= set(ids)
        elif level == "mang":
            mang_ids |= set(ids)
            sys_ids |= set(ids)
            allowed |= set(ids)
        else:
            allowed |= set(ids)
        default = cfg.default_chat or ids[0]
    else:
        if level == "mang":
            mang_ids -= set(ids)
        elif level == "sys":
            sys_ids -= set(ids)
            mang_ids -= set(ids)
        else:
            allowed -= set(ids)
            sys_ids -= set(ids)
            mang_ids -= set(ids)
        default = cfg.default_chat
        if default in ids and default not in allowed:
            default = sorted(allowed, key=id_sort_key)[0] if allowed else ""
    persist_permissions(cfg, allowed, sys_ids, mang_ids, default)

    pend = Pending(os.path.join(cfg.state_dir, "pending.json"))
    for cid in ids:
        if allow:
            pend.remove(cid)

    verb = "allowed" if allow else "denied"
    suffix = f" ({level})" if level != "chat" else ""
    print(f"{verb}{suffix}: {', '.join(ids)}")
    print(f"  TG_ALLOWED_CHAT_IDS={','.join(sorted(allowed, key=id_sort_key)) or '(none)'}")
    print(f"  TG_SYS_CHAT_IDS={','.join(sorted(sys_ids, key=id_sort_key)) or '(none)'}")
    print(f"  TG_MANG_CHAT_IDS={','.join(sorted(mang_ids, key=id_sort_key)) or '(none)'}")
    print(f"  TG_DEFAULT_CHAT={default or '(none)'}")
    if notify_daemon(cfg, {"cmd": "reload"}):
        print(f"{colours['dim']}daemon reloaded{colours['rst']}")
    else:
        print(f"{colours['yellow']}daemon not reachable - it will pick this up on next restart{colours['rst']}")
    return 0


def _parse_level(rest):
    """Split '<sys|mang> <id> ...' into (level, ids)."""
    level = "chat"
    args = list(rest)
    if args and args[0].lower() in ("sys", "system"):
        level = "sys"
        args = args[1:]
    elif args and args[0].lower() in ("mang", "manage"):
        level = "mang"
        args = args[1:]
    return level, args


def cmd_allow(cfg, colours, rest):
    level, ids = _parse_level(rest)
    if not ids:
        print("usage: tgmessage allow [sys|mang] <chatid> [...]", file=sys.stderr)
        return 2
    return _apply_allow(cfg, colours, ids, allow=True, level=level)


def cmd_deny(cfg, colours, rest):
    level, ids = _parse_level(rest)
    if not ids:
        print("usage: tgmessage deny [sys|mang] <chatid> [...]", file=sys.stderr)
        return 2
    return _apply_allow(cfg, colours, ids, allow=False, level=level)


def cmd_shell(cfg, colours, rest):
    """Enable/disable the Telegram root shell, or show its settings."""
    c = colours
    action = (rest[0].lower() if rest else "status")
    if action not in ("on", "off", "status", "enable", "disable", "show"):
        print("usage: tgmessage shell on|off|status", file=sys.stderr)
        return 2

    if action in ("on", "enable"):
        write_env(cfg.home, {"TG_SHELL_ENABLED": "1"})
        cfg.shell_enabled = True
        print(f"{c['red']}root shell ENABLED{c['rst']} - sys chats can now use /sh-on")
        print("  sys access + a private chat are required; use `tgmessage shell off` to revoke")
    elif action in ("off", "disable"):
        write_env(cfg.home, {"TG_SHELL_ENABLED": "0"})
        cfg.shell_enabled = False
        print(f"{c['green']}root shell DISABLED{c['rst']} - /sh-on is refused and open shells close")
    else:
        state = f"{c['red']}ON (dangerous){c['rst']}" if cfg.shell_enabled else "off"
        print(f"root shell: {state}")
        print(f"  idle timeout    {human_duration(cfg.shell_idle)}")
        print(f"  max session     {human_duration(cfg.shell_max)}")
        print(f"  command timeout {human_duration(cfg.shell_timeout)}")
        print(f"  allowed chats   sys users only: "
              f"{', '.join(sorted(cfg.sys, key=id_sort_key)) or '(none)'}")
        print("  private chats   required")
        return 0

    if notify_daemon(cfg, {"cmd": "reload"}):
        print(f"  {c['dim']}daemon reloaded{c['rst']}")
    else:
        print(f"  {c['yellow']}daemon not reachable - it will pick this up on next restart{c['rst']}")
    return 0


def cmd_commands(cfg, colours, rest):
    """Register or show the command menu Telegram displays when pressing '/'."""
    c = colours
    action = (rest[0].lower() if rest else "register")
    if action in ("register", "set", "sync", "update"):
        ok, err = register_bot_commands(cfg)
        if not ok:
            print(f"! could not register commands: {err}", file=sys.stderr)
            return 1
        print(f"{c['green']}registered {len(BOT_COMMANDS)} commands{c['rst']} in the Telegram '/' menu")
        for name, desc in BOT_COMMANDS:
            print(f"  /{name:<14} {c['dim']}{desc}{c['rst']}")
        return 0
    if action in ("show", "list", "status"):
        try:
            rows = Telegram(cfg).call("getMyCommands", {}) or []
        except TelegramError as exc:
            print(f"! could not read commands: {exc}", file=sys.stderr)
            return 1
        if not rows:
            print("no commands registered - run: tgmessage commands register")
            return 0
        print(f"{c['b']}Telegram '/' menu{c['rst']}")
        for row in rows:
            print(f"  /{row.get('command', ''):<14} {c['dim']}{row.get('description', '')}{c['rst']}")
        return 0
    print("usage: tgmessage commands [register|show]", file=sys.stderr)
    return 2


def _service_list(cfg):
    return list(cfg.watch_services)


def cmd_service(cfg, colours, rest):
    c = colours
    action = (rest[0].lower() if rest else "list")
    args = rest[1:] if rest else []

    if action in ("list", "ls", "show", "status"):
        units = _service_list(cfg)
        if not units:
            print("no watched services - add one with: tgmessage service add <name>")
            return 0
        notify_only = set(cfg.service_no_restart)
        print(f"{c['b']}watched services{c['rst']}  (saved in .env, TG_WATCH_SERVICES)")
        states = dict(status_services(units)) if has_systemd() else {}
        for unit in units:
            label = unit[: -len(".service")] if unit.endswith(".service") else unit
            state = states.get(label)
            mark = f"{c['green']}{state}{c['rst']}" if state == "active" else (
                f"{c['red']}{state or 'unknown'}{c['rst']}"
            )
            tag = f"  {c['yellow']}[notify-only - no auto-restart]{c['rst']}" if unit in notify_only else ""
            print(f"  {label}  ({mark}){tag}")
        return 0

    if action in ("add", "watch") or action in ("remove", "rm", "rem", "del", "unwatch"):
        want = action in ("add", "watch")
        if not args:
            print(f"usage: tgmessage service {action} <systemd-unit> [...]", file=sys.stderr)
            return 2
        current = set(_service_list(cfg))
        notify_only = set(cfg.service_no_restart)
        units = []
        for raw in args:
            for part in str(raw).replace(",", " ").split():
                unit = normalize_unit(part)
                if not unit:
                    print(f"! not a valid unit name: {part}", file=sys.stderr)
                    return 2
                units.append(unit)
        if want:
            current |= set(units)
            notify_only -= set(units)      # re-adding goes back to auto-restart
        else:
            current -= set(units)
            notify_only -= set(units)      # no dangling notify-only entry
        write_env(cfg.home, {
            "TG_WATCH_SERVICES": ",".join(sorted(current)),
            "TG_SERVICE_NO_RESTART": ",".join(sorted(notify_only)),
        })
        cfg.watch_services = sorted(current)
        cfg.service_no_restart = sorted(notify_only)
        verb = "watching" if want else "no longer watching"
        print(f"{verb}: {', '.join(units)}")
        print(f"  TG_WATCH_SERVICES={','.join(sorted(current)) or '(none)'}")
        missing = []
        if want and has_systemd():
            for unit in units:
                r = subprocess.run(["systemctl", "cat", unit], capture_output=True, text=True)
                if r.returncode != 0:
                    missing.append(unit)
        if missing:
            print(
                f"{c['yellow']}! not installed right now: {', '.join(missing)}"
                f" - the watcher will report it as failed{c['rst']}"
            )
        _service_saved_note(cfg, colours)
        return 0

    # tgmessage service <unit> stop     -> reports only, never restarts it
    # tgmessage service <unit> restart  -> back to the default (auto-restart)
    unit = normalize_unit(rest[0]) if rest else ""
    if unit and args and args[0].lower() in ("stop", "restart"):
        mode = args[0].lower()
        if unit not in cfg.watch_services:
            print(
                f"! {unit} is not in the watch list - add it first: tgmessage service add {unit}",
                file=sys.stderr,
            )
            return 2
        if mode == "stop":
            cfg.service_no_restart = sorted(set(cfg.service_no_restart) | {unit})
            note = "reports only now - the watcher will NOT restart it"
        else:
            cfg.service_no_restart = sorted(set(cfg.service_no_restart) - {unit})
            note = "auto-restart is back on (default)"
        write_env(cfg.home, {
            "TG_SERVICE_NO_RESTART": ",".join(cfg.service_no_restart),
        })
        print(f"{unit}: {note}")
        print(f"  TG_SERVICE_NO_RESTART={','.join(cfg.service_no_restart) or '(none)'}")
        _service_saved_note(cfg, colours)
        return 0

    print(
        "usage: tgmessage service <add|remove|rem|list> [unit ...]\n"
        "       tgmessage service <unit> stop|restart",
        file=sys.stderr,
    )
    return 2


def _service_saved_note(cfg, colours):
    if notify_daemon(cfg, {"cmd": "reload"}):
        print(f"{colours['dim']}daemon reloaded (no watcher restart needed - it re-reads .env every cycle){colours['rst']}")
    else:
        print(f"{colours['dim']}the watcher re-reads .env every cycle - no restart needed{colours['rst']}")


def cmd_status(cfg, colours, rest):
    c = colours
    print(f"{c['b']}tgmessage{c['rst']}  home {cfg.home}")
    print(f"  .env          {env_file(cfg.home)} {'(ok)' if os.path.exists(env_file(cfg.home)) else '(MISSING)'}")
    print(f"  token         {'***' + cfg.token[-6:] if cfg.token else '(none)'}")
    print(f"  allowed       {', '.join(sorted(cfg.allowed, key=id_sort_key)) or '(none)'}")
    print(f"  sys           {', '.join(sorted(cfg.sys, key=id_sort_key)) or '(none)'}")
    print(f"  mang          {', '.join(sorted(cfg.mang, key=id_sort_key)) or '(none)'}")
    shell = "ON (dangerous)" if cfg.shell_enabled else "off (tgmessage shell on)"
    print(f"  root shell    {shell}")
    print(f"  services      {', '.join(cfg.watch_services) or '(none - tgmessage service add)'}")
    print(f"  default chat  {cfg.default_chat or '(none)'}")
    print(f"  listening     {cfg.host}:{cfg.port}")
    print(f"  working dir   {read_workdir(cfg)}")
    print(f"  bot api       {cfg.api_root or 'Telegram cloud'}  (files up to {human_size(cfg.max_download)})")
    if cfg.is_local_api:
        print(f"  local api     {local_api_service_state()}")

    up = reachable(cfg)
    if has_systemd() and unit_installed():
        state = systemctl("is-active", f"{SERVICE_NAME}.service").stdout.strip()
        print(f"  service       {SERVICE_NAME}.service is {state} (systemd, enabled on boot)")
    elif daemon_pid(cfg):
        print(f"  service       running without systemd (pid {daemon_pid(cfg)})")
    else:
        print("  service       not installed as a service")
    print(f"  socket        {'reachable' if up else 'NOT reachable'}")

    if cfg.token:
        try:
            me = Telegram(cfg).get_me()
            print(f"  telegram      ok - @{me.get('username')} ({me.get('first_name')})")
        except TelegramError as exc:
            print(f"  telegram      {c['red']}FAILED{c['rst']} {exc}")
    rows = _merged_sessions(cfg)
    allowed = [r for r in rows if r["allowed"]]
    pend = [r for r in rows if not r["allowed"] and r["pending"]]
    print(f"  sessions      {len(allowed)} allowed, {len(pend)} pending request(s)")
    for row in allowed:
        label = f"@{row['username']}" if row.get("username") else (row.get("name") or row["chat_id"])
        print(f"                  {label}  {c['dim']}{row['chat_id']}{c['rst']}  {row['count']} msgs")
    for row in pend:
        label = f"@{row['username']}" if row.get("username") else (row.get("name") or row["chat_id"])
        print(f"                  {c['yellow']}{label}  {row['chat_id']} (request){c['rst']}")
    return 0 if up else 1


def cmd_log(cfg, colours, rest):
    n = 20
    chat = getattr(cfg, "log_chat", None)
    args = list(rest)
    if args:
        try:
            n = int(args[0])
        except ValueError:
            pass
    hist = History(cfg.state_dir, cfg.history)
    if chat:
        records = [(rec.get("ts") or 0, str(chat), rec) for rec in hist.tail(chat, n)]
    else:
        records = []
        for cid in set(hist.chats()) | set(cfg.allowed):
            for rec in hist.tail(cid, n):
                records.append((rec.get("ts") or 0, str(cid), rec))
        records.sort(key=lambda r: r[0])
        records = records[-n:]
    if not records:
        print("(no messages yet)")
        return 0
    registry = Registry(os.path.join(cfg.state_dir, "chats.json"))
    for ts, cid, rec in records:
        who = "Bot" if rec.get("dir") == "in" else "Me "
        col = colours["green"] if rec.get("dir") == "in" else colours["cyan"]
        meta = registry.get(cid) if not chat else {}
        name = ("@" + meta["username"]) if meta.get("username") else (meta.get("name") or "") if not chat else ""
        tag = f"{colours['dim']}{name}:{colours['rst']} " if name else ""
        stamp = time.strftime("%m-%d %H:%M", time.localtime(ts))
        print(f"{colours['dim']}{stamp}{colours['rst']} {tag}{col}{who}>{colours['rst']} {rec.get('text', '')}")
    return 0


def cmd_up(cfg, colours, rest):
    cfg.ensure()
    how = service_start(cfg)
    print(f"daemon: {how}")
    return 0


def cmd_down(cfg, colours, rest):
    print(f"daemon: {service_stop(cfg)}")
    return 0


def cmd_restart(cfg, colours, rest):
    cfg.ensure()
    print(f"daemon: {service_restart(cfg)}")
    return 0


def cmd_install(cfg, colours, rest):
    script = os.path.join(cfg.home, "install.sh")
    if not os.path.exists(script):
        print(f"install.sh not found in {cfg.home}", file=sys.stderr)
        return 1
    return subprocess.call(["bash", script, *rest])


def cmd_uninstall(cfg, colours, rest):
    script = os.path.join(cfg.home, "install.sh")
    if os.path.exists(script):
        return subprocess.call(["bash", script, "--uninstall"])
    return 1


def cmd_version(cfg, colours, rest):
    print("tgmessage 1.1")
    return 0


COMMANDS = {
    "chat": cmd_chat,
    "attach": cmd_chat,
    "send": cmd_send,
    "file": cmd_file,
    "sendfile": cmd_file,
    "daemon": cmd_daemon,
    "token": cmd_token,
    "sessions": cmd_sessions,
    "session": cmd_sessions,
    "requests": cmd_requests,
    "request": cmd_requests,
    "allow": cmd_allow,
    "deny": cmd_deny,
    "shell": cmd_shell,
    "commands": cmd_commands,
    "command": cmd_commands,
    "service": cmd_service,
    "services": lambda cfg, colours, rest: cmd_service(cfg, colours, ["list"]),
    "local-api": cmd_local_api,
    "localapi": cmd_local_api,
    "status": cmd_status,
    "log": cmd_log,
    "up": cmd_up,
    "down": cmd_down,
    "restart": cmd_restart,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "version": cmd_version,
    "help": lambda cfg, colours, rest: (print(HELP), 0)[1],
}


def main(argv):
    colours = colour(sys.stdout.isatty())
    home = os.environ.get("TG_HOME") or DEFAULT_HOME

    parser = argparse.ArgumentParser(add_help=False, prog="tgmessage")
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--token")
    parser.add_argument("--port")
    parser.add_argument("--home")
    parser.add_argument("--to", dest="to", metavar="CHATID")
    parser.add_argument("--chat", dest="chat", metavar="CHATID")
    parser.add_argument("--allow", dest="allow", action="append", metavar="CHATID")
    parser.add_argument("--deny", dest="deny", action="append", metavar="CHATID")
    parser.add_argument("--request", "--requests", dest="request", action="store_true")
    parser.add_argument("--FILE", "--file", dest="file", metavar="PATH")
    parser.add_argument("--caption", dest="caption")
    parser.add_argument("rest", nargs="*")
    opts, unknown = parser.parse_known_args(argv)

    if opts.home:
        home = opts.home
    cfg = Config(home)

    if opts.help:
        print(HELP)
        return 0

    # --token / --port are shortcuts for the `token` command
    if opts.token is not None or opts.port is not None:
        if opts.port is not None:
            try:
                int(opts.port)
            except ValueError:
                print("port must be a number", file=sys.stderr)
                return 2
            write_env(cfg.home, {"TG_PORT": opts.port})
            print(f"saved TG_PORT={opts.port} to {env_file(cfg.home)}")
        if opts.token is not None:
            rc = cmd_token(Config(home), colours, [opts.token])
        else:
            rc = 0
            if unit_installed() or daemon_pid(cfg):
                print(f"daemon restarted ({service_restart(cfg)})")
        return rc

    if opts.to:
        if not looks_like_id(opts.to):
            print(f"--to expects a chat id, got: {opts.to}", file=sys.stderr)
            return 2
        cfg.default_chat = str(opts.to)

    if opts.chat:
        cfg.log_chat = str(opts.chat)

    if opts.allow:
        vals = list(opts.allow)
        extra = opts.rest + unknown
        if vals and vals[0].lower() in ("sys", "system", "mang", "manage") and all(looks_like_id(x) for x in extra):
            vals += extra
        return cmd_allow(cfg, colours, vals)
    if opts.deny:
        vals = list(opts.deny)
        extra = opts.rest + unknown
        if vals and vals[0].lower() in ("sys", "system", "mang", "manage") and all(looks_like_id(x) for x in extra):
            vals += extra
        return cmd_deny(cfg, colours, vals)
    if opts.request:
        return cmd_requests(cfg, colours, [])

    # --FILE is a shortcut for the `file` command
    if opts.caption and not opts.file:
        print("--caption only makes sense with --FILE", file=sys.stderr)
        return 2
    if opts.file:
        args = [opts.file]
        if opts.caption:
            args.append(opts.caption)
        return cmd_file(cfg, colours, args)

    rest = opts.rest + unknown
    if not rest:
        cfg.ensure()
        migrate_legacy(cfg)
        return cmd_chat(cfg, colours, [])

    head = rest[0]
    if head in COMMANDS:
        if head not in ("daemon", "version", "help"):
            cfg.ensure()
        return COMMANDS[head](cfg, colours, rest[1:])

    # anything else is treated as a message to send
    return cmd_send(cfg, colours, rest)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
