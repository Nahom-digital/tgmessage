# tgmessage

Talk to your Telegram bot from any terminal - one session per chat - with a
self-healing watcher in the same app.

- `tgmessage` - the command you type, from anywhere
- `tgmessage.py daemon` - long-polls Telegram, keeps per-chat history, serves a local socket
- `tgmessage.py chat` - the interactive `Me>` / `Bot>` UI
- `watcher/` - repairs network/clock/DNS/disk, restarts the services you add
- everything runs under systemd and comes back after a reboot

## Quick start

Requirements: Debian/Ubuntu with systemd and python3 (standard library only,
no pip). You need root once, a token from
[@BotFather](https://t.me/BotFather), and a Telegram account.

```bash
git clone https://github.com/Nahom-digital/tgmessage /root/tgmessage
cd /root/tgmessage
sudo ./install.sh --token '123456789:AA...'   # installs bot + watcher

tgmessage requests                # message your bot first; your id shows here
tgmessage allow <your-chat-id>    # chat access
tgmessage allow sys <your-id>     # + /status, /logs and /cancel_upload
tgmessage allow mang <your-id>    # + watcher reports (recommended for you)

tgmessage service add <unit>      # watch a service (down -> restart + alert)
tgmessage status                  # bridge health
watcher check                     # machine health
```

Large files (>20 MB) need the optional local Bot API server:

```bash
sudo tgmessage local-api <api_id> <api_hash>   # see "Large files" below
```

## Permissions

`tgmessage allow <id>` only lets that chat talk to the bot. Higher levels are
opt-in and managed from the CLI (or `/allow` in the UI):

| command | gives |
|---|---|
| `tgmessage allow <id>` | chat, files, history |
| `tgmessage allow sys <id>` | + `/status`, `/logs` and `/cancel_upload` in Telegram |
| `tgmessage allow mang <id>` | + watcher reports (manage); `mang` implies `sys` |

Revoke with `tgmessage deny [sys\|mang] <id>`; a plain deny removes everything.
The watcher sends **nothing** until at least one `mang` chat exists.

## Telegram chat commands

`/help` works for every allowed chat. The first three need `sys` access.

| command | does |
|---|---|
| `/status` | server report: active downloads with ids + %, watched services, CPU, temp, fan, RAM, disk, battery, IPs |
| `/logs [n]` | last `n` lines of the watcher log (default 40) |
| `/cancel_upload <id>` | stop an active upload shown in `/status` and remove its partial file |
| `/help` (`/start`) | list the commands |
| photo / file | saved to `files/`, you get the path back |

## Large files (optional local Bot API)

Telegram's **cloud** Bot API only lets bots download files up to 20 MB. This is
a server-side Telegram limit, not a tgmessage setting. For larger files
(Telegram allows up to 2 GB per file), run Telegram's official local Bot API
server on the same machine. It stores the file locally, so tgmessage can copy
it from disk without the cloud download cap.

Build it once:

```bash
sudo apt-get install -y cmake g++ gperf libssl-dev zlib1g-dev
git clone --recursive https://github.com/tdlib/telegram-bot-api /opt/telegram-bot-api
cmake -S /opt/telegram-bot-api -B /opt/telegram-bot-api/build \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local
sudo cmake --build /opt/telegram-bot-api/build --target install -- -j1
```

Then save the app credentials from [my.telegram.org](https://my.telegram.org)
(`API development tools`) and start everything with one command:

```bash
sudo tgmessage local-api <api_id> <api_hash>
tgmessage local-api status          # binary, service, API root, size limits
sudo tgmessage local-api off        # go back to the cloud API
```

The command stores `TG_API_ID` / `TG_API_HASH` in `.env` (mode 600), installs
`telegram-bot-api.service`, stops tgmessage, logs the bot out of the cloud Bot
API (the required Telegram handoff), and starts the local daemon. If any step
fails, it restores the cloud config and restarts tgmessage there. `off` does
the reverse and logs the bot out of the local server first.

Downloads are written in 256 KiB chunks and handled by **one ordered worker**
(one file at a time), so large files do not exhaust RAM or saturate a low-end
CPU. The local server also runs at lower CPU/IO priority. `/status` shows
queued and active transfers with their ids; `/cancel_upload <id>` cancels
either one.

## Terminal commands

```bash
tgmessage                        # open the chat UI on the default session
tgmessage chat 123456789         # open the UI on one chat
tgmessage "hello"                # send one message
tgmessage --to 123456789 "hi"    # send to a specific chat
echo "piped text" | tgmessage    # send each line from stdin

tgmessage --FILE notes.txt       # send a file
tgmessage file notes.txt         # same thing
tgmessage sessions               # chats, permissions, message counts
tgmessage requests               # pending requests (or --request)

tgmessage allow <id> [...]             # chat access
tgmessage allow sys|mang <id> [...]    # raise the level
tgmessage deny [sys|mang] <id> [...]   # lower/remove access

tgmessage service add <unit> [...]     # watch a systemd service
tgmessage service remove <unit> [...]  # stop watching (alias: rem)
tgmessage service <unit> stop          # report only - never auto-restart it
tgmessage service <unit> restart       # back to auto-restart (default)
tgmessage service list                 # show watched services + mode

tgmessage log 40 [--chat 123]    # recent messages
tgmessage status                 # token/service/socket/sessions health
tgmessage local-api <id> <hash>  # local Bot API: files up to 2 GB (sudo)
tgmessage local-api status|off   # inspect or disable the local Bot API
tgmessage up | down | restart    # control the daemon by hand
watcher check                    # machine health report (no changes)
watcher once                     # full check WITH repairs + alerts
watcher log [n]                  # follow the watcher log
```

Inside the UI: `/session [n|id]`, `/allow [sys|mang] <id>`, `/deny <id>`,
`/requests`, `/copy [n|all]`, `/file <path> [caption]`, `//text` for a literal
slash. `Ctrl+C` quits, `Enter` sends, `Ctrl+Y` copies the newest reply.

## Watcher

The watcher keeps the machine alive with no one at the console. Every cycle it
repairs what it can and reports what it did to the `mang` chats:

- **network** - NIC renamed after a PC swap; brings up the best ethernet
  interface (ifup -> DHCP -> static fallback from `/etc/default/watcher`) and
  rewrites `/etc/network/interfaces` to match the real hardware
- **DNS** - rewrites `/etc/resolv.conf` when resolution breaks
- **clock** - restarts NTP; if still wrong, sets the time from an HTTPS
  `Date:` header (dead CMOS battery on an old PC)
- **services** - every unit you added with `tgmessage service add`: enables,
  `reset-failed`s and restarts it (throttled), alerts if it stays down. A
  unit set to report-only with `tgmessage service <unit> stop` is never
  touched - you only get a Telegram message when it is down.
- **disk** - remounts a read-only rootfs `rw`, vacuums the journal when
  `DISK_WARN_PCT` is reached
- **other failed units** - reported (not touched)

### Watched services

Nothing is watched until you add it - the list lives in `.env`, not in the
code:

```bash
tgmessage service add nginx ssh  # watch: enable, restart when down, alert
tgmessage service list           # state of each service + its mode
tgmessage service nginx stop     # report only - alert, but never restart it
tgmessage service nginx restart  # back to auto-restart (default)
tgmessage service rem nginx      # remove it from the watch list
```

A newly added service gets auto-restart by default. `stop` is per unit and
just sends you a Telegram message when it is down. Both live in
`TG_WATCH_SERVICES` / `TG_SERVICE_NO_RESTART` and apply on the next watcher
cycle - no watcher restart needed.

Watcher messages go straight to the Bot API (token from `.env`), so they work
even if the tgmessage daemon is down; while offline they queue in
`watcher/state/outbox.jsonl`. Only `TG_MANG_CHAT_IDS` receive them.

Config: `/etc/default/watcher` (`WATCH_INTERVAL`, `NOTIFY`, `TG_HOME`,
`STATIC_IP`, `STATIC_GW`, `DNS_SERVERS`, `DISK_WARN_PCT`).

## Files

```
/root/tgmessage/
    .env                 token, permissions, watched services, host, port
    tgmessage            shell wrapper (symlinked to /usr/local/bin)
    tgmessage.py         daemon, UI, /status + /logs, allow/service commands
    tgmessage.service.in systemd unit template
    install.sh           installer / uninstaller
    files/               files/photos received from Telegram
    /var/lib/telegram-bot-api/  local Bot API file store (optional)
    watcher/
        watcher.py         the caretaker (network/clock/DNS/disk/services)
        watcher.sh         shell front-end (also /usr/local/bin/watcher)
        install.sh         (re)install the watcher units
        systemd/           watcher units that get installed
        state/             watcher state, backups, Telegram outbox
        logs/watcher.log   the watcher log (shown by /logs)
    state/
        <chatid>.jsonl     one history file per allowed chat
        pending.json       chats waiting to be allowed
        chats.json         chat metadata
        offset.json        Telegram update offset
        daemon.log         daemon log (also: journalctl -u tgmessage)
```

`.env` keys:

```
TG_BOT_TOKEN=...
TG_API_ROOT=                # empty = cloud; local: http://127.0.0.1:8081
TG_API_ID=                  # local Bot API credentials (my.telegram.org)
TG_API_HASH=
TG_MAX_DOWNLOAD_MB=         # empty = 20 cloud / 2000 local
TG_MAX_UPLOAD_MB=           # empty = 50 cloud / 2000 local
TG_ALLOWED_CHAT_IDS=...     # chat access
TG_SYS_CHAT_IDS=...         # /status + /logs
TG_MANG_CHAT_IDS=...        # watcher reports
TG_WATCH_SERVICES=...       # services the watcher watches
TG_SERVICE_NO_RESTART=...   # watched services it should only report (no restart)
TG_DEFAULT_CHAT=...
TG_HOST=127.0.0.1
TG_PORT=8765
```

## Troubleshooting

```bash
tgmessage status                 # start here for the bridge
tgmessage requests               # who is waiting to be allowed
journalctl -u tgmessage -n 50    # or: tail -f state/daemon.log

watcher check                    # machine-level health report
watcher log                      # follow the watcher log
journalctl -u watcher -n 50      # or: tail -f watcher/logs/watcher.log
```

- **nothing shows up** - the chat is probably not allowed: `tgmessage requests`,
  then `tgmessage allow <chatid>`.
- **`/status` says permission denied** - grant sys: `tgmessage allow sys <id>`.
- **no watcher messages** - grant mang: `tgmessage allow mang <id>`.
- **daemon not reachable** - `tgmessage up`.
- **token rejected** - re-copy it from BotFather and run
  `tgmessage --token '<token>'`.
- **files over 20 MB are rejected** - the cloud Bot API limit:
  `sudo tgmessage local-api <api_id> <api_hash>`, then check
  `tgmessage local-api status` and `journalctl -u telegram-bot-api -n 50`.
- **local Bot API stopped** - `sudo systemctl restart telegram-bot-api`.

## Uninstall

```bash
cd /root/tgmessage
sudo ./install.sh --uninstall                # removes both services + bin links
sudo ./install.sh --uninstall --keep-watcher # keeps the watcher
```

`.env`, `state/` and `files/` are never deleted by the uninstaller.
