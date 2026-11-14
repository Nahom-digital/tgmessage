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
tgmessage allow sys <your-id>     # + /status, /logs, /cancel_upload, /ai
tgmessage allow mang <your-id>    # + watcher reports (recommended for you)
tgmessage shell on                # optional: allow /sh-on root shell for sys chats

tgmessage service add <unit>      # watch a service (down -> restart + alert)
tgmessage status                  # bridge health
watcher check                     # machine health
```

Large files (>20 MB) need the optional local Bot API server:

```bash
sudo tgmessage local-api <api_id> <api_hash>   # see "Local Bot API" below
```

## Permissions

`tgmessage allow <id>` only lets that chat talk to the bot. Higher levels are
opt-in and managed from the CLI (or `/allow` in the UI):

| command | gives |
|---|---|
| `tgmessage allow <id>` | chat, files, history |
| `tgmessage allow sys <id>` | + `/status`, `/logs`, `/cancel_upload`, `/ai` and the opt-in root shell in Telegram |
| `tgmessage allow mang <id>` | + watcher reports and `/recover` (manage); `mang` implies `sys` |

Revoke with `tgmessage deny [sys\|mang] <id>`; a plain deny removes everything.
The watcher sends **nothing** until at least one `mang` chat exists.

The root shell is **off by default** even for sys chats. Enable it explicitly
with `tgmessage shell on` (see [Root shell](#root-shell-opt-in-dangerous)).

## Telegram chat commands

`/help` works for every allowed chat. `/status`, `/logs`, `/cancel_upload`,
`/ai` and the root shell need `sys` access; `/recover` needs `mang` access.

| command | does |
|---|---|
| `/status` | server report: queued/active transfers with ids, %, downloaded/total size, speed, watched services, CPU, temp, fan, RAM, disk, battery, IPs |
| `/logs [n]` | last `n` lines of the watcher log (default 40) |
| `/cancel_upload <id>` | stop a queued/active transfer shown in `/status` and remove its partial file |
| `/recover [unit]` | try hard to start a down watched service (unmask/enable if needed, reset-failed, start, retry); without a unit it recovers every down watched service and appends the last journal lines of any service that stays down |
| `/sh_on` (`/sh-on`) | open an opt-in root shell in this private chat; every following plain message is run as a command |
| `/sh_off` (`/sh-off`) | close the root shell and kill a command that is still running |
| `/ai` | start AI mode: talk to the pi coding harness in the saved working directory |
| `/ai_exit` (`/ai-exit`) | stop AI mode and close the pi process |
| `/model` | in AI mode, list models as Telegram buttons |
| `/thinking` | in AI mode, list reasoning levels as Telegram buttons |
| `/session` (`/resume`) | in AI mode, list saved pi sessions for the current cd as Telegram buttons and resume one |
| `/ai-status` | in AI mode, show context usage and input/output/total tokens |
| `/ai-agent` | show the rules injected into the AI at session start |
| `/ai-agent-add <text>` | add a persistent rule for this chat |
| `/ai-agent-remove <n\|text>` | remove a persistent rule by number or text |
| `/btw <text>` | in AI mode, ask a quick side question without changing the main session |
| `/live` | show running pi sessions and their subagents in the current cd |
| `/help` (`/start`) | list the commands |
| photo / file | saved to `files/`, you get the path back |

The bot registers this list in Telegram's `/` command menu on startup.
Telegram only allows lowercase letters, digits and underscores in menu
commands, so the menu shows `/sh_on` and `/sh_off`; `/sh-on` and `/sh-off`
still work if typed. Sync it manually with `tgmessage commands`.

### Root shell (opt-in, dangerous)

`/sh-on` gives the chat **full control of the machine as root** - the same
privileges as the tgmessage systemd service. It is disabled by default and
must be enabled from the console:

```bash
tgmessage shell on       # allow /sh-on for sys chats
tgmessage shell status   # state + timeout settings + sys chats
tgmessage shell off      # refuse /sh-on and close every open shell now
```

When enabled, only chats with `sys` access and only **private** chats can use
it. Groups, non-sys chats and the terminal UI are refused.

While a shell is on, send commands as ordinary messages (no leading `/`);
known bot commands (`/status`, `/logs`, `/recover`, `/help`) still work.
`/sh_off` closes the session immediately. Shell replies are sent as Telegram
`<pre>` code blocks, so long output is monospace and has Telegram's copy button.
Each command runs in a clean non-login `bash`; `cd` is remembered between
messages, but exported variables, aliases and shell functions are not.
Interactive programs are not supported.

Active shell sessions are saved in `state/shell.json` (mode `600`) and are
restored when the tgmessage **service** restarts, including their `cd`
directory and remaining idle/max lifetime. A full machine reboot closes them
(the boot id is recorded), and revoking `sys`, `tgmessage shell off`, or
`/sh_off` removes them.

Safety limits (all configurable in `.env`):

| setting | default | meaning |
|---|---|---|
| `TG_SHELL_ENABLED` | `0` | feature gate; `/sh-on` is refused while off |
| `TG_SHELL_IDLE_SECONDS` | `600` | close the shell after this much inactivity |
| `TG_SHELL_MAX_SECONDS` | `1800` | maximum lifetime of one shell session |
| `TG_SHELL_TIMEOUT` | `120` | kill one command after this many seconds |

Also enforced in code:

- one command at a time per chat, at most 2 commands total
- 20 commands per minute per chat
- 16 KB of output kept per command; extra output is discarded, so `apt upgrade`
  is not interrupted, but a runaway that reads 64 MB is killed
- stdin is `/dev/null`, so interactive programs fail instead of hanging forever
- output is split into Telegram-sized messages
- edited messages are never executed (a command cannot run twice)
- revoking `sys`, running `tgmessage shell off`, or restarting the daemon
  closes live shells; `/sh-off` also kills a running command
- every command and exit status is written to `state/daemon.log`

The command runs with the daemon's OS user (root when installed with the
provided service). Output and commands are stored by Telegram and in
`state/<chatid>.jsonl`, so do not run commands whose output contains secrets
you do not want in those places. For anything long-running, use `nohup`/tmux
or a systemd unit instead of keeping a chat shell open.

### AI mode (`/ai`)

`/ai` connects a private sys chat to the [pi](https://github.com/earendil-works/pi)
coding harness. It starts `pi --mode rpc` in the saved working directory, so
`cd` from a previous `/sh-on` session is used even after `/sh-off`. `/status`
always shows that `cd`.

While AI mode is on, normal messages are prompts for pi. Thinking text and
tool calls are hidden; a `thinking....` message is shown while pi works and
the assistant's normal reply is sent to the chat. `/model` and `/thinking`
open inline Telegram buttons, `/session` (also `/resume`) lists saved pi
sessions for the current cd with buttons and resumes the one you pick, `/new`
starts a fresh pi session, `/stop` aborts a run, `/compact` compacts context
and `/pi <text>` sends text or a pi slash command explicitly. `/ai-status`
shows context usage and token counts. `/status`, `/logs`, `/recover` and
`/cancel_upload` still run the server commands. `/ai-exit` stops AI mode.
`/btw <text>` asks a quick side question without adding it to the main
conversation. `/live` shows running pi sessions in the current cd, how long
they have been running, and the subagents under them.

AI conversations are saved as normal pi sessions, so `/session` also shows
sessions you created in the pi terminal for the same current directory.

### AI rules and Telegram control

`tgmessage.md` is injected into every new pi session with
`--append-system-prompt`. It tells the AI that it is running in Telegram and
how to control the response with `<tg>` control blocks. The AI can send plain
messages, code blocks, raw Telegram HTML, typing actions and inline buttons.
The AI is also told to send a short status before a big task and short
progress updates during long work.

```text
<tg>
{"actions":[
  {"type":"status","text":"Running the tests..."},
  {"type":"code","text":"pytest -q"},
  {"type":"buttons","text":"Continue?","buttons":[{"label":"Yes","value":"yes"},{"label":"No","value":"no"}]}
]}
</tg>
```

The bot removes the block and executes the actions. A button press sends its
`value` back to the AI as a new user message.

`/ai-agent` shows the default rules and the custom rules for this chat.
`/ai-agent-add <text>` appends a custom rule and `/ai-agent-remove <n|text>`
removes one. Custom rules are saved under `state/ai-agent-<chatid>.json` and
are injected into every new AI session for that chat. A running session gets
the change as an immediate rule update message.

`/ai` is refused while a root shell is on; run `/sh-off` first. It only works
in a private chat with `sys` access. The pi executable is found on `PATH`; set
`TG_PI_BIN` if it is somewhere else. Optional `TG_AI_MODEL` and `TG_AI_PROVIDER`
choose the starting model.

### Side questions and live sessions

`/btw <text>` checks that `@narumitw/pi-btw` is installed. If it is missing,
the bot sends the install command. pi-btw itself only opens its side thread in
the interactive terminal, so the Telegram bridge runs the same side-question
prompt and recent conversation context through a throwaway `pi -p` process.
The main session is not changed. `btw-system-prompt.md` holds that side prompt.

`/live` reads heartbeat data from `tg-live.ts` and scans `/proc` for pi
processes in the current working directory. It shows only live sessions, how
long each has been running, and the subagents under the current AI session.
Running subagents show their elapsed time; finished subagents show their total
run time. The observer extension is loaded into every AI pi process with
`--extension`. Without it, `/live` still shows the sessions but no subagent
details.

Both files must stay next to `tgmessage.py`:

- `btw-system-prompt.md` - side-question system prompt
- `tg-live.ts` - pi extension that mirrors live subagent records

## Local Bot API (large files)

Telegram's **cloud** Bot API only lets bots download files up to 20 MB. This is
a server-side Telegram limit, not a tgmessage setting. The official local Bot
API server removes that download cap (uploads up to 2 GB are supported too).
It runs beside tgmessage on the same machine and stores incoming files in
`TG_LOCAL_API_DIR`, so tgmessage can read them directly from disk.

### Enable it

Create an app at [my.telegram.org](https://my.telegram.org)
(`API development tools`) and copy its **api_id** / **api_hash**. Then run:

```bash
sudo tgmessage local-api <api_id> <api_hash>
tgmessage local-api status          # binary, service, API root, size limits
sudo tgmessage local-api off        # go back to the cloud API
```

If `/usr/local/bin/telegram-bot-api` is missing, the command **builds and
installs it automatically** and shows live progress (`[ 10%] ...`). It installs
the build dependencies, clones the official source to `TG_LOCAL_API_SRC`, and
builds with `TG_LOCAL_API_JOBS` (default `1`) at low CPU/IO priority. The bot
stays online on the cloud API during the build, and the full output is kept in
`state/local-api-build.log`.

That command:

- stores the API credentials in `.env` with mode `600`
- builds the server if needed, showing live progress
- installs and starts `/etc/systemd/system/telegram-bot-api.service`
- binds the server only to `127.0.0.1` on `TG_LOCAL_API_PORT` (8081 by
  default), with lower CPU/IO priority
- stops tgmessage, logs the bot out of the cloud Bot API (Telegram's required
  handoff), and restarts tgmessage in local mode
- automatically restores the cloud configuration if the switch fails

If you prefer to build the server yourself:

```bash
sudo apt-get install -y cmake g++ gperf libssl-dev zlib1g-dev
git clone --recursive https://github.com/tdlib/telegram-bot-api /opt/telegram-bot-api
cmake -S /opt/telegram-bot-api -B /opt/telegram-bot-api/build \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local
sudo nice -n 19 cmake --build /opt/telegram-bot-api/build --target install -- -j1
```

`tgmessage local-api off` logs out of the local server first, then switches
back to the cloud API.

### Configuration

Normally `tgmessage local-api` manages these; they are listed so you can tune
the server or recover a manual setup. The local-mode values go in `.env`:

| key | default | meaning |
|---|---|---|
| `TG_API_ROOT` | empty (cloud) | `http://127.0.0.1:8081` for local mode |
| `TG_API_ID` | empty | Telegram app id from my.telegram.org |
| `TG_API_HASH` | empty | Telegram app hash; keep it secret |
| `TG_LOCAL_API_PORT` | `8081` | port the local server listens on; must match `TG_API_ROOT` |
| `TG_LOCAL_API_DIR` | `/var/lib/telegram-bot-api` | local server storage; needs room for large files |
| `TG_LOCAL_API_SRC` | `/opt/telegram-bot-api` | source dir; cloned there automatically if missing |
| `TG_LOCAL_API_JOBS` | `1` | build jobs; keep `1` on a low-end machine |
| `TG_MAX_DOWNLOAD_MB` | `20` cloud / `2000` local | maximum size per downloaded file |
| `TG_MAX_UPLOAD_MB` | `50` cloud / `2000` local | maximum size per uploaded file |

