# pods-head-tracker

> **Unofficial** open-source bridge. This project has no connection with
> Apple. Apple does not approve this project. AirPods is a trademark of
> Apple Inc.

This project changes **AirPods** into a head tracker for
**[OpenTrack](https://github.com/opentrack/opentrack)** on **Windows**.
OpenTrack sends the head movement to games and simulators, for example
iRacing. A small **Raspberry Pi** operates as the Bluetooth bridge.

The Pi opens the private head-tracking stream of the AirPods with BlueZ.
The Pi converts the orientation data into pitch and yaw values. The Pi
sends these values to OpenTrack through **Wi-Fi (UDP)** or through a
**USB cable**. The bridge starts automatically when the Pi starts. To use
it, apply power to the Pi and put the AirPods in your ears.

https://github.com/user-attachments/assets/617cd9a7-1b34-467b-9f04-5ade3d9c7e3e

## The two primary functions

### 1. Head tracking for games

- The AirPods measure the movement of your head.
- The bridge calculates pitch and yaw from this data.
- The bridge sends the values to OpenTrack.
- OpenTrack moves your view in the game or the simulator.

### 2. Audio loopback: game audio in the AirPods (optional)

- The Pi shows on the PC as a **USB sound card**.
- The PC plays the game audio into this sound card.
- The Pi sends the audio to the AirPods through Bluetooth (A2DP).
- One USB cable carries the head-tracking data and the game audio.

## Why a Raspberry Pi and not only Windows?

Windows has no user-mode API for Bluetooth Classic L2CAP. The AirPods use
this transport. Thus a normal Windows program cannot open the
head-tracking channel. A test confirmed this: a raw Winsock
`BTHPROTO_L2CAP` connection to PSM `0x1001` fails. Also, the AirPods send
sensor data only through an **encrypted** link. BlueZ supplies this
encryption with no extra configuration.

A Raspberry Pi with Linux and BlueZ does the two tasks. The Pi then sends
the result to Windows.

The Pi is not mandatory. The bridge is Python code that uses only the
standard library and the Linux Bluetooth stack. Thus the bridge can
operate on each Linux machine that has BlueZ and a Bluetooth Classic
(BR/EDR) adapter. We tested only the Pi. The USB serial transport is the
one exception: it needs a board with a USB device/OTG controller, for
example the Pi Zero. Other bridge hardware can use only the Wi-Fi UDP
transport.

Windows is also not mandatory:

- If your gaming PC uses **Linux** (for example CachyOS or Bazzite),
  OpenTrack for Linux has the same "FreePIE UDP receiver" input.
- Set `UDP_HOST` to the IP address of the gaming PC. Then Windows is not
  necessary.
- The gaming PC can also be the bridge: run `opentrack_bridge.py` on it
  with `UDP_HOST=127.0.0.1`. Then a Pi is not necessary.
- For Proton games, use the Wine/Proton freetrack output of OpenTrack.
  For native games, use its virtual-joystick output.
- We did not test this configuration. But no part of the pipeline is for
  Windows only.

We tested a **Pi Zero 2 W** (Raspberry Pi OS, BlueZ 5.82) with **AirPods 4
(ANC)**. We did not test the other models. But all AirPods models after
AirPods (2nd generation) have the motion sensors and use the same
head-tracking stream. Thus the bridge can operate with: AirPods (3rd
generation), AirPods 4, all AirPods Pro, and AirPods Max. The bridge has
no dependencies. It uses only the Python 3 standard library that comes
with Raspberry Pi OS.

## Contents of this repository

| File | Function |
|---|---|
| `opentrack_bridge.py` | The bridge. It reads the orientation stream, calculates pitch and yaw, and sends the data to OpenTrack through UDP and/or USB serial. It connects again automatically and starts the stream again after an interruption. |
| `airpods_ht_probe.py` | A diagnostic tool. It connects, starts head tracking, and shows the raw sensor packets in decoded form. |
| `pods-head-tracker.conf.example` | The configuration template. It holds your AirPods MAC address, the transport selection, and the PC IP address. Copy it to `/etc/pods-head-tracker.conf` on the Pi. |
| `pods-head-tracker.service` | The systemd unit. It starts the bridge at boot time and starts it again after a failure. It is generic; all settings come from the configuration file. |
| `install.sh` | The installer and updater for the three files above. It copies the bridge and the unit into place. It creates the configuration file from the template on the first run. It enables and starts the service after configuration. |
| `setup-usb-serial.sh` | A one-time script. It changes the Pi Zero into a USB serial gadget for the USB transport. |
| `setup-usb-audio.sh` | A one-time script. It adds **USB audio**: you hear the game through the AirPods through the same USB cable. It installs the four files below. |
| `pods-usb-gadget.sh` / `.service` | A boot-time script and unit. They build the composite USB gadget (serial **+** sound card). This gadget replaces `g_serial` when USB audio is installed. |
| `pods-usb-audio.sh` / `.service` | A forwarder and its unit. They send the USB audio to the AirPods through Bluetooth A2DP (BlueALSA). |

## Setup

### 1. Pair and bond the AirPods (permanent)

The bond between AirPods and BlueZ can fail easily. Obey this procedure
exactly. The procedure stores a permanent link key. Then the Pi connects
again **without pairing mode**, and the bond stays after a reboot:

```bash
sudo systemctl restart bluetooth      # REQUIRED: this removes a stuck LE-only
                                      # discovery filter. The filter hides the
                                      # Classic address of the AirPods.
bluetoothctl power on
bluetoothctl pairable on
# Put the AirPods in pairing mode (open the case, hold the button until the LED is white):
bluetoothctl scan on                  # wait for "<MAC> AirPods", then: scan off
bluetoothctl pair <AIRPODS_MAC>
bluetoothctl trust <AIRPODS_MAC>
# A "connect" command that fails with br-connection-profile-unavailable is
# EXPECTED. That is the audio profile. The bridge does not use it unless you
# install USB audio. After setup-usb-audio.sh, the connect command is
# successful. The AACP channel operates correctly in both cases.
```

To make sure that the bond is stored, run:

```bash
sudo grep -c LinkKey /var/lib/bluetooth/<ADAPTER>/<AIRPODS_MAC>/info
```

The output must be `1`. Use the full explicit path. A `*/` glob cannot
read these directories, because only root can read them.

> Pair the Bluetooth **Classic** address of the AirPods, and put this
> address in the configuration. This is the address that `scan on` shows
> after the bluetooth restart. It is not the LE address.

### 2. Copy the files to the Pi

```bash
scp *.py *.sh *.service *.conf.example pi@<PI_HOST>:~/
```

### 3. Install the bridge (configuration + service)

On the Pi:

```bash
chmod +x install.sh
sudo ./install.sh                       # installs the files, creates the configuration
sudo nano /etc/pods-head-tracker.conf   # set AIRPODS_MAC, TRANSPORT, UDP_HOST
sudo ./install.sh                       # run again: enables + starts the service
```

You change only the configuration file. The service unit is generic; do
not edit it. The bridge is in `/usr/local/bin`. Thus no part depends on
your username or your home directory.

To update the installation, run `sudo ./install.sh` again. The script
never replaces your configuration file. If you prefer manual steps:
`install.sh` contains only the usual `install` and `systemctl` commands
in the correct sequence. You can read the script.

### 4. Select a transport

#### Option A — Wi-Fi UDP (no cables)

1. Set `TRANSPORT=udp` in the configuration. This is the default value.
2. Set `UDP_HOST` to the LAN IP address of your PC.
3. In OpenTrack, set Input to **"FreePIE UDP receiver"**.
4. Open the input settings. Set the port to **4242** (or your `UDP_PORT`
   value).
5. Click **Start**.
6. If the firewall shows a prompt, permit the connection. The inbound
   packets from the Pi need this permission.

Recommendation for a Pi Zero 2 W: disable the Wi-Fi power-save function.
This stops lag spikes.

```bash
con=$(nmcli -t -f NAME,TYPE connection show --active | grep -i wireless | head -1 | cut -d: -f1)
sudo nmcli connection modify "$con" 802-11-wireless.powersave 2   # 2 = disable
```

#### Option B — USB serial (recommended: no network, no dropouts)

This option removes Wi-Fi from the path. The Pi connects to the PC as a
USB serial gadget. The PC shows the Pi as a COM port.

On the Pi (one time):

```bash
chmod +x setup-usb-serial.sh
sudo ./setup-usb-serial.sh
sudo reboot
```

Then:

1. Connect the inner **USB** port of the Pi Zero (NOT the **PWR** port)
   to a **data** USB port on the PC. Use a cable that can carry data. The
   PC port also supplies power to the Pi.
2. Windows Device Manager shows **"USB Serial Device (COMx)"**. Windows
   10 and 11 have the driver. You do not install software. (Windows 7
   needs a CDC-ACM `.inf` file. This document does not include
   Windows 7.)
3. Set `TRANSPORT=serial` in `/etc/pods-head-tracker.conf`. Then run
   `sudo systemctl restart pods-head-tracker`.
4. In OpenTrack, set Input to **"Hatire Arduino"**. Open the input
   settings. Select the COM port. Set the baud rate to **115200**. Keep
   all other settings at their default values (the bridge sends frames in
   the default axis sequence of OpenTrack). Click **Start**.

`TRANSPORT=both` sends the data through Wi-Fi and USB at the same time.
Use this value to compare the transports or to keep a backup transport.

Note: `dr_mode=peripheral` has an effect on that USB port. The port can
then not operate as a host for devices (keyboards, hubs). The port
operates only in the gadget role.

## USB audio loopback: hear the game through the AirPods (optional)

The AirPods are on your head, and they are connected to the Pi. The same
setup can carry the game audio:

- The Pi shows on the PC as a **USB sound card**, adjacent to the COM
  port.
- Windows plays audio into this sound card.
- The Pi sends this audio to the AirPods through Bluetooth (A2DP).

One cable and one pair of AirPods supply head tracking and audio
together. This function is playback only. The bridge does not carry the
AirPods microphone.

On the Pi (one time, after the files from step 2 are on it):

```bash
chmod +x setup-usb-audio.sh
sudo ./setup-usb-audio.sh
sudo reboot
```

This script **replaces** the `g_serial` gadget with a composite gadget
(serial **+** audio). `pods-usb-gadget.service` builds the composite
gadget at boot time. The serial transport does not change: the device is
still `/dev/ttyGS0`, and the configuration stays the same.

Do not run `setup-usb-serial.sh` after this. That script would remove the
composite gadget. (If you try, the script refuses and shows an
explanation.) It is not necessary to run `setup-usb-serial.sh` before
`setup-usb-audio.sh`. The script `setup-usb-audio.sh` operates on a new
Pi.

Then, on Windows:

1. Device Manager finds the gadget again as a new device. The COM port
   gets a **new number**. Select the new port one time in the Hatire
   settings of OpenTrack. An **"AirPods Bridge"** playback device shows
   in Settings → System → Sound. Windows 10 and 11 have the driver. You
   do not install software.
2. Set "AirPods Bridge" as the default output. As an alternative, send
   only the game audio to it: Settings → System → Sound → Advanced → App
   volume and device preferences.

What to expect:

- **The latency is Bluetooth-audio latency**: approximately 150–300 ms
  end to end. This is satisfactory for immersion, engine noise, and
  music. It is not satisfactory for competitive audio signals. Adjust the
  latency with `AUDIO_LATENCY_MS` in the configuration. A lower value
  gives a faster response. A higher value gives fewer dropouts. The
  default value of **50** is safe. A value of **20** operated without
  errors on the tested Pi Zero 2 W with tracking active. Thus 20 is a
  good target. Values below 20 give no audible improvement, because the
  internal buffer of the AirPods (approximately 150 ms) is the largest
  part of the total.
- The codec is SBC. This is the codec in the BlueALSA package of Debian.
  The audio stops when you put the AirPods in the case. The audio
  continues automatically when you remove them. This is the same retry
  loop as the tracking service.
- The Windows volume control sets the **loudness**. It scales the USB
  stream. `AUDIO_VOLUME` sets the Bluetooth maximum of the AirPods one
  time for each connection (default 70 of 127). The forwarder sets this
  value explicitly. Without this, an unused A2DP source can stay at
  volume zero and play silence that seems healthy.
- When audio is on, use `TRANSPORT=serial` and not `both`. Wi-Fi and
  Bluetooth use the same antenna on the Pi. Idle Wi-Fi keeps the air
  clear for A2DP.

**Head tracking only, audio off:** If you do not run
`setup-usb-audio.sh`, the audio functions are not installed, and the
tracking operates as before. To stop audio temporarily after
installation:

1. Set `AUDIO_ENABLE=0` in the configuration.
2. Run `sudo systemctl restart pods-usb-audio`.

The audio service becomes idle. The tracking continues. To start audio
again, set the value to `1` (or remove the line) and restart the service.

Troubleshooting commands:

- `systemctl status pods-usb-gadget pods-usb-audio bluealsa`
- `arecord -l` (the sound card name is `UAC2Gadget`)
- `sudo journalctl -u pods-usb-audio -f` while the service does its
  retries

## Daily use

1. Apply power to the Pi.
2. Put the AirPods in your ears.

The service connects automatically. The stream starts in a few seconds.
After each connection, the bridge calibrates the neutral ("forward")
position. Look forward and keep your head still for a moment. A still
head calibrates in a fraction of a second. The bridge discards samples
that it captures during movement, and tries again automatically. The
journal shows `calibrated` when calibration is complete.

```bash
systemctl status pods-head-tracker              # state + battery summary
sudo journalctl -u pods-head-tracker -f         # live log
```

### Re-centering (set the forward position again)

On the **serial transport**, re-centering is usually automatic. The
bridge re-centers **each time you click Start in OpenTrack**. (The bridge
detects that the PC reads the COM port again after a stop.) Thus sit
straight and look forward when you click Start. A stop shorter than
approximately 3 seconds does not cause a re-center. Thus a short PC
interruption cannot re-center you in the middle of a race. Set
`RECENTER_ON_START=0` to disable this function.

To set the neutral position again at a different moment, on **each
transport**, without a restart:

```bash
sudo systemctl kill --signal=SIGUSR1 pods-head-tracker
```

Then look forward and keep your head still for a moment. The journal
shows `recenter requested`, then `calibrated`.

OpenTrack can also cause a re-center during a session through the serial
port:

- Put `RECENTER` (the default value of `RECENTER_CMD`) in the **Reset
  command** field of the Hatire dialog. Then the **Reset** button
  re-centers the bridge.
- The **Send** box of the dialog also operates.

The *Center hotkey* of OpenTrack does not go to the bridge. It sets a new
zero in the PC software only. That is satisfactory for daily use. But a
re-center in the bridge also moves the ±180° wrap seam away from your
forward direction. A PC-side center cannot do this.

Why does the Wi-Fi transport not re-center automatically on Start? The
FreePIE UDP input of OpenTrack only receives data. No data goes back to
the Pi. Thus the bridge cannot know when OpenTrack starts to listen. Use
SIGUSR1 with this transport.

`sudo systemctl restart pods-head-tracker` is the full reset, and it
always operates.

### Battery and status

The AirPods send battery reports through the same connection. The bridge
decodes these reports and writes them to the journal:

- at each change
- as a heartbeat, every 15 minutes (`BATTERY_LOG_SECS`)
- as a warning, when a bud discharges to 20% (`BATTERY_WARN_PCT`)

The service status also shows the current state:

```console
$ systemctl status pods-head-tracker
     ...
     Status: "streaming | battery L 82% R 80% case 71%"
```

## Cautions

- **Only one L2CAP connection is possible at one time.** Stop the service
  before you run the probe or the bridge manually.
- **`pkill -f opentrack_bridge` can kill your own SSH shell** if your
  command line contains that string. Use
  `pkill -9 -f '[o]pentrack_bridge'`.
- **`Host is down` (errno 112)** in the journal means only that the
  AirPods are not available (in the case, or out of the ear). The service
  tries again.
- **No data with the buds in your ears:** the journal shows
  `no data - re-sent start` continuously, but the battery and ear reports
  are correct. Two START packet variants exist, and some models only
  answer the other variant. Set `HT_START=both` in the configuration and
  restart the service. The bridge then sends the two variants alternately
  until data arrives. The journal line `stream started after start ...`
  shows the variant that operates on your model; you can then set
  `HT_START` to that variant (`alt` or `def`) permanently. The probe
  (`airpods_ht_probe.py --variant both --burst full`) gives the same
  answer with more detail.
- **Missing configuration:** If `/etc/pods-head-tracker.conf` does not
  exist, the service fails immediately. `systemctl status` shows a clear
  message. This is intentional.
- **Tracking is irregular only while audio plays** (with USB audio
  installed): tracking and audio use one Bluetooth link. Increase
  `AUDIO_LATENCY_MS`, and use `TRANSPORT=serial` to keep Wi-Fi off the
  shared antenna.

## Known limitations

- **Yaw and pitch only.** The current mathematics cannot derive roll from
  the orientation values of the AirPods. The bridge always sends roll
  as 0.
- One pair of AirPods for each Pi. The bridge owns the single AACP
  channel.

## Credits and license

The AACP protocol details, the head-tracking packet format, and the
pitch/yaw mathematics come from
**[LibrePods](https://github.com/kavishdevar/librepods)** (GPLv3). The
sources are its `HeadOrientation` head-tracking implementation and its
documentation of the battery packets and the ear-detection packets. This
project is a downstream implementation for the Raspberry Pi. It has the
same license: **[GPLv3](LICENSE)**.

**[sony-head-tracker](https://github.com/NicholasSlattery/sony-head-tracker)**
gave the idea for this project. That project changes the motion sensors
of Sony headphones into an OpenTrack head tracker. It showed that earbud
tracking operates well for games.
