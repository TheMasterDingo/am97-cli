#!/usr/bin/env python3
"""am97 -- command-line control for the Angry Miao AM Infinity .97.

Talks to the mouse over the same vendor-defined HID channel its official web
configurator uses, so no driver and no browser is needed. Useful for
scripting: killing the lighting along with the rest of your desk, switching
polling rate per game, reading battery into a status bar, and so on.

The protocol was transcribed from the configurator's own JavaScript bundle,
so the frames sent here are the frames the official tool sends.

    am97 status
    am97 light off
    am97 light color '#FF4C00'
    am97 mouse-light off
    am97 all off                     # dongle + mouse
    am97 rate 4000
    am97 dpi set 0 1600

MIT licensed. Not affiliated with or endorsed by Angry Miao. Use at your own
risk -- see the safety notes in the README.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

__version__ = "1.0.0"

try:
    import hid
except ImportError:                                          # pragma: no cover
    sys.exit("hidapi is not installed:  pip install hidapi")


# --------------------------------------------------------------- device ids

VID = 0x0E8D
PID_DONGLE = 0x0703          # 2.4G receiver
PID_MOUSE = 0x0880           # mouse, connected by cable
ALT_IDS = ((0x35A1, 0x0035),)
USAGE_PAGE = 0xFF13          # vendor collection carrying the config channel

REPORT_ID = 0x14
PAYLOAD_LEN = 61

TYPE_CMD, TYPE_REPLY, TYPE_WRITE_ACK = 0x5A, 0x5B, 0x5D
DEV_LOCAL, DEV_RELAY = 0x00, 0x80


# --------------------------------------------------------------- command ids
#
# Verified against live captures: getBattery, getDongleBattery, getProfile,
# getReportRate, setReportRate, getDongleLight, setDongleLight, readNvData,
# saveRgb, setRgbRealtime, setLightSettings.
# Transcribed from the bundle but not exercised here: everything else.

CMD = {
    "getProfile": 12489, "changeProfile": 12488,
    "getBattery": 12495, "getDongleBattery": 12303,
    "getReportRate": 12305, "setReportRate": 12304,
    "getDpi": 12485, "setDpi": 12484,
    "setDpiLoopRange": 12486, "setCurrentDpiStage": 12487,
    "getDpiButton": 12523, "setDpiButton": 12522,
    "getLod": 12493, "setLod": 12492,
    "getMotionSync": 12499, "setMotionSync": 12498,
    "getAngleSnapping": 12501, "setAngleSnapping": 12500,
    "getRippleControl": 12503, "setRippleControl": 12502,
    "getFpsMode": 12511, "setFpsMode": 12510,
    "getSwDebounce": 12521, "setSwDebounce": 12520,
    "getSleepTime": 12307, "setSleepTime": 12306,
    "getRotateAngle": 12505, "setRotateAngle": 12504,
    "getKeyRemap": 12483, "setKeyRemap": 12482,
    "getDongleLight": 12302, "setDongleLight": 12301,
    "setLightSettings": 12467, "setRgbRealtime": 12480, "saveRgb": 2573,
    "readNvData": 2572,
}

# Refused outright. These sit in the same id range as everything else and are
# indistinguishable from a read at the byte level -- sweeping ids blindly is
# how you wipe your configuration and your 2.4G pairing.
BLOCKED = {
    12494: "resetSettings (wipes all configuration)",
    12395: "clearBonded (destroys the 2.4G pairing)",
    12394: "setPairingUniaa (pairing handshake)",
    4353: "rebootPairingDevice",
}

NV_LIGHT_SETTINGS = 24586    # 0x600A: [enabled, effect, speed]
NV_LIGHT_COLORS = 25088      # 0x6200: rgb pattern
NV_READ_LEN = 1000

RGB_FRAME_INTERVAL = 16
RGB_LED_COUNT = 1
RGB_REALTIME_FLAG = 0x80

HUE_MAX = 1530
DPI_MIN, DPI_MAX, DPI_STEP = 50, 30000, 50
VALID_RATES = (125, 250, 500, 1000, 2000, 4000, 8000)

DONGLE_EFFECTS = {
    "solid": 1, "breathing": 2, "arc": 3, "mosaic": 4,
    "aurora": 5, "night": 6, "flow": 7,
}
MOUSE_EFFECTS = {0: "solid", 1: "breathing", 2: "neon"}

# Used only if the device does not answer a read.
DONGLE_LIGHT_FALLBACK = {
    "on": True, "effect": 1, "hue1": 0, "sat1": 100,
    "hue2": 0, "sat2": 100, "hue3": 0, "sat3": 100,
    "speed": 10, "brightness": 200,
}
MOUSE_LIGHT_FALLBACK = {"on": True, "effect": 0, "speed": 1}


# --------------------------------------------------------------- colour maths

def hex_to_rgb(text):
    t = text.strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6 or any(c not in "0123456789abcdefABCDEF" for c in t):
        raise ValueError(f"not a hex colour: {text}")
    return int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)


def hex_to_hue_sat(text):
    """The dongle stores hue (0-1530) and saturation (1-100), not RGB."""
    r, g, b = (v / 255 for v in hex_to_rgb(text))
    hi, lo = max(r, g, b), min(r, g, b)
    c = hi - lo
    if c == 0:
        h = 0.0
    elif hi == r:
        h = ((g - b) / c) % 6
    elif hi == g:
        h = (b - r) / c + 2
    else:
        h = (r - g) / c + 4
    deg = (h * 60) % 360
    sat = 0 if hi == 0 else round(c / hi * 100)
    return round(deg / 360 * HUE_MAX), max(1, min(100, sat))


def hue_to_hex(raw):
    t = max(0, min(HUE_MAX, int(raw)))
    if t <= 255:
        r, g, b = 255, t, 0
    elif t <= 510:
        r, g, b = 510 - t, 255, 0
    elif t <= 765:
        r, g, b = 0, 255, t - 510
    elif t <= 1020:
        r, g, b = 0, 1020 - t, 255
    elif t <= 1275:
        r, g, b = t - 1020, 0, 255
    else:
        r, g, b = 255, 0, HUE_MAX - t
    return f"#{r:02X}{g:02X}{b:02X}"


def u16le(v):
    return [v & 0xFF, (v >> 8) & 0xFF]


def clamp_dpi(v):
    return max(DPI_MIN, min(DPI_MAX, (int(v) // DPI_STEP) * DPI_STEP))


# --------------------------------------------------------------- exceptions

class Am97Error(RuntimeError):
    pass


class DeviceNotFound(Am97Error):
    pass


class DeviceBusy(Am97Error):
    pass


# --------------------------------------------------------------- the device

class Am97:
    """Vendor HID client for the AM Infinity .97."""

    def __init__(self, path, has_dongle, dry_run=False):
        self.has_dongle = has_dongle
        self.dry_run = dry_run
        # Mouse-side settings travel over the relay only via the dongle.
        self.mouse_id = DEV_RELAY if has_dongle else DEV_LOCAL
        self.dev = None
        if dry_run:
            return
        try:
            self.dev = hid.device()
            self.dev.open_path(path)
        except Exception as exc:
            self.dev = None
            raise DeviceBusy(
                f"could not open the device ({exc}). If the web configurator "
                f"is open in a browser tab, close it and try again."
            ) from exc

    # -- discovery ---------------------------------------------------------

    @staticmethod
    def discover(prefer="auto"):
        """Return (path, has_dongle) or None.

        prefer: 'auto' (dongle first), 'dongle', or 'usb'. The dongle is
        preferred because it owns the dongle lighting and can still reach the
        mouse by relay, so it can service every command on its own.
        """
        dongle = usb = None
        try:
            entries = hid.enumerate()
        except Exception:
            return None
        for d in entries:
            if d.get("usage_page") != USAGE_PAGE:
                continue
            ids = (d.get("vendor_id"), d.get("product_id"))
            if ids == (VID, PID_DONGLE):
                dongle = (d["path"], True)
            elif ids == (VID, PID_MOUSE) or ids in ALT_IDS:
                usb = (d["path"], False)
        if prefer == "dongle":
            return dongle
        if prefer == "usb":
            return usb
        return dongle or usb

    @classmethod
    def open(cls, prefer="auto", dry_run=False):
        found = cls.discover(prefer)
        if not found:
            if dry_run:
                return cls(None, True, dry_run=True)
            raise DeviceNotFound(
                "AM Infinity .97 not found. Plug in the 2.4G dongle or "
                "connect the mouse by cable.")
        return cls(*found, dry_run=dry_run)

    def close(self):
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass

    # -- wire format -------------------------------------------------------
    #
    #  [payload_len][device_id] [05][type][len_lo][len_hi][cmd_lo][cmd_hi][data]
    #
    # device_id 0x00 addresses whatever you are connected to; 0x80 addresses
    # the mouse through the dongle's relay. type 0x5a is a command, 0x5b a
    # reply, 0x5d a write acknowledgement. len counts the command id plus the
    # data. The payload is zero-padded to 61 bytes and carried in HID feature
    # report 0x14.

    @staticmethod
    def _frame(device_id, cmd_id, data=()):
        data = bytes(data)
        body = (bytes([0x05, TYPE_CMD]) + bytes(u16le(len(data) + 2))
                + bytes(u16le(cmd_id)) + data)
        return (bytes([len(body), device_id]) + body).ljust(PAYLOAD_LEN, b"\x00")

    def send(self, cmd_id, data=(), device_id=None, expect_reply=True,
             tries=40):
        if cmd_id in BLOCKED:
            raise Am97Error(f"refusing {cmd_id:#06x}: {BLOCKED[cmd_id]}")
        device_id = self.mouse_id if device_id is None else device_id
        frame = self._frame(device_id, cmd_id, data)
        if self.dry_run:
            print(f"  would send dev={device_id:#04x} cmd={cmd_id:#06x} "
                  f"{frame[:20].hex(' ')} ...")
            return None
        self.dev.send_feature_report(bytes([REPORT_ID]) + frame)
        if not expect_reply:
            return None
        # A read may carry several stacked responses, and a reply left over
        # from an earlier write-only command can arrive first, so walk the
        # frames and keep polling until the command id matches.
        for _ in range(tries):
            r = bytes(self.dev.get_feature_report(REPORT_ID, PAYLOAD_LEN + 1))
            if len(r) < 9 or r[1] == 0:
                time.sleep(0.03)
                continue
            i = 3
            while i + 6 <= len(r):
                if r[i] != 0x05 or r[i + 1] not in (TYPE_REPLY, TYPE_WRITE_ACK):
                    break
                n = r[i + 2] | (r[i + 3] << 8)
                rid = r[i + 4] | (r[i + 5] << 8)
                payload = r[i + 6:i + 4 + n]
                if rid == cmd_id:
                    return payload
                i += 4 + n
            time.sleep(0.03)
        return None

    def read_nv(self, nv_id):
        """readNvData -> [status][nv_lo][nv_hi][len_lo][len_hi][data...]"""
        p = self.send(CMD["readNvData"], u16le(nv_id) + u16le(NV_READ_LEN))
        if not p or len(p) < 6 or p[0] != 0:
            return None
        if (p[1] | (p[2] << 8)) != nv_id:
            return None
        n = p[3] | (p[4] << 8)
        body = p[5:]
        return body[:n] if n > 0 else body

    # -- reads -------------------------------------------------------------

    def battery(self):
        p = self.send(CMD["getBattery"])
        if not p or len(p) < 5 or p[0] != 0:
            return None
        return {"percent": p[2], "charging": bool(p[1]), "present": bool(p[4])}

    def hub_battery(self):
        """Spare battery in the dongle/hub, when one is seated."""
        if not self.has_dongle:
            return None
        p = self.send(CMD["getDongleBattery"], device_id=DEV_LOCAL)
        if not p or len(p) < 5 or p[0] != 0 or not p[4]:
            return None
        return {"percent": p[2], "charging": bool(p[1]), "present": True}

    def profile(self):
        p = self.send(CMD["getProfile"])
        return None if not p or len(p) < 2 else p[1]

    def polling_rate(self):
        p = self.send(CMD["getReportRate"])
        return None if not p or len(p) < 3 or p[0] != 0 else p[1] | (p[2] << 8)

    def _flag(self, key):
        p = self.send(CMD[key])
        return None if not p or len(p) < 2 else bool(p[1])

    def _byte(self, key):
        p = self.send(CMD[key])
        return None if not p or len(p) < 2 else p[1]

    def dongle_light(self):
        if not self.has_dongle or self.dry_run:
            return None
        p = self.send(CMD["getDongleLight"], device_id=DEV_LOCAL)
        if not p or len(p) < 13:
            return None
        return {
            "on": bool(p[0]), "effect": p[1],
            "hue1": (p[2] << 8) | p[3], "sat1": p[4],
            "hue2": (p[5] << 8) | p[6], "sat2": p[7],
            "hue3": (p[8] << 8) | p[9], "sat3": p[10],
            "speed": max(1, p[11] - 80),    # firmware stores speed + 80
            "brightness": p[12],
        }

    def mouse_light(self):
        if self.dry_run:
            return None
        d = self.read_nv(NV_LIGHT_SETTINGS)
        if not d or len(d) < 3:
            return None
        return {"on": bool(d[0]), "effect": d[1], "speed": d[2]}

    def mouse_colour(self):
        if self.dry_run:
            return None
        d = self.read_nv(NV_LIGHT_COLORS)
        if not d or len(d) < 7:
            return None
        return f"#{d[4]:02X}{d[5]:02X}{d[6]:02X}"

    def dpi(self):
        p = self.send(CMD["getDpi"])
        if not p or len(p) < 3 or p[0] != 0:
            return None
        count, current = p[1], p[2]
        n = 8 if 0 < count <= 8 and len(p) >= 3 + count * 4 else count
        xs, ys = 3, 3 + n * 2
        cols = ys + n * 2
        coloured = len(p) >= cols + n * 3
        stages = []
        for i in range(n):
            x = p[xs + i * 2] | (p[xs + i * 2 + 1] << 8)
            y = p[ys + i * 2] | (p[ys + i * 2 + 1] << 8)
            colour = "#000000"
            if coloured:
                o = cols + i * 3
                colour = f"#{p[o]:02X}{p[o + 1]:02X}{p[o + 2]:02X}"
            stages.append({"index": i, "x": x, "y": y, "colour": colour})
        return {"count": count, "current": current, "stages": stages}

    # -- writes ------------------------------------------------------------
    #
    # Lighting has no "set just this field" command: the firmware takes the
    # whole block or nothing. So every change is read-modify-write, which is
    # exactly what the configurator does behind its switches and sliders.

    def set_dongle_light(self, **changes):
        if not self.has_dongle:
            raise Am97Error("dongle lighting needs the 2.4G dongle connected")
        cfg = self.dongle_light() or dict(DONGLE_LIGHT_FALLBACK)
        cfg.update(changes)
        h1, h2, h3 = cfg["hue1"], cfg["hue2"], cfg["hue3"]
        self.send(CMD["setDongleLight"], [
            1 if cfg["on"] else 0, cfg["effect"],
            (h1 >> 8) & 0xFF, h1 & 0xFF, max(1, min(100, cfg["sat1"])),
            (h2 >> 8) & 0xFF, h2 & 0xFF, max(1, min(100, cfg["sat2"])),
            (h3 >> 8) & 0xFF, h3 & 0xFF, max(1, min(100, cfg["sat3"])),
            max(1, min(20, cfg["speed"])) + 80,
            max(1, min(255, cfg["brightness"])),
        ], device_id=DEV_LOCAL, expect_reply=False)
        return cfg

    def set_mouse_light(self, **changes):
        cfg = self.mouse_light() or dict(MOUSE_LIGHT_FALLBACK)
        cfg.update(changes)
        self.send(CMD["setLightSettings"],
                  [1 if cfg["on"] else 0, cfg["effect"],
                   max(1, min(10, cfg["speed"]))],
                  expect_reply=False)
        return cfg

    def set_mouse_colour(self, colour):
        """Three commands, the same sequence the configurator uses."""
        r, g, b = hex_to_rgb(colour)
        self.send(CMD["saveRgb"],
                  u16le(NV_LIGHT_COLORS) + [RGB_LED_COUNT] + u16le(3)
                  + [RGB_FRAME_INTERVAL, r, g, b], expect_reply=False)
        time.sleep(0.1)
        self.send(CMD["setRgbRealtime"],
                  [RGB_REALTIME_FLAG, 0, RGB_FRAME_INTERVAL, 1, r, g, b],
                  expect_reply=False)
        cur = self.mouse_light() or dict(MOUSE_LIGHT_FALLBACK)
        self.send(CMD["setLightSettings"],
                  [1 if cur["on"] else 0, cur["effect"],
                   max(1, min(10, cur["speed"]))],
                  expect_reply=False)

    def set_polling_rate(self, hz):
        if hz not in VALID_RATES:
            raise Am97Error(f"polling rate must be one of {VALID_RATES}")
        self.send(CMD["setReportRate"], u16le(hz))

    def set_dpi_stage(self, index, x, y=None):
        x = clamp_dpi(x)
        y = clamp_dpi(y) if y is not None else x
        self.send(CMD["setDpi"], [index] + u16le(x) + u16le(y))
        return x, y

    def select_dpi_stage(self, index):
        self.send(CMD["setCurrentDpiStage"], [index])

    def set_profile(self, index):
        self.send(CMD["changeProfile"], [index])

    def set_flag(self, key, value):
        self.send(CMD[key], [1 if value else 0])

    def set_lod(self, value):
        self.send(CMD["setLod"], [max(0, min(2, value))])


# --------------------------------------------------------------------- CLI

FLAGS = {
    "motionsync": "setMotionSync",
    "anglesnap": "setAngleSnapping",
    "ripple": "setRippleControl",
    "fpsmode": "setFpsMode",
}


def collect_status(m):
    return {
        "connection": "dongle" if m.has_dongle else "usb",
        "battery": m.battery(),
        "hub_battery": m.hub_battery(),
        "profile": m.profile(),
        "polling_rate": m.polling_rate(),
        "lod": m._byte("getLod"),
        "motion_sync": m._flag("getMotionSync"),
        "angle_snapping": m._flag("getAngleSnapping"),
        "ripple_control": m._flag("getRippleControl"),
        "mouse_light": m.mouse_light(),
        "mouse_colour": m.mouse_colour(),
        "dongle_light": m.dongle_light(),
        "dpi": m.dpi(),
    }


def print_status(s):
    print(f"connection     {s['connection']}")
    if s["battery"]:
        b = s["battery"]
        print(f"battery        {b['percent']:3d}%"
              f"{'  charging' if b['charging'] else ''}")
    if s["hub_battery"]:
        h = s["hub_battery"]
        print(f"hub battery    {h['percent']:3d}%"
              f"{'  charging' if h['charging'] else ''}")
    print(f"profile        {s['profile']}")
    print(f"polling rate   {s['polling_rate']} Hz")
    print(f"lift-off dist  {s['lod']}")
    print(f"sensor         motion-sync={s['motion_sync']}  "
          f"angle-snap={s['angle_snapping']}  ripple={s['ripple_control']}")
    ml = s["mouse_light"]
    if ml:
        print(f"mouse light    {'on' if ml['on'] else 'off'}  "
              f"{MOUSE_EFFECTS.get(ml['effect'], ml['effect'])}  "
              f"{s['mouse_colour'] or '--'}")
    dl = s["dongle_light"]
    if dl:
        name = next((k for k, v in DONGLE_EFFECTS.items()
                     if v == dl["effect"]), dl["effect"])
        print(f"dongle light   {'on' if dl['on'] else 'off'}  {name}  "
              f"{hue_to_hex(dl['hue1'])} sat={dl['sat1']} "
              f"bright={dl['brightness']} speed={dl['speed']}")
    d = s["dpi"]
    if d:
        print(f"dpi            stage {d['current']} of {d['count']}")
        for st in d["stages"][:d["count"]]:
            mark = "*" if st["index"] == d["current"] else " "
            xy = str(st["x"]) if st["x"] == st["y"] else f"{st['x']}/{st['y']}"
            print(f"  {mark} [{st['index']}] {xy:<12} {st['colour']}")


def build_parser():
    ap = argparse.ArgumentParser(
        prog="am97",
        description="Control the Angry Miao AM Infinity .97 from the "
                    "command line.")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    ap.add_argument("--device", choices=["auto", "dongle", "usb"],
                    default="auto", help="which endpoint to talk to")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output where applicable")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="suppress confirmation output")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the frames instead of sending them")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show everything the device reports")

    p = sub.add_parser("light", help="dongle lighting")
    ls = p.add_subparsers(dest="action", required=True)
    for name in ("on", "off", "toggle", "status"):
        ls.add_parser(name)
    q = ls.add_parser("color", aliases=["colour"]); q.add_argument("hex")
    q = ls.add_parser("brightness"); q.add_argument("value", type=int)
    q = ls.add_parser("speed"); q.add_argument("value", type=int)
    q = ls.add_parser("effect"); q.add_argument("name", choices=DONGLE_EFFECTS)

    p = sub.add_parser("mouse-light", help="mouse lighting")
    ms = p.add_subparsers(dest="action", required=True)
    for name in ("on", "off", "toggle", "status"):
        ms.add_parser(name)
    q = ms.add_parser("color", aliases=["colour"]); q.add_argument("hex")

    p = sub.add_parser("all", help="dongle and mouse lighting together")
    p.add_argument("action", choices=["on", "off", "toggle"])

    p = sub.add_parser("rate", help="polling rate in Hz")
    p.add_argument("hz", type=int, choices=VALID_RATES)

    p = sub.add_parser("dpi", help="DPI stages")
    ds = p.add_subparsers(dest="action")
    q = ds.add_parser("select"); q.add_argument("index", type=int)
    q = ds.add_parser("set")
    q.add_argument("index", type=int)
    q.add_argument("x", type=int)
    q.add_argument("y", type=int, nargs="?")

    p = sub.add_parser("toggle", help="sensor options")
    p.add_argument("feature", choices=sorted(FLAGS))
    p.add_argument("state", choices=["on", "off"])

    p = sub.add_parser("lod", help="lift-off distance (0 low .. 2 high)")
    p.add_argument("value", type=int, choices=[0, 1, 2])

    p = sub.add_parser("profile", help="switch onboard profile")
    p.add_argument("index", type=int)

    p = sub.add_parser("raw", help="send one command id and print the reply")
    p.add_argument("cmd_id", help="decimal or 0x-prefixed")
    p.add_argument("--to", default="0x80", help="device id (0x00 or 0x80)")

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    say = (lambda *a: None) if args.quiet else print

    try:
        m = Am97.open(prefer=args.device, dry_run=args.dry_run)
    except Am97Error as exc:
        print(exc, file=sys.stderr)
        return 1

    try:
        c = args.cmd

        if c == "status":
            s = collect_status(m)
            print(json.dumps(s, indent=2)) if args.json else print_status(s)

        elif c in ("light", "mouse-light"):
            dongle = c == "light"
            read = m.dongle_light if dongle else m.mouse_light
            write = m.set_dongle_light if dongle else m.set_mouse_light
            a = args.action
            if a == "status":
                cur = read()
                if not cur:
                    print("no reply", file=sys.stderr)
                    return 2
                if not dongle:
                    cur = dict(cur, colour=m.mouse_colour())
                print(json.dumps(cur, indent=2))
            elif a in ("on", "off", "toggle"):
                cur = read()
                on = (not cur["on"]) if (a == "toggle" and cur) else (a == "on")
                write(on=on)
                say(f"{'dongle' if dongle else 'mouse'} light -> "
                    f"{'on' if on else 'off'}")
            elif a in ("color", "colour"):
                if dongle:
                    hue, sat = hex_to_hue_sat(args.hex)
                    m.set_dongle_light(hue1=hue, sat1=sat)
                    say(f"dongle colour -> {args.hex.upper()} "
                        f"(hue {hue}, sat {sat})")
                else:
                    m.set_mouse_colour(args.hex)
                    say(f"mouse colour -> {args.hex.upper()}")
            elif a == "brightness":
                m.set_dongle_light(brightness=args.value)
                say(f"dongle brightness -> {args.value}")
            elif a == "speed":
                m.set_dongle_light(speed=args.value)
                say(f"dongle speed -> {args.value}")
            elif a == "effect":
                m.set_dongle_light(effect=DONGLE_EFFECTS[args.name])
                say(f"dongle effect -> {args.name}")

        elif c == "all":
            targets = [("mouse", m.mouse_light, m.set_mouse_light)]
            if m.has_dongle:
                targets.insert(0, ("dongle", m.dongle_light,
                                   m.set_dongle_light))
            else:
                say("no dongle connected -- dongle lighting skipped")
            for label, read, write in targets:
                cur = read()
                on = (not cur["on"]) if (args.action == "toggle" and cur) \
                    else (args.action == "on")
                write(on=on)
                say(f"{label} light -> {'on' if on else 'off'}")

        elif c == "rate":
            m.set_polling_rate(args.hz)
            say(f"polling rate -> {args.hz} Hz")

        elif c == "dpi":
            if args.action == "select":
                m.select_dpi_stage(args.index)
                say(f"dpi stage -> {args.index}")
            elif args.action == "set":
                x, y = m.set_dpi_stage(args.index, args.x, args.y)
                say(f"dpi stage {args.index} -> {x}/{y}")
            else:
                d = m.dpi()
                if not d:
                    print("no reply", file=sys.stderr)
                    return 2
                print(json.dumps(d, indent=2))

        elif c == "toggle":
            m.set_flag(FLAGS[args.feature], args.state == "on")
            say(f"{args.feature} -> {args.state}")

        elif c == "lod":
            m.set_lod(args.value)
            say(f"lift-off distance -> {args.value}")

        elif c == "profile":
            m.set_profile(args.index)
            say(f"profile -> {args.index}")

        elif c == "raw":
            v = m.send(int(args.cmd_id, 0), device_id=int(args.to, 0))
            print(v.hex(" ") if v else "no reply")

    except Am97Error as exc:
        print(exc, file=sys.stderr)
        return 2
    except Exception as exc:                                 # pragma: no cover
        print(f"{args.cmd} failed: {exc}", file=sys.stderr)
        return 2
    finally:
        m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