Example `.env` for local mode:

```ini
TG_API_ROOT=http://127.0.0.1:8081
TG_API_ID=12345678
TG_API_HASH=0123456789abcdef0123456789abcdef
TG_LOCAL_API_PORT=8081
TG_LOCAL_API_DIR=/var/lib/telegram-bot-api
TG_LOCAL_API_SRC=/opt/telegram-bot-api
TG_LOCAL_API_JOBS=1
TG_MAX_DOWNLOAD_MB=2000
TG_MAX_UPLOAD_MB=2000
```

If you change `TG_LOCAL_API_PORT`, also update `TG_API_ROOT` and restart both
services. Put `TG_LOCAL_API_DIR` on a disk with enough free space **before**
enabling local mode; the local server stores its download there and tgmessage
hardlinks it into `files/` when both paths are on the same filesystem (it
falls back to a chunked copy otherwise).

Inspect the generated unit with `systemctl cat telegram-bot-api` and follow its
log with `journalctl -u telegram-bot-api -f`.

Downloads are handled by **one ordered worker** (one file at a time) in
256 KiB chunks, so large files do not exhaust RAM or saturate a low-end CPU.
`/status` shows queued and active transfers with their ids, percent, downloaded
and total size, and current speed. While the local Bot API is fetching a large
file, tgmessage reads the server's growing temp file so the percentage and
speed update before `getFile` returns.
`/cancel_upload <id>` cancels either one. Because the local server's `getFile`
blocks until its fetch finishes, cancelling an active local download briefly
restarts `telegram-bot-api` to abort that fetch; the bot reconnects
automatically and the partial temp file is removed.

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
tgmessage shell on|off|status          # enable/disable the Telegram root shell
tgmessage commands [register|show]     # sync the Telegram '/' command menu

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
    .env                 token, permissions, watched services, bot API settings
    tgmessage            shell wrapper (symlinked to /usr/local/bin)
    tgmessage.py         daemon, UI, /status + /logs, allow/service commands
    tgmessage.md         AI rules injected into every pi session
    btw-system-prompt.md system prompt for /btw side questions
    tg-live.ts           pi extension that writes live session/subagent state
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
        offset.json        update offset per Bot API server (cloud/local)
        live/              live session heartbeats from tg-live.ts
        daemon.log         daemon log (also: journalctl -u tgmessage)
