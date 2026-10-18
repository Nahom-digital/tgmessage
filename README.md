# tgmessage

Talk to your Telegram bot from any terminal - **one session per chat** - with a
self-healing server watcher built into the same app: if the network dies, the
clock jumps, a service crashes or the disk fills up, the watcher fixes it and
tells you on Telegram. Its log is one `/logs` away, right in the chat.

| piece | what it does |
|---|---|
| `tgmessage` (shell wrapper) | the command you type, from anywhere |
| `tgmessage.py daemon` | long-polls Telegram, keeps per-chat history, serves a local socket |
| `tgmessage.py chat` | the interactive `Me>` / `Bot>` UI, attaches to the daemon |
| `watcher/` | the caretaker: repairs network/clock/DNS/services/disk, reports over Telegram |

The daemon is a systemd service, so it **comes back after a reboot** and keeps
receiving while you are logged out. Messages that arrive while you are away are
waiting for you the next time you open the UI.

The watcher is also systemd - two units:

- `watcher-boot.service` - early oneshot, fixes NIC rename + clock **before**
  `cloudflared`/`tgmessage` start (runs before `network-pre.target`)
- `watcher.service` - the loop; restarts itself forever (`Restart=always`,
  `StartLimitIntervalSec=0`)

## Deploy on your server (quick start)

