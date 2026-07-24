# pods-head-tracker

> **Unofficial** open-source bridge. Not affiliated with or endorsed by
> Apple. AirPods is a trademark of Apple Inc.

Turn **AirPods** into a head tracker for **[OpenTrack](https://github.com/opentrack/opentrack)**
(and any game/sim it feeds, e.g. iRacing) on **Windows**, using a small
**Raspberry Pi** as the Bluetooth bridge.

The Pi opens the AirPods' private head-tracking stream over BlueZ, converts the
orientation to pitch/yaw, and forwards it to OpenTrack — over **Wi-Fi (UDP)**
or a **USB cable**. It runs as a boot-time service, so you just
power the Pi and wear the AirPods.

https://github.com/user-attachments/assets/617cd9a7-1b34-467b-9f04-5ade3d9c7e3e

## Why a Raspberry Pi (and not just Windows)?

Windows has **no user-mode API for Bluetooth Classic L2CAP** — the transport
the AirPods use — so a normal Windows program can't open the head-tracking
channel (confirmed empirically: a raw Winsock `BTHPROTO_L2CAP` connect to PSM
`0x1001` fails). And the AirPods only stream sensor data over an **encrypted**
link, which BlueZ provides out of the box. A Raspberry Pi running Linux/BlueZ
does both for free, and bridges the result to Windows.

The Pi is convenience, not a hard requirement: the bridge is plain Python
stdlib on top of the Linux Bluetooth stack, so it should run on **any Linux
machine** with BlueZ and a Bluetooth Classic (BR/EDR) adapter (untested
beyond the Pi). The USB serial transport is the one Pi-ism — it needs a
board with a USB *device*/OTG controller like the Pi Zero's, so other
bridge hardware is Wi-Fi UDP only.

Windows isn't required either. If your gaming rig runs **Linux** (CachyOS,
Bazzite, ...) and plays the sims natively or through Proton, OpenTrack's
Linux build has the same "FreePIE UDP receiver" input — set `UDP_HOST` to
the rig's IP and skip Windows entirely. The rig can even double as the
bridge: run `opentrack_bridge.py` on it with `UDP_HOST=127.0.0.1` and no
Pi at all. For Proton games, use OpenTrack's Wine/Proton freetrack output;
for native titles, its virtual-joystick output. Untested, but nothing in
the pipeline is Windows-specific.

Tested on a **Pi Zero 2 W** (Raspberry Pi OS, BlueZ 5.82) with **AirPods 4
(ANC)**. It should work with every AirPods model released after AirPods
(2nd generation) — AirPods (3rd gen), AirPods 4, all AirPods Pro, AirPods
Max — since they all carry the motion sensors and speak the same
head-tracking stream. No dependencies — Python 3 standard library only, as
shipped with Raspberry Pi OS.

## What's here

| File | Purpose |
|---|---|
| `opentrack_bridge.py` | The bridge: streams orientation, computes pitch/yaw, sends it to OpenTrack over UDP and/or USB serial. Auto-reconnects and re-triggers the stream. |
| `airpods_ht_probe.py` | Diagnostic: connect, start head tracking, print/decode the raw sensor packets. |
| `pods-head-tracker.conf.example` | Config template — your AirPods MAC, transport choice, PC IP. Copy to `/etc/pods-head-tracker.conf` on the Pi. |
| `pods-head-tracker.service` | systemd unit — runs the bridge on boot, restarts on failure. Generic; all settings come from the config file. |
| `setup-usb-serial.sh` | One-shot script that turns the Pi Zero into a USB serial gadget for the USB transport. |
| `setup-usb-audio.sh` | One-shot script that adds **USB audio**: hear the game through the AirPods over the same USB cable. Installs the four files below. |
| `pods-usb-gadget.sh` / `.service` | Boot-time script + unit that build the composite USB gadget (serial **+** sound card) that replaces `g_serial` when USB audio is installed. |
| `pods-usb-audio.sh` / `.service` | Forwarder + unit that pump the USB audio into the AirPods over Bluetooth A2DP (BlueALSA). |

## Setup

### 1. Pair + bond the AirPods (persistent)

AirPods↔BlueZ bonding is finicky; this exact recipe stores a persistent link
key so the Pi reconnects **without pairing mode** and survives reboots:

```bash
sudo systemctl restart bluetooth      # REQUIRED: clears a stuck LE-only
                                      # discovery filter that hides the
                                      # AirPods' Classic address
bluetoothctl power on
bluetoothctl pairable on
# AirPods in pairing mode (case open, hold button until white LED):
bluetoothctl scan on                  # wait for "<MAC> AirPods", then: scan off
bluetoothctl pair <AIRPODS_MAC>
bluetoothctl trust <AIRPODS_MAC>
# The follow-up "connect" failing with br-connection-profile-unavailable is
# EXPECTED (that's the audio profile, unused unless you install USB audio —
# after setup-usb-audio.sh it should succeed) — the AACP channel still works.
```

