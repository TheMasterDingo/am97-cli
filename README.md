# am97-cli
Command-line control for the Angry Miao AM Infinity .97 mouse: lighting, DPI, polling rate, etc..

```
$ am97 status
connection     dongle
battery         94%
hub battery    100%  charging
profile        0
polling rate   4000 Hz
lift-off dist  0
sensor         motion-sync=True  angle-snap=False  ripple=False
mouse light    on  solid  #FF823C
dongle light   on  solid  #FF4C00 sat=75 bright=120 speed=12
dpi            stage 0 of 1
  * [0] 1800         #FFFFFF
```

Written because the configurator is browser-only, which means the mouse can't
take part in desk automation — turning the lights off with everything else,
switching polling rate per game, putting battery in a status bar.

## Install

```
pip install hidapi
git clone https://github.com/<you>/am97
cd am97
python am97.py status
```

Single file, no packaging needed. Drop `am97.py` wherever is convenient.

**Linux** needs udev access to the device:

```
# /etc/udev/rules.d/70-am97.rules
KERNEL=="hidraw*", ATTRS{idVendor}=="0e8d", ATTRS{idProduct}=="0703", MODE="0660", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="0e8d", ATTRS{idProduct}=="0880", MODE="0660", TAG+="uaccess"
```

Then `sudo udevadm control --reload && sudo udevadm trigger`.

**Windows** works out of the box. Close the web configurator tab first — the
browser holds the device open and `am97` will tell you so.

## Usage

```
am97 status                      # everything the device reports
am97 status --json               # same, machine-readable

am97 light off | on | toggle     # dongle lighting
am97 light color '#FF4C00'
am97 light brightness 120
am97 light speed 12
am97 light effect solid|breathing|flow|mosaic|aurora|night|arc

am97 mouse-light off | on | toggle
am97 mouse-light color '#FF823C'

am97 all off                     # dongle + mouse in one go

am97 rate 4000                   # 125 250 500 1000 2000 4000 8000
am97 dpi                         # list stages
am97 dpi select 1
am97 dpi set 0 1600              # x = y
am97 dpi set 0 1600 1400         # independent
am97 lod 0
am97 toggle motionsync on
am97 profile 1
```

Global flags: `--device auto|dongle|usb`, `--json`, `--quiet`, `--dry-run`.
Exit codes: `0` ok, `1` no device, `2` command failed.

## How it works

Commands ride HID feature report `0x14` on the vendor collection `0xFF13`:

```
[payload_len][device_id] [05][type][len_lo][len_hi][cmd_lo][cmd_hi][data...]
```

* `device_id` — `0x00` addresses whatever you're connected to, `0x80`
  addresses the mouse through the dongle's relay.
* `type` — `0x5a` command, `0x5b` reply, `0x5d` write acknowledgement.
* `len` — command id (2 bytes) plus data length.
* Payload is zero-padded to 61 bytes.

Replies arrive by polling the same feature report. The device answers with a
zero-length frame while busy, and can stack several responses into one frame,
so the reader walks the frames until the command id matches.

Two quirks worth knowing if you extend this:

**Lighting is all-or-nothing.** There's no "set just the brightness" command.
The dongle takes one 13-byte block, the mouse takes three bytes, and you send
the whole thing every time. Every change here is therefore read-modify-write,
which is what the configurator does behind its switches too.

**The dongle stores hue, not RGB.** Hue is 0–1530 with saturation 1–100, so
hex colours are converted before sending (`hex_to_hue_sat`). The mouse does
store literal RGB. Speed is stored with 80 added to it.

## Safety

Four command ids are blocked in `BLOCKED` and refused before anything is
sent:

| id | name | what it does |
|---|---|---|
| 12494 | `resetSettings` | wipes all configuration |
| 12395 | `clearBonded` | destroys the 2.4G pairing |
| 12394 | `setPairingUniaa` | pairing handshake |
| 4353 | `rebootPairingDevice` | drops the link |

They exist because **command ids are not safe to enumerate**. Reads, writes
and destructive operations are interleaved in one flat id space with nothing
in the bytes distinguishing them — a "read" of an id that happens to be
`clearBonded` invokes `clearBonded`. This was found the hard way; scanning a
256-id range cost a pairing and a full config. `am97 raw` will happily send
anything not on the block list, so treat it accordingly.

Nothing here writes firmware. The worst realistic outcome is a setting you
have to change back.

## Verified vs. transcribed

The protocol was read out of the configurator's JS bundle and cross-checked
against WebHID captures. These commands were observed on real hardware:

`getBattery`, `getDongleBattery`, `getProfile`, `getReportRate`,
`setReportRate`, `getDongleLight`, `setDongleLight`, `readNvData`, `saveRgb`,
`setRgbRealtime`, `setLightSettings`

The rest — DPI, LOD, sensor toggles, sleep timers, rotation, key remapping —
are transcribed from the bundle and encoded correctly as far as the source
shows, but haven't been exercised. Reports welcome.

Only tested with the dongle connected. The USB-direct path is implemented
(`--device usb`) but unconfirmed.

## Not implemented

Key remapping and macros. The command ids and payload shapes are in the
bundle if someone wants them. Worth noting: the key-remap type table has no
tap/hold distinction, so per-button tap-vs-hold bindings aren't achievable in
firmware as it stands — that one needs Angry Miao.

## Licence

MIT. Not affiliated with or endorsed by Angry Miao. Use at your own risk.
