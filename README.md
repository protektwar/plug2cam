# plug2cam

A local desktop viewer and control tool for "Plug2View" / HK-family mini Wi-Fi cameras. It talks to the cameras directly on your LAN, with no vendor app and no cloud.

The protocol was reverse-engineered from packet captures and the Plug2View Android app.

## Features

- **Auto-discovery:** cameras on the LAN are found automatically, and a new LAN search runs every 15 s.
- **Live panel:** one tile per camera, all streaming at the same time.
- **Camera window:** click a tile to open the camera in its own window, with:
  - fullscreen (F11 or double-click);
  - snapshot (S);
  - recording of every frame as a JPEG;
  - live stats: resolution, fps, frame size, dropped frames, reconnects.
- **Remembered cameras:** cameras are saved and reconnected at the next start, even when broadcasts don't reach them.
- **Camera list** (toolbar):
  - switch a camera on or off;
  - rename it;
  - remove and restore (a removed camera is not added back by scans);
  - forget it completely;
  - add a camera by IP;
  - turn auto-add for new cameras on or off.
- **Video settings:**
  - quality: Low, Normal, Good, High, Excellent, or a custom bitrate;
  - resolution: 320×240, 640×480 or 1280×720;
  - frame rate.
- **Wi-Fi setup:**
  - scan for the networks the camera can see;
  - join it to a network, with DHCP or a static IP.
- **Auto-reconnect:** stalled streams are reconnected automatically.

## Requirements

- Python 3.8+
- PyQt6 (or PyQt5)

`camb_gui.py` is a single self-contained file, with the camera protocol built in.

```
pip install PyQt6
```

## Usage

### GUI

```
python3 camb_gui.py                          # find cameras on the LAN and show them all
python3 camb_gui.py --cams 192.168.1.50      # also add cameras by IP (comma separated)
python3 camb_gui.py --bcast 192.168.178.255  # extra broadcast address(es) for discovery
python3 camb_gui.py --scan 30                # seconds between LAN searches (default 15)
```

| Where | Action |
|---|---|
| Main window | Click a tile to open the camera; right-click a tile for Wi-Fi… and Remove camera… |
| Toolbar | **Scan now**, **Camera list** |
| Camera window | **Settings…** (video quality), **WiFi…**, **Record frames**, **Snapshot**, **Fullscreen** |

Saved cameras live in `~/.config/camb_gui/cameras.json`, or under `$XDG_CONFIG_HOME` if that is set.

## Important: one program per machine

The cameras talk to **UDP port 5000** on the client. If two programs use that port on the same machine (for example, two copies of the GUI), the camera's packets are split between them and the streams stall.

`camb_gui.py` refuses to start while the port is in use. Close the other program first.

## Wi-Fi setup

1. Connect the PC to the camera's own hotspot, or to the network the camera is on now.
2. Start the GUI. The camera should appear.
3. Open the camera, then **WiFi…**. Pick or type the network, enter the password, and press **Apply**.
4. The camera restarts and joins the new network. Switch the PC back to that network, and the camera reappears after the next scan.

> If the network name or password is wrong, the camera will not come back on the network. You may then need its hotspot or its reset button. Try it first on a camera you can easily reach.

The command is sent only to the selected camera, never as a broadcast. (The official app broadcasts it, which would reconfigure every camera that hears it.)

## Video quality values

| Quality | `stream` | Default resolution (`enc`) | Default fps (`rate`) |
|---|---|---|---|
| Low | 1 | 320×240 (3) | 6 |
| Normal | 2 | 640×480 (5) | 6 |
| Good | 3 | 640×480 (5) | 12 |
| High | 4 | 640×480 (5) | 15 |
| Excellent | 5 | 640×480 (5) | 15 |
| Custom | bitrate in kbps (>5) | any | any |

Resolution codes (`enc`): 3 = 320×240, 5 = 640×480, 9 = 1280×720. The frame rates offered by the app are 3, 6, 9, 12, 15 and 30.

## Protocol notes

| Part | Details |
|---|---|
| Transport | UDP 5000 ↔ 5000 |
| Control packets | `00 00 [(len<<4) u16][2B][00][(len-4) u16] d1 07 00 00` + bencode XOR `0xE9` |
| Data packets | `[seq u16][(len<<4) u16] 32 01 00 [(len-4) u16][code u16] 00 00` + text (2 bytes plain, rest XOR `0xE9`) |
| Video session | `HK_RES_REQ` → `SessionCreate` → `SessionStart` → MJPEG frames → `SessionDelete` |
| Read settings | `MainCmd=800` |
| Set video | `MainCmd=801;SubCmd=803;stream=…;enc=…;rate=…;` |
| LAN search | UDP 2627 (client port 2726); `MainCmd=LocalData` |
| Wi-Fi | On the LAN-search port as plain bencode: `MainCmd=703` = scan (reply `MainCmd=LocalWifi`, keys `ValueN<i>` / `WifiDataEnc<i>` / `WifiSafeType<i>`), `MainCmd=700` = join a network |

`MainCmd=700` carries these keys:

- `wifisid`: the network name;
- `wifipassword`: the password, each byte XOR `0x3C`;
- `encrytype` / `safetype`: the security type, sent as `auto`;
- `bootproto`: `dhcp` or `static`;
- `Ip`, `netmask`, `netwet` (the gateway), `dns0`, `dns1`: the address settings;
- `isopen`: 1 = Wi-Fi on.

The GUI's Wi-Fi packets are byte-identical to the official app's, apart from 3 random ID bytes in the header.

## Not yet verified on real cameras

- 1280×720 (`enc=9`), 30 fps, and the allowed custom-bitrate range.
- The format of the Wi-Fi scan reply, which was inferred from the app's native library. If the scan list stays empty, type the network name instead.
- Reading the current Wi-Fi settings is not implemented. The dialog fills in only the network name the camera reports in its LAN-search reply.

## Files

| File | Purpose |
|---|---|
| `camb_gui.py` | Desktop GUI, including the camera protocol |