Verify: `sudo grep -c LinkKey /var/lib/bluetooth/<ADAPTER>/<AIRPODS_MAC>/info`
should print `1` (use the explicit path — a `*/` glob can't read the
root-only dirs).

> The MAC you pair (and put in the config) is the AirPods' Bluetooth
> **Classic** address — the one `scan on` shows after the bluetooth restart,
> not the LE address.

### 2. Deploy to the Pi

```bash
scp opentrack_bridge.py airpods_ht_probe.py pods-head-tracker.conf.example \
    pods-head-tracker.service setup-usb-serial.sh setup-usb-audio.sh \
    pods-usb-gadget.sh pods-usb-gadget.service \
    pods-usb-audio.sh pods-usb-audio.service pi@<PI_HOST>:~/
```

### 3. Create your config

On the Pi:

```bash
sudo install -m 644 pods-head-tracker.conf.example /etc/pods-head-tracker.conf
sudo nano /etc/pods-head-tracker.conf   # set AIRPODS_MAC, TRANSPORT, UDP_HOST
```

The config file is the only thing you personalize — the service unit is
generic and never needs editing.

### 4. Install the bridge and the service

```bash
sudo install -m 755 opentrack_bridge.py /usr/local/bin/
sudo install -m 644 pods-head-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pods-head-tracker
```

The bridge lives in `/usr/local/bin`, so nothing depends on your username or
home directory. When updating to a newer version later, just re-run the
first command and restart the service.

### 5. Pick a transport

#### Option A — Wi-Fi UDP (zero cables)

`TRANSPORT=udp` in the config (the default), with `UDP_HOST` set to your PC's
LAN IP. In OpenTrack: Input = **"FreePIE UDP receiver"** → its settings →
**port 4242** (or your `UDP_PORT`) → **Start**. Allow it through the firewall
if prompted — LAN packets from the Pi need the inbound allow.

Recommended on a Pi Zero 2 W — disable Wi-Fi power-save (stops lag spikes):

```bash
con=$(nmcli -t -f NAME,TYPE connection show --active | grep -i wireless | head -1 | cut -d: -f1)
sudo nmcli connection modify "$con" 802-11-wireless.powersave 2   # 2 = disable
```

#### Option B — USB serial (recommended: no network, no dropouts)

Removes Wi-Fi from the path entirely: the Pi plugs into the PC as a USB
serial gadget and shows up as a COM port.

On the Pi (once):

```bash
chmod +x setup-usb-serial.sh
sudo ./setup-usb-serial.sh
sudo reboot
```

Then:

1. Cable the Pi Zero's inner **USB** port (NOT **PWR**) to a **data** USB port
   on the PC with a data-capable cable. The PC port also powers the Pi.
2. Windows Device Manager shows **"USB Serial Device (COMx)"** — the driver is
   inbox on Windows 10/11, nothing to install. (Windows 7 would need a
   CDC-ACM `.inf`; out of scope here.)
3. Set `TRANSPORT=serial` in `/etc/pods-head-tracker.conf`, then
   `sudo systemctl restart pods-head-tracker`.
