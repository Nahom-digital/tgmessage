#!/usr/bin/env python3
"""
watcher.py - self-healing babysitter for the portable "server" disk.

This service exists because this Debian disk is moved between PCs (right now
its NIC is called enp0s31f6; in the old PC it will be something else, the
clock may be wrong, the keyboard does not work, ...).  On every boot and on
every cycle the watcher makes sure that:

  * the network comes up - on hardware change the NIC is renamed, so the
    config in /etc/network/interfaces is rewritten for the real interface,
    DHCP first, static fallback (STATIC_IP / STATIC_GW from
    /etc/default/watcher),
  * DNS works (rewrites /etc/resolv.conf if the resolver is broken),
  * the clock is sane (dead CMOS battery in an old PC -> restarts NTP and
    falls back to an HTTPS Date: header, because TLS and Telegram need
    correct time),
  * the systemd services YOU added with `tgmessage service add <name>` are
    enabled and running (restart + alert if not).  There is no hardcoded
    service list: nothing is watched until you add it.  A unit marked with
    `tgmessage service <name> stop` is report-only - the watcher alerts when
    it is down but never enables or restarts it.
  * the root filesystem is read-write (remounts it if the kernel flipped it
    to read-only after an I/O error),
  * the disk is not full (vacuums the systemd journal above DISK_WARN_PCT),
  * other failed systemd units are reported.

Notifications go straight to the Telegram Bot API using the token that is
already configured in /root/tgmessage/.env.  They are only sent to the chat
ids listed in TG_MANG_CHAT_IDS (managed with `tgmessage allow mang <id>`);
with no mang id configured the watcher logs but never sends, and while the
network is down messages are queued in state/outbox.jsonl for later.

Configuration: /etc/default/watcher      (see watcher/install.sh)
Logs:          /root/tgmessage/watcher/logs/watcher.log   (journalctl -u watcher)
State:         /root/tgmessage/watcher/state/
               (also reachable from Telegram with the /logs chat command)

CLI (also reachable as `watcher <cmd>`):
  watcher.py --daemon      run the watchdog loop  (systemd service)
  watcher.py --boot        quick repair pass, meant to run early at boot
  watcher.py --once        full check WITH repairs, then exit (cron/manual)
  watcher.py --check       report status only, change nothing
  watcher.py --notify TXT  send one Telegram message
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, "state")
LOG_DIR = os.path.join(BASE, "logs")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
OUTBOX_FILE = os.path.join(STATE_DIR, "outbox.jsonl")
INTERFACES = "/etc/network/interfaces"
INTERFACES_D = "/etc/network/interfaces.d"
RESOLV_CONF = "/etc/resolv.conf"
CONF_FILE = "/etc/default/watcher"
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"

# default config (overridable in /etc/default/watcher)
DEFAULTS = {
    "WATCH_INTERVAL": "30",
    "NOTIFY": "1",
    "TG_HOME": "/root/tgmessage",
    "STATIC_IP": "192.0.2.100/24",
    "STATIC_GW": "192.0.2.1",
    "DNS_SERVERS": "1.1.1.1 8.8.8.8",
    "DISK_WARN_PCT": "92",
}

# tgmessage manages the watched services with `tgmessage service add`.
UNIT_SUFFIXES = (
    ".service", ".timer", ".path", ".socket", ".target", ".mount",
    ".automount", ".swap", ".slice", ".scope", ".device",
)
NETWORK_UNIT = "networking"

log = logging.getLogger("watcher")


def setup_logging() -> None:
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    have_file = any(isinstance(h, RotatingFileHandler) for h in log.handlers)
    if not have_file:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(LOG_DIR, "watcher.log"),
                                 maxBytes=2_000_000, backupCount=5)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    have_stream = any(type(h) is logging.StreamHandler for h in log.handlers)
    if not have_stream:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def run(cmd, timeout=20):
    """Run a command and never raise.  Returns CompletedProcess(rc, out, err)."""
    if isinstance(cmd, str):
        cmd = ["/bin/sh", "-c", cmd]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        return subprocess.CompletedProcess(cmd, 124, out, "timeout")
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def sc(*args, timeout=30):
    return run(["systemctl", *args], timeout=timeout)


def unit_enabled(unit) -> str:
    return sc("is-enabled", unit, timeout=10).stdout.strip() or "unknown"


def unit_active(unit) -> str:
    return sc("is-active", unit, timeout=10).stdout.strip() or "unknown"


def load_conf() -> dict:
    conf = dict(DEFAULTS)
    try:
        with open(CONF_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return conf


CONF = load_conf()


def conf_int(key: str, default: int) -> int:
    try:
        return int(CONF.get(key, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# state                                                                       #
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


STATE = load_state()


def save_state() -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(STATE, f, indent=1, sort_keys=True)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning("cannot save state: %s", e)


def boot_id() -> str:
    try:
        with open(BOOT_ID_FILE) as f:
            return f.read().strip()
    except OSError:
        return "unknown"


# --------------------------------------------------------------------------- #
# tgmessage .env (managed by the tgmessage CLI)                               #
# --------------------------------------------------------------------------- #

def tg_env_path() -> str:
    return os.path.join(CONF.get("TG_HOME", "/root/tgmessage"), ".env")


def read_tg_env() -> dict:
    data = {}
    try:
        with open(tg_env_path(), "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


def split_ids(raw) -> list:
    out = []
    for chunk in str(raw or "").replace(",", " ").split():
        if chunk and chunk not in out:
            out.append(chunk)
    return out


def normalize_unit(name) -> str:
    name = str(name or "").strip()
    if not name:
        return ""
    if not name.endswith(UNIT_SUFFIXES):
        name += ".service"
    if not re.fullmatch(r"[A-Za-z0-9@_.:\-]+", name):
        return ""
    return name


def watched_services() -> list:
    """The systemd units the user added with `tgmessage service add`."""
    out = []
    for chunk in split_ids(read_tg_env().get("TG_WATCH_SERVICES")):
        unit = normalize_unit(chunk)
        if unit and unit not in out:
            out.append(unit)
    return out


def no_restart_services() -> list:
    """Watched units the user set to report-only (`tgmessage service <unit> stop`)."""
    out = []
    for chunk in split_ids(read_tg_env().get("TG_SERVICE_NO_RESTART")):
        unit = normalize_unit(chunk)
        if unit and unit not in out:
            out.append(unit)
    return out


def enable_units() -> list:
    """What the early boot pass should enable: the watcher itself + added services.

    Report-only units are left alone: if they are disabled, we only tell the user.
    """
    units = ["watcher.service", "watcher-boot.service"]
    report_only = set(no_restart_services())
    for unit in watched_services():
        if unit not in units and unit not in report_only:
            units.append(unit)
    return units


# --------------------------------------------------------------------------- #
# Telegram (direct Bot API - independent of the tgmessage daemon)             #
# --------------------------------------------------------------------------- #

def tg_creds():
    """Bot token + the mang chat ids that receive watcher messages."""
    data = read_tg_env()
    return (data.get("TG_BOT_TOKEN") or "").strip(), split_ids(data.get("TG_MANG_CHAT_IDS"))


def tg_send(text: str) -> bool:
    token, chats = tg_creds()
    if not token or not chats:
        log.warning("telegram not sent (need TG_MANG_CHAT_IDS; use: tgmessage allow mang <chatid>)")
        return False
    ok_all = True
    for chat in chats:
        payload = json.dumps({"chat_id": chat, "text": text,
                              "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token,
            data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                ok = 200 <= resp.status < 300
            if ok:
                log.info("telegram message sent to %s (%d chars)", chat, len(text))
            else:
                ok_all = False
        except Exception as e:  # noqa: BLE001 - the watcher must never die because of this
            log.warning("telegram send to %s failed: %s", chat, e)
            ok_all = False
    return ok_all


def outbox_add(text: str) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(OUTBOX_FILE, "a") as f:
            f.write(json.dumps({"ts": time.time(), "text": text}) + "\n")
        log.info("message queued in outbox (%d chars)", len(text))
    except OSError as e:
        log.warning("cannot queue message: %s", e)


def notify(text: str, key=None, dedupe: int = 1800, force: bool = False) -> bool:
    """Send a Telegram notification, de-duplicated by `key` unless force=True.

    Only the chats in TG_MANG_CHAT_IDS receive watcher messages. With no such
    chat configured nothing is sent and nothing is queued (log only).
    """
    if conf_int("NOTIFY", 1) == 0:
        log.info("notify (suppressed): %s", text.replace("\n", " | "))
        return False
    token, chats = tg_creds()
    if not token or not chats:
        log.info("notify (no mang chat configured, logged only): %s",
                 text.replace("\n", " | "))
        return False
    now = time.time()
    alerts = STATE.setdefault("alerts", {})
    if key and not force:
        if now - alerts.get(key, 0) < dedupe:
            log.debug("notification %s de-duplicated", key)
            return False
    if tg_send(text):
        if key:
            alerts[key] = now
            save_state()
        return True
    outbox_add(text)
    return False


def flush_outbox() -> None:
    """Deliver queued messages once the network is back."""
    if not os.path.exists(OUTBOX_FILE):
        return
    try:
        with open(OUTBOX_FILE, "r") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
    except OSError:
        return
    if not lines:
        return
    keep, sent = [], 0
    for line in lines:
        try:
            text = json.loads(line).get("text", "")
        except ValueError:
            continue
        if text and tg_send(text):
            sent += 1
        else:
            keep.append(line)
    try:
        with open(OUTBOX_FILE, "w") as f:
            f.write("\n".join(keep) + ("\n" if keep else ""))
    except OSError:
        pass
    if sent:
        log.info("outbox: %d queued message(s) delivered", sent)

# --------------------------------------------------------------------------- #
# network - the big one, because the NIC name changes with the PC             #
# --------------------------------------------------------------------------- #

VIRT_IFACE_RE = re.compile(r"^(lo|veth|br-|virbr|docker|tun|tap|wg|tailscale|zt|vmnet|bond|dummy)")


def eth_candidates():
    """Physical ethernet interfaces, best candidate first (cable plugged first)."""
    found = []
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []
    for name in names:
        if VIRT_IFACE_RE.match(name):
            continue
        base = os.path.join("/sys/class/net", name)
        try:
            with open(os.path.join(base, "type")) as f:
                typ = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if typ != 1:                                   # 1 = ARPHRD_ETHER
            continue
        if os.path.isdir(os.path.join(base, "wireless")):
            continue
        carrier = "?"
        try:
            with open(os.path.join(base, "carrier")) as f:
                carrier = f.read().strip()
        except OSError:
            pass
        has_device = os.path.exists(os.path.join(base, "device"))
        found.append({"name": name, "carrier": carrier, "device": has_device})
    found.sort(key=lambda d: (d["carrier"] != "1", not d["device"], d["name"]))
    return [d["name"] for d in found]


def link_up(name: str) -> None:
    run(["ip", "link", "set", "dev", name, "up"], timeout=10)


def iface_ips(name: str):
    r = run(["ip", "-4", "-o", "addr", "show", "dev", name, "scope", "global"], timeout=10)
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", r.stdout)


def iface_ip(name: str):
    ips = iface_ips(name)
    return ips[0] if ips else None


def default_route():
    r = run(["ip", "-4", "route", "show", "default"], timeout=10)
    m = re.search(r"default via (\S+) dev (\S+)", r.stdout)
    return (m.group(1), m.group(2)) if m else None


def ping(host: str, iface=None, timeout: int = 3) -> bool:
    cmd = ["ping", "-c", "1", "-W", str(timeout)]
    if iface:
        cmd += ["-I", iface]
    cmd.append(host)
    return run(cmd, timeout=timeout + 4).returncode == 0


def tcp_ok(host: str, port: int = 443, timeout: int = 3) -> bool:
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return True
    except OSError:
        return False


def dns_lookup(name: str = "cloudflare.com") -> bool:
    r = run(["getent", "-s", "dns", "hosts", name], timeout=6)
    return r.returncode == 0 and bool(r.stdout.strip())


def internet_ok() -> bool:
    return tcp_ok("1.1.1.1") or tcp_ok("8.8.8.8") or tcp_ok("9.9.9.9")


def net_status() -> dict:
    st = {"iface": None, "ip": None, "gw": None, "dev": None,
          "gw_ping": False, "internet": False, "dns": False, "ok": False}
    route = default_route()
    if route:
        st["gw"], st["dev"] = route
    iface = st["dev"]
    if iface is None:
        for name in eth_candidates():
            if iface_ip(name):
                iface = name
                break
    if iface is None:
        cands = eth_candidates()
        iface = cands[0] if cands else None
    st["iface"] = iface
    if iface:
        st["ip"] = iface_ip(iface)
    if st["gw"]:
        st["gw_ping"] = ping(st["gw"], timeout=3)
    st["internet"] = internet_ok()
    if st["internet"]:
        st["dns"] = dns_lookup()
    st["ok"] = bool(st["ip"] and route and st["internet"] and st["dns"])
    return st


def cfg_ifaces():
    """Interface names mentioned in /etc/network/interfaces(.d)/*."""
    names = set()
    paths = [INTERFACES]
    try:
        paths += [os.path.join(INTERFACES_D, f) for f in sorted(os.listdir(INTERFACES_D))]
    except OSError:
        pass
    for path in paths:
        try:
            with open(path, "r") as f:
                for line in f:
                    m = re.match(r"\s*(?:auto|allow-hotplug)\s+(.+)", line)
                    if m:
                        names.update(m.group(1).split())
                        continue
                    m = re.match(r"\s*iface\s+(\S+)", line)
                    if m:
                        names.add(m.group(1))
        except OSError:
            continue
    return names


def fix_dns() -> bool:
    """Write public resolvers into /etc/resolv.conf (backup kept)."""
    servers = CONF.get("DNS_SERVERS", DEFAULTS["DNS_SERVERS"]).split()
    if os.path.islink(RESOLV_CONF):
        target = os.readlink(RESOLV_CONF)
        if "systemd/resolve" in target:
            if unit_active("systemd-resolved") == "active":
                run(["resolvectl", "flush-caches"], timeout=10)
                return dns_lookup()
            os.unlink(RESOLV_CONF)
    try:
        bak = os.path.join(STATE_DIR, "resolv.conf.bak")
        if not os.path.exists(bak):
            os.makedirs(STATE_DIR, exist_ok=True)
            shutil.copy2(RESOLV_CONF, bak)
    except OSError:
        pass
    try:
        with open(RESOLV_CONF, "w") as f:
            f.write("# written by watcher - previous file kept in %s/resolv.conf.bak\n" % STATE_DIR)
            for s in servers:
                f.write("nameserver %s\n" % s)
    except OSError as e:
        log.warning("cannot write %s: %s", RESOLV_CONF, e)
        return False
    ok = dns_lookup()
    log.info("resolv.conf rewritten (nameservers: %s), dns working: %s", " ".join(servers), ok)
    return ok


def dhcp_gateway(name: str):
    gw = None
    try:
        with open("/var/lib/dhcp/dhclient.%s.leases" % name) as f:
            for line in f:
                m = re.search(r"option routers (\S+);", line)
                if m:
                    gw = m.group(1)
    except OSError:
        pass
    return gw


def dhcp_attempt(name: str, timeout: int = 30):
    log.info("trying DHCP on %s", name)
    run(["dhclient", "-4", "-r", name], timeout=10)      # drop broken lease state, best effort
    time.sleep(0.5)
    r = run(["dhclient", "-4", "-1", "-v", name], timeout=timeout + 10)
    ifr = iface_ip(name)
    if r.returncode != 0 and not ifr:
        log.info("dhclient on %s exited rc=%s: %s", name, r.returncode,
                 (r.stderr or r.stdout).strip()[-200:])
    if ifr and not default_route():
        gw = dhcp_gateway(name)
        if gw:
            run(["ip", "route", "replace", "default", "via", gw, "dev", name], timeout=10)
    return ifr


def static_attempt(name: str) -> bool:
    addr = CONF.get("STATIC_IP", DEFAULTS["STATIC_IP"])
    gw = CONF.get("STATIC_GW", DEFAULTS["STATIC_GW"])
    log.info("trying static %s via %s on %s", addr, gw, name)
    run(["ip", "addr", "replace", addr, "dev", name], timeout=10)
    run(["ip", "route", "replace", "default", "via", gw, "dev", name], timeout=10)
    time.sleep(1)
    if ping(gw, iface=name, timeout=3) or internet_ok():
        return True
    run(["ip", "addr", "del", addr, "dev", name], timeout=10)
    run(["ip", "route", "del", "default", "via", gw, "dev", name], timeout=10)
    return False


def cleanup_iface(name: str) -> None:
    """Remove half-configured addresses/routes from a failed attempt."""
    run(["ip", "-4", "addr", "flush", "dev", name, "scope", "global"], timeout=10)
    r = run(["ip", "-4", "route", "show", "default", "dev", name], timeout=10)
    for line in r.stdout.splitlines():
        parts = line.split()
        if "via" in parts:
            gw = parts[parts.index("via") + 1]
            run(["ip", "route", "del", "default", "via", gw, "dev", name], timeout=10)


def persist_interfaces(name: str, method: str) -> None:
    """Write a /etc/network/interfaces that boots correctly on THIS hardware."""
    if method in (None, "configured"):
        return
    if method == "dhcp":
        body = "auto %s\niface %s inet dhcp\n" % (name, name)
    else:
        addr = CONF.get("STATIC_IP", DEFAULTS["STATIC_IP"]).split("/")[0]
        gw = CONF.get("STATIC_GW", DEFAULTS["STATIC_GW"])
        dns = CONF.get("DNS_SERVERS", DEFAULTS["DNS_SERVERS"])
        body = ("auto %s\niface %s inet static\n"
                "    address %s\n    netmask 255.255.255.0\n"
                "    gateway %s\n    dns-nameserver %s\n" % (name, name, addr, gw, dns))
    content = (
        "# This file is managed automatically by %s/watcher.py (part of tgmessage).\n"
        "# It was rewritten because the network hardware changed (NIC: %s).\n"
        "# A backup of the old file is in %s/state/.\n\n"
        "source /etc/network/interfaces.d/*\n\n"
        "auto lo\niface lo inet loopback\n\n" % (BASE, name, BASE) + body)
    try:
        with open(INTERFACES, "r") as f:
            current = f.read()
    except OSError:
        current = None
    if current == content:
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        bak = os.path.join(STATE_DIR, "interfaces.bak")
        if current is not None and not os.path.exists(bak):
            with open(bak, "w") as f:
                f.write(current)
        tmp = INTERFACES + ".watcher-tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, INTERFACES)
        log.warning("rewrote %s: interface %s via %s", INTERFACES, name, method)
    except OSError as e:
        log.warning("cannot rewrite %s: %s", INTERFACES, e)


def repair_network():
    """One repair pass. Returns (status_dict, [change_notes])."""
    changes = []
    st = net_status()

    if st["ok"]:
        return st, changes

    # internet reachable but only DNS broken -> cheap fix
    if st["internet"] and not st["dns"]:
        if fix_dns():
            changes.append("dns fixed")
        return net_status(), changes

    # LAN fine but uplink down -> nothing a watcher can do, do not churn
    if st["ip"] and st["gw"] and st["gw_ping"] and not st["internet"]:
        log.warning("network: LAN %s is up but the internet is unreachable (router/uplink problem?)",
                    st["ip"])
        return st, changes

    cands = eth_candidates()
    if not cands:
        log.error("network: no physical ethernet interface found at all")
        return st, changes

    configured = cfg_ifaces()
    for name in cands:
        log.info("network: attempting repairs on %s", name)
        link_up(name)
        time.sleep(2)
        method = None
        success = False

        # 1. normal ifupdown path when the interface is configured
        if name in configured:
            r = run(["ifup", name], timeout=25)
            if r.returncode not in (0,):
                log.info("ifup %s rc=%s: %s", name, r.returncode, r.stderr.strip()[:200])
            st2 = net_status()
            if st2["ip"] and st2["internet"]:
                method = "configured"
                success = True
            else:
                cleanup_iface(name)

        # 2. DHCP (portable, works on any LAN)
        if not success:
            dhcp_attempt(name)
            st2 = net_status()
            if st2["ip"] and st2["internet"]:
                method = "dhcp"
                success = True

        # 3. last resort: the configured static address (STATIC_IP/STATIC_GW)
        if not success:
            if static_attempt(name):
                st2 = net_status()
                if st2["internet"]:
                    method = "static"
                    success = True
            if not success:
                cleanup_iface(name)

        if not success:
            log.warning("network: %s did not get working internet", name)
            continue

        st2 = net_status()
        if not st2["dns"]:
            if fix_dns():
                changes.append("dns fixed")
            st2 = net_status()
        persist_interfaces(name, method)
        changes.append("network up on %s via %s (%s)" % (name, method, st2["ip"]))
        log.warning("network repaired: %s via %s on %s (%s)",
                    st2["ip"], method, name, "internet OK" if st2["internet"] else "no internet")
        return st2, changes

    log.error("network: no ethernet interface reached the internet")
    return net_status(), changes

# --------------------------------------------------------------------------- #
# systemd services                                                            #
# --------------------------------------------------------------------------- #

def restart_rate_ok(unit: str, min_gap: int = 300) -> bool:
    now = time.time()
    lr = STATE.setdefault("last_restart", {})
    if now - lr.get(unit, 0) >= min_gap:
        lr[unit] = now
        save_state()
        return True
    return False


def check_services(repair: bool = True):
    problems = []
    report_only = set(no_restart_services())
    for unit in watched_services():
        label = unit[: -len(".service")] if unit.endswith(".service") else unit
        ro = unit in report_only
        try:
            enabled = unit_enabled(unit)
            if enabled == "not-found":
                problems.append("%s is not installed" % unit)
                if repair:
                    notify("watcher: %s is in TG_WATCH_SERVICES but is not installed" % unit,
                           key="missing:%s" % unit, dedupe=86400)
                continue
            if enabled in ("disabled", "masked", "bad"):
                if ro:
                    problems.append("%s is %s (notify-only)" % (label, enabled))
                    if repair:
                        notify("watcher: %s is %s - notify-only, I am not touching it" % (unit, enabled),
                               key="reportonly-enabled:%s" % unit, dedupe=1800)
                    continue
                if not repair:
                    problems.append("%s is %s" % (label, enabled))
                    continue
                if enabled == "masked":
                    sc("unmask", unit)
                sc("enable", unit)
                log.warning("%s was %s - enabled it", unit, enabled)
                notify("watcher: enabled %s (it was %s)" % (unit, enabled),
                       key="enable:%s" % unit, dedupe=3600)

            active = unit_active(unit)
            if active not in ("active", "activating"):
                if ro:
                    problems.append("%s (%s) is %s (notify-only)" % (label, unit, active))
                    if repair:
                        notify("watcher: %s (%s) is %s - notify-only, restart it yourself if you want"
                               % (label, unit, active),
                               key="reportonly-down:%s" % unit, dedupe=1800)
                    continue
                if not repair:
                    problems.append("%s (%s) is %s" % (label, unit, active))
                    continue
                if unit == NETWORK_UNIT:
                    if active == "failed":
                        sc("reset-failed", unit)
                        log.info("reset failed state of %s", unit)
                    continue
                if restart_rate_ok(unit):
                    sc("reset-failed", unit)
                    sc("start", unit, timeout=60)
                    time.sleep(2)
                    active = unit_active(unit)
                    if active == "active":
                        log.warning("%s was down - started it", unit)
                        notify("watcher: %s (%s) was down, restarted it" % (label, unit),
                               key="restart:%s" % unit, dedupe=1800)
                    else:
                        problems.append("%s (%s) still %s after restart" % (label, unit, active))
                        notify("watcher: %s (%s) is %s and did not come back" % (label, unit, active),
                               key="down:%s" % unit, dedupe=1800)
        except Exception:
            log.exception("service check crashed for %s", unit)
            problems.append("check crashed for %s" % unit)
    return problems


# --------------------------------------------------------------------------- #
# clock / disk / filesystem                                                   #
# --------------------------------------------------------------------------- #

def clock_sane() -> bool:
    now = time.time()
    return 1735689600 <= now <= 2051222400      # 2025-01-01 .. 2035-01-01


def http_date_now():
    import ssl as _ssl
    ctx = _ssl._create_unverified_context()
    req = urllib.request.Request("https://cloudflare.com", headers={"User-Agent": "watcher"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return resp.headers.get("Date")
    except Exception as e:  # noqa: BLE001
        log.warning("cannot fetch time over HTTPS: %s", e)
        return None


def fix_clock(repair: bool = True) -> bool:
    if clock_sane():
        return True
    if not repair:
        return False
    log.warning("system clock looks wrong: %s", time.strftime("%Y-%m-%d %H:%M:%S"))
    run(["timedatectl", "set-ntp", "true"], timeout=15)
    sc("restart", "systemd-timesyncd", timeout=30)
    for _ in range(6):
        time.sleep(2)
        if clock_sane():
            log.info("clock fixed by NTP")
            return True
    if internet_ok():
        date_hdr = http_date_now()
        if date_hdr:
            try:
                ts = int(parsedate_to_datetime(date_hdr).timestamp())
                run(["date", "-u", "-s", "@%d" % ts], timeout=10)
                log.warning("clock was very wrong - forced it from HTTPS Date header")
                notify("watcher: the clock was badly wrong (dead CMOS battery in the old PC?). "
                       "I set it from the internet. TLS services may have failed before this.",
                       key="clock", dedupe=3600)
            except Exception as e:  # noqa: BLE001
                log.warning("cannot parse Date header: %s", e)
    return clock_sane()


def root_rw(repair: bool = True) -> bool:
    opts = run(["findmnt", "-no", "OPTIONS", "/"], timeout=10).stdout.strip()
    if "rw" in opts.split(","):
        return True
    log.error("root filesystem is mounted READ-ONLY (%s)", opts)
    if repair:
        run(["mount", "-o", "remount,rw", "/"], timeout=20)
        ok = "rw" in run(["findmnt", "-no", "OPTIONS", "/"], timeout=10).stdout.split(",")
        if ok:
            log.warning("remounted / read-write")
            notify("watcher: root filesystem was read-only - remounted read-write. "
                   "The kernel may have hit an I/O error; please check the disk soon.",
                   key="ro", dedupe=86400)
            return True
        notify("watcher: root filesystem is READ-ONLY and I could not remount it!",
               key="ro-fail", dedupe=1800)
    return False


def disk_check(repair: bool = True):
    r = run(["df", "-P", "/"], timeout=10)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    pct = None
    if len(lines) >= 2:
        m = re.search(r"(\d+)%", lines[-1])
        if m:
            pct = int(m.group(1))
    if pct is not None:
        STATE["disk_pct"] = pct
    warn = conf_int("DISK_WARN_PCT", 92)
    if pct is not None and pct >= warn:
        log.error("root filesystem is %d%% full", pct)
        if repair and os.path.isdir("/var/log/journal"):
            run(["journalctl", "--vacuum-size=200M"], timeout=60)
            log.info("vacuumed systemd journal to free space")
            notify("watcher: root disk was %d%% full - vacuumed the journal. Clean up something!" % pct,
                   key="disk", dedupe=3600)
    return pct


def other_failed_units():
    known = set(watched_services()) | {NETWORK_UNIT, "watcher.service", "watcher-boot.service"}
    r = sc("list-units", "--failed", "--plain", "--no-legend", timeout=20)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] not in known:
            out.append(parts[0])
    return out


def report_failed_units() -> None:
    for unit in other_failed_units():
        log.warning("unrelated failed unit: %s", unit)
        notify("watcher: unrelated systemd unit failed: %s" % unit,
               key="failed:%s" % unit, dedupe=3600)


# --------------------------------------------------------------------------- #
# reports                                                                     #
# --------------------------------------------------------------------------- #

def compose_report(title: str, net: dict, problems) -> str:
    lines = ["watcher: %s" % title, "host: %s" % socket.gethostname()]
    if net["internet"]:
        lines.append("network: OK - %s %s (gw %s)" % (net["iface"], net["ip"], net["gw"]))
    elif net["ip"]:
        lines.append("network: LAN only - %s %s (gw %s, no internet)" % (net["iface"], net["ip"], net["gw"]))
    else:
        lines.append("network: DOWN (no working interface)")
    if net["internet"] and not net["dns"]:
        lines.append("dns: BROKEN")
    units = watched_services()
    if not units:
        lines.append("services: none configured (tgmessage service add <name>)")
    else:
        for unit in units:
            act = unit_active(unit)
            mark = "OK" if act == "active" else act.upper()
            lines.append("%s: %s" % (unit, mark))
    if not clock_sane():
        lines.append("clock: WRONG (%s)" % time.strftime("%Y-%m-%d %H:%M:%S"))
    if STATE.get("disk_pct") is not None:
        lines.append("disk: %d%% used" % STATE["disk_pct"])
    if problems:
        lines.append("problems:")
        lines += ["  - %s" % p for p in problems]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# modes                                                                       #
# --------------------------------------------------------------------------- #

def daemon() -> None:
    setup_logging()
    interval = conf_int("WATCH_INTERVAL", 30)
    log.info("watcher daemon starting (interval %ss, pid %d)", interval, os.getpid())
    bid = boot_id()
    reported = STATE.get("boot_reported") == bid
    cycle = 0
    while True:
        cycle += 1
        net = {"internet": False, "dns": False, "ip": None, "iface": None, "gw": None}
        problems = []
        try:
            net, changes = repair_network()
            if changes:
                notify("watcher: repaired the network:\n" +
                       "\n".join("- " + c for c in changes),
                       key="netfix", dedupe=600)
            fix_clock()
            problems += check_services()
            if cycle % 10 == 1:
                disk_check()
            root_rw()
            report_failed_units()
            if net["internet"]:
                flush_outbox()
            if not reported and net["internet"]:
                notify(compose_report("booted up", net, problems),
                       key="boot:%s" % bid, force=True)
                STATE["boot_reported"] = bid
                reported = True
        except Exception:
            log.exception("watcher cycle crashed")
        save_state()
        log.info("cycle %d done: net=%s ip=%s problems=%d",
                 cycle, "OK" if net.get("internet") else "no-internet", net.get("ip"), len(problems))
        sys.stdout.flush()
        time.sleep(15 if (problems or not net.get("internet")) else interval)


def cmd_boot() -> int:
    setup_logging()
    log.info("boot repair starting (hardware-change pass)")
    net, changes = repair_network()
    fix_clock()
    for unit in enable_units():
        try:
            en = unit_enabled(unit)
            if en in ("disabled", "masked"):
                if en == "masked":
                    sc("unmask", unit)
                sc("enable", unit)
                log.warning("boot: %s was %s - enabled it", unit, en)
        except Exception:
            log.exception("boot: cannot enable %s", unit)
    log.info("boot repair done: network=%s %s changes=%s",
             "OK" if net["ok"] else "not-ready", net.get("ip") or "", changes)
    sys.stdout.flush()
    return 0


def cmd_check(repair: bool) -> int:
    setup_logging()
    if repair:
        net, _ = repair_network()
        fix_clock()
    else:
        net = net_status()
    problems = check_services(repair=repair)
    rw = root_rw(repair=repair)
    disk_check(repair=repair)
    if repair:
        report_failed_units()
    print(compose_report("status report", net, problems))
    if not rw:
        print("WARNING: root filesystem is not read-write")
    overall = net["ok"] and not problems and rw and clock_sane()
    print("overall: %s" % ("OK" if overall else "ATTENTION NEEDED"))
    return 0 if overall else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="watcher - self-healing caretaker for the portable server disk")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--daemon", action="store_true", help="run the watchdog loop (systemd)")
    g.add_argument("--boot", action="store_true", help="one early repair pass (systemd boot unit)")
    g.add_argument("--once", action="store_true", help="one full check WITH repairs and alerts, then exit")
    g.add_argument("--check", action="store_true", help="report status only, change nothing")
    g.add_argument("--notify", metavar="TEXT", help="send one Telegram message and exit")
    ap.add_argument("--version", action="version", version="watcher 1.0")
    args = ap.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    if args.notify is not None:
        setup_logging()
        token, chats = tg_creds()
        if not token or not chats:
            print("not sent: no TG_MANG_CHAT_IDS configured (tgmessage allow mang <chatid>)")
            return 1
        ok = tg_send(args.notify)
        if not ok:
            outbox_add(args.notify)
            print("queued (telegram unreachable)")
        else:
            print("sent")
        return 0 if ok else 1
    if args.daemon:
        daemon()
        return 0
    if args.boot:
        return cmd_boot()
    if args.check:
        return cmd_check(repair=False)
    return cmd_check(repair=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
