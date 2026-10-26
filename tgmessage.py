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
    tgmessage allow sys <id>     + /status, /logs and /cancel_upload from Telegram
    tgmessage allow mang <id>    + watcher reports (manage)
    tgmessage deny [sys|mang] <id>
    tgmessage service add <unit> watch a systemd service (restart + alert)

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
SERVICE_NAME = "tgmessage"
MAX_FILE = 50 * 1024 * 1024  # Telegram cloud Bot API limit for uploads


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
    ("TG_MAX_DOWNLOAD_MB", "", "max download size in MB (default: 20 cloud, 2000 local)"),
    ("TG_MAX_UPLOAD_MB", "", "max upload size in MB (default: 50 cloud, 2000 local)"),
    ("TG_ALLOWED_CHAT_IDS", "", "chat ids allowed to talk to this bot"),
    ("TG_SYS_CHAT_IDS", "", "chat ids allowed to use /status, /logs and /cancel_upload (tgmessage allow sys)"),
    ("TG_MANG_CHAT_IDS", "", "chat ids that receive watcher reports (tgmessage allow mang)"),
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
                "allowed_updates": json.dumps(["message", "edited_message"]),
            },
            timeout=self.cfg.poll_timeout + 20,
        )

    def send_message(self, chat_id, text):
        return self.call("sendMessage", {"chat_id": chat_id, "text": text}, timeout=30)

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
        timeout = 300 if getattr(self.cfg, "is_local_api", False) else 30
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
# live system status for the /status chat command
# --------------------------------------------------------------------------

_active_downloads = {}
_active_downloads_lock = threading.Lock()
_active_download_seq = 0


def start_download(name, chat_id=None, total=None):
    """Register a transfer so /status can report it while it runs."""
    global _active_download_seq
    with _active_downloads_lock:
        _active_download_seq += 1
        key = _active_download_seq
        _active_downloads[key] = {
            "id": key,
            "name": name,
            "chat_id": chat_id,
            "done": 0,
            "total": int(total or 0),
            "started": time.time(),
            "state": "queued",
            "cancelled": False,
        }
    return key


def begin_download(key):
    """Mark a queued transfer as the one currently downloading."""
    with _active_downloads_lock:
        rec = _active_downloads.get(key)
        if rec is None:
            return
        rec["state"] = "downloading"
        rec["started"] = time.time()


def update_download(key, done, total=None):
    with _active_downloads_lock:
        rec = _active_downloads.get(key)
        if rec is None:
            return
        rec["done"] = max(0, int(done))
        if total:
            rec["total"] = int(total)


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


def downloads_status_lines():
    """Lines for /status describing active downloads ([] when idle)."""
    downloads = active_downloads()
    if not downloads:
        return []
    lines = [
        f"downloads: {len(downloads)} queued/active (cancel with /cancel_upload <id>)"
    ]
    for rec in downloads:
        done = rec.get("done") or 0
        total = rec.get("total") or 0
        if rec.get("state") == "queued":
            detail = "queued"
        elif total:
            pct = max(0, min(100, int(done * 100 / total)))
            detail = f"{pct}% ({human_size(done)} / {human_size(total)})"
        else:
            detail = f"{human_size(done)} (size unknown)"
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