```

`.env` keys:

```
TG_BOT_TOKEN=...
TG_API_ROOT=                # empty = cloud; local: http://127.0.0.1:8081
TG_API_ID=                  # local Bot API app credentials (my.telegram.org)
TG_API_HASH=                # keep this secret
TG_LOCAL_API_PORT=8081      # local server port (must match TG_API_ROOT)
TG_LOCAL_API_DIR=/var/lib/telegram-bot-api  # local server file store
TG_LOCAL_API_SRC=/opt/telegram-bot-api      # source; auto-cloned if missing
TG_LOCAL_API_JOBS=1         # build jobs (1 = low-end friendly)
TG_MAX_DOWNLOAD_MB=         # empty = 20 cloud / 2000 local
TG_MAX_UPLOAD_MB=           # empty = 50 cloud / 2000 local
TG_ALLOWED_CHAT_IDS=...     # chat access
TG_SYS_CHAT_IDS=...         # /status, /logs, /cancel_upload, /ai + root shell
TG_MANG_CHAT_IDS=...        # watcher reports
TG_SHELL_ENABLED=0          # 1 = allow /sh-on for sys chats (dangerous)
TG_SHELL_IDLE_SECONDS=600   # close an idle shell after 10m
TG_SHELL_MAX_SECONDS=1800   # maximum shell lifetime (30m)
TG_SHELL_TIMEOUT=120        # kill one command after 2m
TG_AI_ENABLED=1             # 1 = allow /ai for sys chats
# TG_PI_BIN=/root/.pi/agent/bin/pi
# TG_AI_MODEL=deepseek-v4.1-flash
# TG_AI_PROVIDER=opencode-go
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
- **`/sh-on` says the root shell is disabled** - enable it from the console:
  `tgmessage shell on`; check with `tgmessage shell status`.
- **`/sh_on` refuses the chat** - it only works in a private chat with `sys`
  access, never in a group. `/sh-on` works too if typed.
- **the `/` menu is empty or stale** - run `tgmessage commands`, then reopen
  the Telegram chat and press `/`.
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