4. In OpenTrack: Input = **"Hatire Arduino"** → its settings → select the COM
   port, baud **115200**, and **leave everything else at defaults** (the
   bridge emits frames in OpenTrack's default axis order) → **Start**.

`TRANSPORT=both` sends over Wi-Fi and USB simultaneously if you want to
compare or keep a fallback.

Note: `dr_mode=peripheral` means that USB port can no longer *host* devices
(keyboards, hubs) — it's dedicated to the gadget role.

## USB audio: hear the game through the AirPods (optional)

Since the AirPods are already on your head and already talking to the Pi,
the same setup can carry game audio: the Pi shows up on the PC as a **USB
sound card** next to the COM port, and everything Windows plays into it is
forwarded to the AirPods over Bluetooth (A2DP). One cable, one pair of buds,
head tracking and audio together. Playback only — the AirPods' microphone is
not bridged.

On the Pi (once, after the files from step 2 are on it):

```bash
chmod +x setup-usb-audio.sh
sudo ./setup-usb-audio.sh
sudo reboot
```

This **replaces** the `g_serial` gadget with a composite one (serial **+**
audio, built at boot by `pods-usb-gadget.service`). The serial transport is
unchanged — still `/dev/ttyGS0`, config untouched — but don't run
`setup-usb-serial.sh` afterwards; it would undo the composite (it refuses,
with an explanation, if you try). You don't need to have run it before
either: `setup-usb-audio.sh` works on a fresh Pi.

Then on Windows:

1. Device Manager re-enumerates the gadget as a new device: the COM port
   gets a **new number** — re-select it once in OpenTrack's Hatire settings
   — and an **"AirPods Bridge"** playback device appears in Settings →
   System → Sound (driver is inbox on Windows 10/11, nothing to install).
2. Set "AirPods Bridge" as the default output — or route only the game to it
   via Settings → System → Sound → Advanced → App volume and device
   preferences.

What to expect:

- **Latency is Bluetooth-audio latency**, roughly 150–300 ms end to end:
  fine for immersion, engine noise, and music; not for competitive audio
  cues. Tune with `AUDIO_LATENCY_MS` in the config (lower = snappier,
  higher = fewer dropouts). The shipped default of **50** is conservative;
  **20** ran clean on the tested Pi Zero 2 W with tracking active, and is
  a good target. Going lower buys nothing audible — the AirPods' own
  ~150 ms sink buffer dominates the total either way.
- Codec is SBC (what Debian's BlueALSA ships). Audio pauses when the buds go
  in the case and resumes on its own when they come out — same retry loop as
  the tracking service.
- **Loudness** is your normal Windows volume slider (it scales the USB
  stream). The buds' own Bluetooth ceiling is set once per connection from
  `AUDIO_VOLUME` (default 70 of 127) — the forwarder sets it explicitly
  because a never-used A2DP source can otherwise sit at volume zero and
  play "healthy" silence.
- With audio on, prefer `TRANSPORT=serial` over `both`: Wi-Fi and Bluetooth
  share the Pi's antenna, and an idle Wi-Fi keeps the air clear for A2DP.

**Head tracking only, audio off:** if you never run `setup-usb-audio.sh`,
none of this exists and tracking works as before. If you installed audio
and want it off temporarily, set `AUDIO_ENABLE=0` in the config and
`sudo systemctl restart pods-usb-audio` — the audio service goes idle,
tracking is untouched. Set it back to `1` (or delete the line) to re-enable.

Troubleshooting: `systemctl status pods-usb-gadget pods-usb-audio bluealsa`,
`arecord -l` (the sound card is `UAC2Gadget`), and
`sudo journalctl -u pods-usb-audio -f` while it retries.

## Daily use

Power the Pi, put the AirPods in your ears — the service connects on its own
and starts streaming within a few seconds. It calibrates the neutral
("forward") pose right after each connection: face forward and hold still
for a moment — a still head calibrates in a fraction of a second, and
samples captured while moving are discarded and retried automatically
(watch for `calibrated` in the journal). To re-center:
`sudo systemctl restart pods-head-tracker`.

```bash
systemctl status pods-head-tracker              # running / connected?
sudo journalctl -u pods-head-tracker -f         # live log
```

## Gotchas

- **Only one L2CAP connection at a time** — stop the service before running
  the probe/bridge by hand.
- **`pkill -f opentrack_bridge` can kill your own SSH shell** if the command
  line contains that string. Use `pkill -9 -f '[o]pentrack_bridge'`.
- **`Host is down` (errno 112)** in the journal just means the AirPods aren't
  reachable yet (in the case / out of ear) — the service keeps retrying.
- **Missing config**: if `/etc/pods-head-tracker.conf` doesn't exist the
  service fails immediately with a clear message in `systemctl status` —
  that's deliberate.
- **Choppy tracking only while audio plays** (USB audio installed): tracking
  and audio share one Bluetooth link. Raise `AUDIO_LATENCY_MS`, and use
  `TRANSPORT=serial` so Wi-Fi stays off the shared antenna.

## Known limitations

- **Yaw + pitch only.** Roll isn't derivable from the AirPods' orientation
  triple with the current math and is always sent as 0.
- One pair of AirPods per Pi; the bridge owns the single AACP channel.

## Credits & license

The AirPods AACP protocol details, the head-tracking packet format, and the
pitch/yaw math are derived from **[LibrePods](https://github.com/kavishdevar/librepods)**
(GPLv3) — specifically its `HeadOrientation` head-tracking implementation.
This project is a downstream, Raspberry-Pi-based reimplementation and is
likewise licensed **[GPLv3](LICENSE)**.

Inspired by **[sony-head-tracker](https://github.com/NicholasSlattery/sony-head-tracker)**,
which turns Sony headphones' motion sensors into an OpenTrack head tracker
and showed how well earbud-based tracking works for gaming.