def chat_help_text(cfg, is_sys=False):
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
    else:
        lines += [
            "/status, /logs and /cancel_upload need sys access - ask the bot owner to run:",
            "  tgmessage allow sys <your-chat-id>",
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
            self.maybe_chat_command(cid, text)
        self.maybe_save_media(cid, msg)

    # ---- chat commands coming from Telegram ---------------------------
    def maybe_chat_command(self, cid, text):
        """Handle /commands typed in the Telegram chat itself."""
        cmd = (text or "").strip()
        if not cmd.startswith("/"):
            return
        head = cmd.split()[0].lower()
        if "@" in head:  # /status@thebot in group chats
            head = head.split("@", 1)[0]
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
        elif head in ("/help", "/start", "/commands"):
            threading.Thread(target=self._chat_help_reply, args=(cid,), daemon=True).start()

    def _record_out(self, cid, text, send=True):
        """Record an outgoing message; optionally deliver it to Telegram first."""
        result = None
        if send:
            try:
                result = self.tg.send_message(cid, text)
            except TelegramError as exc:
                log(self.cfg, f"reply to {cid} failed: {exc}")
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
        log(self.cfg, f"/cancel_upload {upload_id} requested by chat {cid}")
        rec = cancel_download(upload_id)
        if rec is None:
            text = f"no active upload with id {upload_id} - use /status to list them"
        elif rec.get("state") == "queued":
            text = f"cancelled queued upload #{upload_id}: {rec.get('name') or 'file'}"
        else:
            done = rec.get("done") or 0
            total = rec.get("total") or 0
            if total:
                detail = f"{human_size(done)} / {human_size(total)} received"
            else:
                detail = f"{human_size(done)} received"
            text = f"cancelled upload #{upload_id}: {rec.get('name') or 'file'} ({detail})"
        self._record_out(cid, text)

    def _chat_help_reply(self, cid):
        log(self.cfg, f"/help requested by chat {cid}")
        self._record_out(cid, chat_help_text(self.cfg, is_sys=self.can_sys(cid)))

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
            try:
                if download_cancelled(key):
                    raise DownloadCancelled()
                meta = self.tg.get_file(file_id)
                server_path = (meta or {}).get("file_path")
                if not server_path:
                    raise TelegramError("getFile returned no file_path")
                if not size:
                    size = (meta or {}).get("file_size")
                    if size:
                        update_download(key, 0, size)
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
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self._download_worker, daemon=True).start()
        threading.Thread(target=self.refresh_allowed, daemon=True).start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._stop)
        try:
            self.serve()
        finally:
            self._clear_pid()

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


def local_api_bin():
    return shutil.which("telegram-bot-api") or LOCAL_API_BIN


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
    binary = shutil.which("telegram-bot-api")
    if not binary and os.path.isfile(LOCAL_API_BIN):
        binary = LOCAL_API_BIN
    if not binary:
        print(
            f"! {LOCAL_API_BIN} is not installed yet.\n"
            "  Build it from https://github.com/tdlib/telegram-bot-api "
            "or use the installer in the project README.",
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
        binary = shutil.which("telegram-bot-api")
        print(f"{c['b']}local Bot API{c['rst']}")
        print(f"  binary        {binary or LOCAL_API_BIN + ' (MISSING)'}")
        print(f"  service       {local_api_service_state()}")
        print(f"  api id        {cfg.api_id or '(not set)'}")
        print(f"  api hash      {'***' + cfg.api_hash[-4:] if cfg.api_hash else '(not set)'}")
        print(f"  api root      {cfg.api_root or 'Telegram cloud (default)'}")
        print(f"  storage       {cfg.local_api_dir}")
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
  tgmessage allow sys <id> ...   + /status, /logs and /cancel_upload from Telegram
  tgmessage allow mang <id> ...  + watcher reports (manage)
  tgmessage deny <id> [...]      revoke everything for a chat
  tgmessage deny sys|mang <id>   revoke only that level

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

From the Telegram chat (sys access needed for the first three):
/status replies with a live server report (active uploads with ids, the services
you added with `tgmessage service add`, CPU + temp, fan speed, RAM, disk,
battery, IPs);
/logs replies with the tail of the watcher system log
(/root/tgmessage/watcher/logs/watcher.log; /logs 100 for more lines);
/cancel_upload <id> stops an active upload listed by /status and deletes its
partial file;
/help lists these commands, and every photo/file you send is saved
into the files/ folder (see TG_FILES_DIR in .env).
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
    print(f"  services      {', '.join(cfg.watch_services) or '(none - tgmessage service add)'}")
    print(f"  default chat  {cfg.default_chat or '(none)'}")
    print(f"  listening     {cfg.host}:{cfg.port}")
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