Requirements: a Debian/Ubuntu server with **systemd** and **python3** (only the
standard library is used - no pip, no venv). You need root once for the
install, a token from [@BotFather](https://t.me/BotFather), and a Telegram
account to talk to the bot.

```bash
# 1. put the code on your server (replace <repository-url>)
git clone <repository-url> /root/tgmessage
cd /root/tgmessage

# 2. install EVERYTHING (bot bridge + watcher) and start it
sudo ./install.sh --token '123456789:AA...'      # token from @BotFather

# 3. message your bot once from Telegram, then allow your chat
tgmessage requests               # shows your chat id as a pending request
tgmessage allow <your-chat-id>   # allow it (saved to .env)

# 4. verify both halves
tgmessage status                 # bridge: service / socket / telegram
watcher check                    # machine: network, services, clock, disk
```

That's it - after this both parts **start automatically on every boot** and
restart themselves if they crash. In Telegram, send `/status` for a live
server report, `/logs` for the watcher's system log, or just chat.

No token yet? Run `sudo ./install.sh` first and set the token later with
`tgmessage --token '<token>'` - it updates `.env` and restarts the daemon.

## Install

Everything (the Telegram bridge AND the watcher) installs from this folder:

```bash
cd /root/tgmessage
sudo ./install.sh --token '123456789:AA...'
```

That does these things:

1. writes `.env` (token + port) without clobbering what is already there,
2. symlinks `tgmessage` and `watcher` into `/usr/local/bin`,
3. installs and enables `tgmessage.service` (reboot survival),
4. installs and enables `watcher.service` + `watcher-boot.service`, runs one
   boot-repair pass,
5. starts everything and prints a health check.

No `pip install` needed - the standard library is all it uses.

- `sudo ./install.sh` - reinstall/refresh using the existing `.env` (safe to
  re-run anytime).
- `sudo ./install.sh --no-watcher` - Telegram bridge only, leave the watcher
  alone.
- `sudo ./install.sh --uninstall` - remove the services and the bin links
  (`--keep-watcher` leaves the watcher installed). Your `.env`, `state/` and
  `files/` are never deleted.

## How to start / stop / check

Normally you never start them by hand: both parts start at boot and restart
themselves on crash. When you do want to poke them:

```bash
# the systemd way
systemctl start|stop|restart tgmessage     # Telegram bridge
systemctl start|stop|restart watcher       # caretaker loop
systemctl status tgmessage                 # or: tgmessage status
systemctl status watcher                   # or: watcher status

# the wrappers
tgmessage up | down | restart              # tgmessage.service via systemd
tgmessage status                           # full health check of the bridge
watcher start | stop | restart             # watcher.service via systemd
watcher check                              # status report (changes nothing)
watcher once                               # full check WITH repairs + alerts
watcher log [n]                            # follow the watcher log

# run by hand, no systemd
tgmessage daemon                           # bridge in the foreground
tgmessage                                  # open the Me>/Bot> UI
watcher run                                # caretaker loop in the foreground
```

## Who is allowed to talk to the bot

A bot can be messaged by anyone, but `tgmessage` only relays chats you explicitly
allow. The allowed ids live in `.env` (`TG_ALLOWED_CHAT_IDS`).

When someone the daemon does not know sends the bot a private message, it is held
as a **request** (`state/pending.json`) - its text is *not* added to any history
and nothing is sent back. You review and decide:

```bash
tgmessage --request            # or: tgmessage requests
tgmessage --allow 123456789    # or: tgmessage allow 123456789
tgmessage --deny  123456789    # revoke access later
```

Each allowed chat gets its own history file, `state/<chatid>.jsonl`, and its own
session in the UI:

```bash
tgmessage sessions             # list chats, allowed vs request, message counts
tgmessage                      # open the UI on the default session
tgmessage chat 123456789       # open the UI straight on one chat
```

Inside the UI:

| command | does |
|---|---|
| `/session` | list sessions (and any pending requests) |
| `/session 2` | switch to session number 2 |
| `/session 123456789` | switch by chat id |
| `/allow <id>` / `/deny <id>` | allow / revoke a chat (saved to `.env`) |
| `/requests` | show who is waiting to be allowed |

Messages from a session you are *not* currently viewing still appear, prefixed
with `[name]`, so you never miss one. The prompt shows the active chat:
`Me[@username]>`.

## Commands you can type in the Telegram chat itself

The daemon watches for these no matter which terminal you are using (or when
none is open). They only work in allowed chats.

| command | does |
|---|---|
| `/status` | reply with a live server report: every service (watcher, ssh, networking, cloudflared, https-ssh, tgmessage, postgresql, cron), CPU load, CPU temperature, fan speed (+ % of max), RAM, disk usage, battery, and the private + public IP address |
| `/logs [n]` | reply with the last `n` lines of the **watcher system log** (default 40). This is the same file as `watcher/logs/watcher.log`; if it is missing, the daemon falls back to `journalctl -u watcher.service`. Long output is trimmed to the newest lines (Telegram caps messages at 4096 chars). |
| `/help` (`/start`) | reply with the list of chat commands |
| any photo / file | **saved automatically** to `files/` (any format: `.sh`, `.txt`, `.zip`, images, audio, video; up to 20 MB - Telegram Bot API limit). You get a confirmation with the saved path. Change the folder with `TG_FILES_DIR` in `.env`. |

Fan speed comes from `/sys/class/hwmon/*/fan*_input` (or a ThinkPad
`/proc/acpi/ibm/fan` tachometer as a fallback). When the sensor also publishes
its full-speed value (`fan*_max`), the reply also shows how hard the fan is
working, as a percentage of its maximum; without a `fan*_max` attribute only
the raw RPM is shown. `fan: n/a (fan speed sensor hardware fault - EC 0xFFFF)`
means the fan tachometer is not reaching the embedded controller (the "no tach
reading" sentinel): the fan may still spin, but its speed cannot be read. That
is a physical problem - a loose fan connector or a worn/aftermarket fan - not
something tgmessage can work around. Once the hardware reports again, the RPM
(and % of max, where published) appears on its own.

Chats are shown by **@username** wherever one exists (fetched from Telegram with
`getChat`), falling back to the contact's display name, then the numeric id.
The numeric id is only shown where it is needed - the `/allow <id>` hint in the
requests list.

## The watcher (self-healing caretaker)

The watcher exists because this disk can be a **portable server disk** (Debian,
headless, possibly no working keyboard): it may be moved between PCs, and a
different PC means a different NIC name, maybe a dead clock, maybe missing
network firmware - with no user sitting at the console to fix anything.

Everything the user cares about must come up **by itself after every boot**:

| thing | what must work |
|---|---|
| networking / DHCP | internet without touching anything |
| SSH | sshd on port **2208** |
| cloudflared | the named tunnel only |
| https-ssh | gateway on 127.0.0.1:**9000** |
| tgmessage | Telegram bridge on 127.0.0.1:**8765** |
| everything else | enabled + running + restart on crash |

### What the watcher fixes by itself

1. **NIC renamed because of the PC swap** (e.g. `enp0s31f6` → `eth0`).
   Detects physical ethernet cards, brings the best one up and tries:
   `ifup` (if configured) → **DHCP** → a static fallback address
   (`STATIC_IP` / `STATIC_GW` in `/etc/default/watcher`). On success it
   **rewrites `/etc/network/interfaces`** to match the real hardware, so the
   next boot is clean. Old file is backed up in `watcher/state/interfaces.bak`.
2. **No cable / no DHCP / unknown LAN** - keeps retrying every cycle and
   reports on Telegram. If even the router is unreachable it says so
   (can't fix physics, but you know).
3. **DNS broken** but internet fine - rewrites `/etc/resolv.conf`
   (backup in `watcher/state/resolv.conf.bak`).
4. **Clock wrong** (old PC = dead CMOS battery → TLS fails everywhere) -
   restarts NTP, and if still wrong sets the time from an HTTPS `Date:`
   header. cloudflared/Telegram then recover.
5. **Service crashed / got disabled / was masked** - every cycle checks
   ssh, cloudflared, https-ssh, tgmessage, postgresql, cron, gpm,
   systemd-timesyncd: `enable`s, `reset-failed`s and restarts
   (throttled to one restart / 5 min per unit) and reports what it did.
6. **Port dead while process alive** (2208, 9000, 8765) - one restart, and
   an alert if the port stays closed.
7. **sshd port 2208 missing from config** - appends it, `sshd -t`, restart
   (config backup in `watcher/state/sshd_config.bak`).
8. **Root filesystem remounted read-only** after an I/O error - remounts it
   `rw` and tells you to check the disk.
9. **Disk almost full** - vacuums the systemd journal and alerts.
10. **Other failed systemd units** - reported (not touched, could be
    user stuff).
11. **Every boot** - sends one full status report to Telegram (host, IPs,
    all services, problems). Only the **named tunnel** is used - the
    temporary quick tunnel is stopped and disabled.
12. **Watcher itself** - systemd restarts it forever; it only ever logs and
    alerts, it never exits on errors.

Watcher Telegram messages are sent **directly via the Bot API** (token from
`.env`), so they arrive even if the tgmessage daemon is down. While there is
no network they are queued in `watcher/state/outbox.jsonl` and delivered later.
The same messages are also written to its log, which is what the Telegram
`/logs` command shows.

### Watcher commands

```
watcher check            status report (changes nothing)
watcher once             full check WITH repairs + alerts
watcher boot-repair      the early repair pass by hand
watcher log [n]          follow the watcher log
watcher start|stop|restart|status
watcher notify "text"    send one Telegram message
su - && /root/tgmessage/watcher/install.sh    re-install/refresh the watcher units
su - && /root/tgmessage/watcher/prep.sh       pre-download NIC firmware (while online)
```

Config: `/etc/default/watcher` (interval, NOTIFY on/off, static fallback IP,
DNS servers, disk warning level). Change a value, then
`systemctl restart watcher`.

### After moving the disk to another PC

Expected flow: GRUB → kernel → `watcher-boot` fixes the new NIC + clock →
cloudflared/tgmessage start → Telegram gets a "booted up" report with the LAN
IP and per-service status. If something needs hands, the report says exactly
what.

### Things no watcher can fix (check these once, in the BIOS)

- **Boot mode**: this disk boots **both ways** - BIOS (legacy) and UEFI
  (GPT, ESP on partition 1, signed `/EFI/BOOT/BOOTX64.EFI` fallback loader,
  Secure Boot works, 1 MiB `BIOS boot` partition + GRUB i386-pc on the MBR).
  Any old PC that can see the disk should boot it. If a BIOS-only machine
  still refuses, run `/root/tgmessage/watcher/rescue-bios-boot.sh` from a
  live USB (it detects the existing BIOS-boot setup and re-installs GRUB if
  needed).
- **"Power On after AC power loss"** → set it to *Power On* in the BIOS, so
  a power cut doesn't leave the PC off until someone presses the button.
- **Wake on LAN** (optional) if you want to start it remotely.
- The PC's **network card must be cable-connected**; WiFi is not configured
  (no credentials files). Firmware for many NICs/WiFi chips can be
  pre-installed by `watcher prep`.

## Use (terminal side)

```bash
tgmessage                        # open the chat UI on the default session
tgmessage chat 123456789         # open the UI on one chat
tgmessage "hello"                # send one message to the default chat
tgmessage --to 123456789 "hi"    # send one message to a specific chat
echo "piped text" | tgmessage    # send each line from stdin
tgmessage --FILE notes.txt       # send a file to the default chat
tgmessage --to 123 --FILE a.txt --caption "here you go"
tgmessage file notes.txt         # same as --FILE

tgmessage sessions               # list chats with counts
tgmessage requests               # pending requests (or --request)
tgmessage allow 123 456          # allow one or more chats (saved to .env)
tgmessage deny 123               # revoke one or more chats
tgmessage log 40                 # last 40 messages across all chats
tgmessage log 40 --chat 123      # ... just one chat
tgmessage status                 # token / service / socket / sessions health
tgmessage restart                # bounce the daemon by hand
```

Keys inside the UI:

| key | does |
|---|---|
| `Enter` | send |
| `Ctrl+C` | quit |
| `Ctrl+Y` | copy the newest reply of the active session |
| `↑` / `↓` | browse what you sent before |
| `←` / `→`, `Home` / `End` | move the cursor |
| `Ctrl+U` / `Ctrl+K` | clear the line / kill to the end |
| `Ctrl+W` | delete the previous word |

Typed commands (intercepted only when they match exactly, so `/help`,
`/start` and friends still reach your bot):

| command | does |
|---|---|
| `/session [n\|id]` | list sessions / switch to one |
| `/allow <id>` / `/deny <id>` | allow / revoke a chat |
| `/requests` | show pending requests |
| `/copy` | copy the newest reply of the active session |
| `/copy 3` | copy the 3rd newest message of the session |
| `/copy all` | copy the whole active conversation |
| `/file <path> [caption]` | send a file to the active session |
| `//text` | send a literal `/text` |

Incoming messages are painted above your draft without eating what you typed.

### Copy

Copy uses OSC 52, so it works over SSH and in containers with no X server and
no clipboard daemon - the escape sequence travels up your connection and the
emulator you are typing in fills its own clipboard. Your terminal needs to
support it (kitty, alacritty, wezterm, ghostty, foot, Windows Terminal,
Konsole, GNOME Terminal do; iTerm2 needs *Preferences -> General -> Selection
-> Applications in terminal may access clipboard*). Inside tmux add
`set -g set-clipboard on`.

Mouse selection still works as usual; `/copy` is the no-mouse path. `/copy`
operates on the session you are currently viewing.

### Paste

Paste is your terminal's own paste (`Ctrl+Shift+V`). The UI turns on
**bracketed paste**, so a multi-line paste arrives as one message instead of
submitting once per line. The edit line is single-line, so pasted newlines
become spaces - for a genuinely multi-line message use `cat file.txt | tgmessage`.

There is no `/paste`: reading the clipboard with OSC 52 is widely disabled
(it is a security hole), so paste stays with the terminal, which owns it.

### Files

Send any file as a Telegram document, from the shell or from inside the UI:

```bash
tgmessage --FILE report.txt
tgmessage --to 123 --FILE report.txt --caption "the numbers you asked for"
tgmessage file report.txt                 # same thing
```

```
Me[@username]> /file report.txt please review
Me[@username]> /file "notes with spaces.txt"
```

You get a plain `sendDocument` upload, so a `.txt` arrives as a downloadable
`.txt` (not reformatted into a chat bubble). Limits and notes:

- **50 MB** is the Telegram bot-API maximum; bigger files are refused before
  they are read into memory.
- The **daemon** reads the path, so it must be a path the service can see.
  `PrivateTmp` is switched off in the unit for exactly this reason, so `/tmp`
  works; if you re-harden the service, expect `/tmp` paths to be invisible.
- The path is expanded (`~` works) and the basename is kept as the filename.
- Without `--to`, a file goes to the default chat.
- Sent files show up in the session as `[file] name (size)`; incoming files
  show up as `[file] name (size)` too.

## How the two halves talk

The daemon owns the only Telegram connection (two `getUpdates` pollers on the
same bot would collide). The UI speaks JSON-lines over `TG_HOST:TG_PORT`:

```
-> {"cmd":"hello"}
<- {"type":"ready","bot":"my_bot","port":8765,"allowed":["123456789"],
    "default_chat":"123456789","pendings":[],"sessions":[...]}
<- {"type":"history_start","chat_id":"123456789"}
<- {"type":"msg","dir":"in","chat_id":"123456789","ts":...,"text":"hey there"}
<- {"type":"history_end","chat_id":"123456789"}
-> {"cmd":"sessions"}
<- {"type":"sessions","sessions":[{"chat_id":"123456789","name":"Alice",...}]}
-> {"cmd":"allow","chat_id":"123456789"}
<- {"type":"allowed","allowed":["123456789"],"default_chat":"123456789"}
-> {"cmd":"send","chat_id":"123456789","text":"hello"}
<- {"type":"msg","dir":"out","chat_id":"123456789","ts":...,"text":"hello"}
<- {"type":"ack","ok":true}
-> {"cmd":"send_file","chat_id":"123456789","path":"/tmp/notes.txt","caption":"hi"}
<- {"type":"ack","ok":true,"file":"notes.txt"}
```

So you can also script it:

```bash
printf '%s\n' '{"cmd":"send","chat_id":"123456789","text":"from a script"}' | nc 127.0.0.1 8765
```

Commands: `hello`, `ping`, `sessions`, `requests`, `history`, `send`,
`send_file`, `allow`, `deny`, `reload`. Events: `ready`, `msg`, `request`,
`sessions`, `requests`, `allowed`, `ack`, `history_start` / `history_end`.

## Files

```
/root/tgmessage/
    .env                 token, allowed chats, default chat, host, port, log path
    tgmessage            shell wrapper (symlinked to /usr/local/bin)
    tgmessage.py         everything else (daemon, UI, /status + /logs chat commands)
    tgmessage.service.in systemd unit template
    install.sh           installer / uninstaller (installs the watcher too)
    README.md            this file
    files/               files/photos received from Telegram
    watcher/             the self-healing caretaker (part of this app)
        watcher.py         the daemon (repairs network/clock/DNS/services/disk)
        watcher.sh         shell front-end (also /usr/local/bin/watcher)
        install.sh         (re)install the watcher systemd units + enable services
        prep.sh            pre-download NIC firmware for an older PC
        rescue-bios-boot.sh  make the disk boot on a BIOS-only PC
        systemd/           watcher units that get installed
        state/             watcher runtime state, backups, Telegram outbox
        logs/watcher.log   the watcher log (also shown by the /logs chat command)
    state/
        offset.json      Telegram update offset - survives reboots
        <chatid>.jsonl   one history file per allowed chat
        pending.json     chats waiting to be allowed
        chats.json       chat metadata (names, usernames, counts, last seen)
        daemon.log       daemon's own log (also: journalctl -u tgmessage)
        daemon.pid       only when running without systemd
        daemon.console.log  only when running without systemd
```

`.env`:

```
TG_BOT_TOKEN=123456789:AA...
TG_ALLOWED_CHAT_IDS=123456789,987654321   # managed by --allow/--deny
TG_DEFAULT_CHAT=123456789                 # one-shot sends + startup session
TG_HOST=127.0.0.1
TG_PORT=8765
TG_HISTORY=300
TG_POLL_TIMEOUT=30
# what /logs shows (default: <this folder>/watcher/logs/watcher.log)
# TG_WATCHER_LOG=/root/tgmessage/watcher/logs/watcher.log
```

`/etc/default/watcher` (the watcher's own config - separate on purpose, so the
watcher can repair Telegram/DNS problems before the bot can talk):

```
WATCH_INTERVAL=30
NOTIFY=1                       # 1 = Telegram alerts, 0 = log only
TG_HOME=/root/tgmessage        # where the bot token + chat id live
STATIC_IP=192.0.2.100/24       # fallback address if DHCP fails (example - set for YOUR LAN)
STATIC_GW=192.0.2.1            # fallback gateway (example - set for YOUR LAN)
DNS_SERVERS="1.1.1.1 8.8.8.8"
DISK_WARN_PCT=92
```

Upgrading from the old single-chat layout: on the first daemon start the old
`TG_CHAT_ID` is folded into `TG_ALLOWED_CHAT_IDS` / `TG_DEFAULT_CHAT`, and
`state/history.jsonl` is copied into `state/<chatid>.jsonl` (the original is
kept as `state/history.jsonl.bak`).

Upgrading from an older standalone watcher layout (the watcher used to live in
`/root/watcher`): the watcher now lives in `/root/tgmessage/watcher/`; its log
is `watcher/logs/watcher.log` and the same data is reachable from Telegram with
`/logs`. The units (`watcher.service`, `watcher-boot.service`) and the
`watcher` command are unchanged.

## Safety

- The socket binds to `127.0.0.1` only - not reachable from the network.
- Only allowed private chats are relayed. Unknown chats are held as requests
  and never answered; groups and channels are ignored and logged.
- `.env` holds a secret and is chmod `600`.
- `tgmessage --FILE` asks the daemon to read the file you name and upload it, so
  treat socket access as file-read access for the `root` user: anything that can
  reach `127.0.0.1:8765` can ask the daemon to send a file it can read, and can
  `allow` a chat. Keep the socket local. (Same for `/logs`: it can read the
  watcher log.)
- The watcher runs as root because it repairs the machine (NIC, DNS, clock,
  sshd). It only acts on the things described above.

## Troubleshooting

```bash
tgmessage status                 # start here for the bot bridge
tgmessage requests               # who is waiting to be allowed
journalctl -u tgmessage -n 50    # or: tail -f /root/tgmessage/state/daemon.log

watcher check                    # machine-level health report
watcher log                      # follow the watcher log
journalctl -u watcher -n 50      # or: tail -f /root/tgmessage/watcher/logs/watcher.log
```

- **`token rejected`** - re-copy the token from BotFather (`/mybots` → your bot
  → API Token) and run `tgmessage --token '<token>'`. Careful: any token pasted
  into a chat should be revoked with `/revoke`.
- **`daemon not reachable`** - `tgmessage up`, or `sudo systemctl start tgmessage`.
- **nothing shows up** - the chat is probably not allowed yet; `tgmessage
  requests`, then `tgmessage allow <chatid>`.
- **`chat <id> is not allowed`** - allow it first: `tgmessage allow <id>`.
- **`another process is polling this bot`** (409) - two daemons are running;
  `tgmessage down` then `tgmessage up`.
- **`/logs` says "no watcher log found"** - the watcher has not written its
  log yet, or `TG_WATCHER_LOG` points somewhere wrong. Check
  `systemctl status watcher`; the fallback shows
  `journalctl -u watcher.service` output instead.
- **anything machine-level (no network, wrong clock, service down)** - the
  watcher is probably already on it; `watcher check` says what it sees and
  Telegram gets alerts when it repairs something.

## Uninstall

```bash
cd /root/tgmessage
sudo ./install.sh --uninstall                # removes both services + bin links
sudo ./install.sh --uninstall --keep-watcher # removes only the Telegram bridge
```

Or by hand:

```
systemctl disable --now tgmessage.service
systemctl disable --now watcher.service watcher-boot.service
rm /etc/systemd/system/tgmessage.service \
   /etc/systemd/system/watcher.service /etc/systemd/system/watcher-boot.service
systemctl daemon-reload
rm -f /usr/local/bin/tgmessage /usr/local/bin/watcher
# your data stays until you delete it too:
rm -rf /root/tgmessage
```
