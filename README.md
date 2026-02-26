# Apple TV 3 VNC Viewer

Stream a remote Linux desktop (via VNC) to a jailbroken Apple TV 3 running Kodi, using an MJPEG relay server.

## Device Details

| Property | Value |
|----------|-------|
| Model | AppleTV3,2 (J33iAP) — 2013 Rev A, A5 chip (ARMv7) |
| OS | iPhone OS 8.4.4 (Build 12H1006, final firmware) |
| Kernel | Darwin 14.0.0 (xnu-2784.40.6) |
| RAM | 512 MB |
| Storage | ~8 GB (1.4 GB rootfs + 6.1 GB /private/var) |
| Display | 1080p via HDMI (Kodi skin renders at 720p) |
| Jailbreak | Yes, with Cydia + MobileSubstrate |
| SSH | port 22 |
| Kodi | 14.2 "Helix" (installed as Kodi.frappliance) |

## Architecture

```
┌───────────────────────────────────┐
│  LXC Host (192.168.50.180)        │
│                                   │
│  ┌─────────────┐  VNC (localhost) │         MJPEG stream          ┌───────────────────┐
│  │  x11vnc     │ ───────────────▶ │  ──────────────────────────▶  │  Apple TV 3       │
│  │  :5900      │                  │  http://192.168.50.180:8888   │  (Kodi 14.2)      │
│  └─────────────┘  ┌─────────────┐ │  /stream                     │  192.168.50.138    │
│                   │  Docker      │ │                              └───────────────────┘
│  ┌─────────────┐  │  mjpeg-vnc- │ │                                       │
│  │  xrdp       │  │  viewer     │ │                                       │  HDMI
│  │  :3389      │  │  :8888      │ │                                       ▼
│  └─────────────┘  └─────────────┘ │                                 ┌──────────┐
└───────────────────────────────────┘                                 │    TV    │
                                                                      └──────────┘
```

1. **x11vnc** mirrors the xrdp desktop session on the LXC container.
2. **mjpeg-vnc-viewer** (Docker container) captures VNC frames via asyncvnc, resizes to 720p, encodes as JPEG, and serves an MJPEG stream.
3. **Kodi** on the Apple TV opens the MJPEG stream URL and displays it fullscreen.

## Network

| Host | IP | Ports |
|------|----|-------|
| LXC host (Proxmox) | 192.168.50.180 | SSH: 22, VNC: 25900, RDP: 23389, MJPEG: 8888 |
| LXC container | (via port forwarding) | SSH: 20022, VNC: 5900, RDP: 3389 |
| Apple TV 3 | 192.168.50.138 | SSH: 22, Kodi HTTP: 8080 |

## Prerequisites

### Apple TV
- Jailbroken Apple TV 3 with SSH (OpenSSH) and Kodi installed
- Kodi web server enabled in `/private/var/mobile/Library/Preferences/Kodi/userdata/guisettings.xml`:
  ```xml
  <webserver>true</webserver>
  <webserverport>8080</webserverport>
  ```
- Restart Kodi after changing the setting

### LXC Host
- Docker and Docker Compose installed
- x11vnc running with `-nocursorshape -nocursorpos` flags (auto-started via xrdp's `startwm.sh`)

### Mac (for management only)
- `sshpass` (for scripted SSH: `brew install hudochenkov/sshpass/sshpass`)

## Setup

### Deploy the MJPEG server

The server runs as a Docker container on the LXC host. Project files are at `~/mjpeg-vnc-viewer/`.

```bash
# Copy project to LXC host (from Mac)
scp -r /Users/evnchn/mjpeg-vnc-viewer evnchn@192.168.50.180:~/

# SSH to host and start
ssh evnchn@192.168.50.180
cd ~/mjpeg-vnc-viewer
docker compose up --build -d
```

Configuration is in `.env`:
```env
VNC_HOST=127.0.0.1
VNC_PORT=25900
WIDTH=1280
HEIGHT=720
MIN_FPS=3
JPEG_QUALITY=80
PORT=8888
```

### x11vnc auto-start

x11vnc is auto-started in `/etc/xrdp/startwm.sh` on the LXC container (192.168.50.180:20022):
```sh
pkill -u "$(whoami)" x11vnc 2>/dev/null
sleep 1
x11vnc -display "$DISPLAY" -rfbport 5900 -nopw -shared -forever \
  -nocursorshape -nocursorpos \
  -bg -o /tmp/x11vnc.log 2>/dev/null
```

The `-nocursorshape -nocursorpos` flags are required — without them, x11vnc sends CursorWithAlpha encoding (0x600) which asyncvnc cannot parse.

## Usage

### Play the stream on Kodi

**Option A — From the remote (no computer needed):**

The stream is saved as a Kodi Favourite at:
`/private/var/mobile/Library/Preferences/Kodi/userdata/favourites.xml`

Navigate to **Favourites** on the Kodi home screen and select **"Billboard"**.

To update the favourite's URL:
```bash
sshpass -p "$ATV_PASSWORD" ssh \
  -oHostKeyAlgorithms=+ssh-rsa,ssh-dss \
  -oPubkeyAcceptedAlgorithms=+ssh-rsa,ssh-dss \
  -oStrictHostKeyChecking=no \
  root@192.168.50.138 \
  "sed -i 's|http://[^)]*|http://192.168.50.180:8888/stream|' \
  /private/var/mobile/Library/Preferences/Kodi/userdata/favourites.xml"
```

**Option B — Via JSON-RPC from another machine:**

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"Player.Open","params":{"item":{"file":"http://192.168.50.180:8888/stream"}},"id":1}' \
  http://192.168.50.138:8080/jsonrpc
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web dashboard with preview |
| `/stream` | GET | MJPEG stream (for Kodi) |
| `/snapshot` | GET | Single JPEG frame |
| `/status` | GET | JSON status (VNC connection, frame availability) |

## SSH Quick Reference

```bash
# Apple TV
ssh -oHostKeyAlgorithms=+ssh-rsa,ssh-dss \
    -oPubkeyAcceptedAlgorithms=+ssh-rsa,ssh-dss \
    root@192.168.50.138

# LXC host
ssh evnchn@192.168.50.180

# LXC container (via port forward)
ssh -p 20022 evnchn@192.168.50.180
```

## Kodi JSON-RPC Cheatsheet

```bash
ATV=http://192.168.50.138:8080/jsonrpc

# Play a stream
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"Player.Open","params":{"item":{"file":"http://192.168.50.180:8888/stream"}},"id":1}' $ATV

# Stop playback
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"Player.Stop","params":{"playerid":2},"id":1}' $ATV

# Get active players
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"Player.GetActivePlayers","id":1}' $ATV
```

## Docker Management

```bash
# On LXC host (192.168.50.180)
cd ~/mjpeg-vnc-viewer

docker compose logs -f          # Follow logs
docker compose restart           # Restart
docker compose down              # Stop
docker compose up --build -d     # Rebuild and start
```

## Technical Notes

- **VNC library**: asyncvnc (async Python VNC client). Only supports Raw and ZLib encodings. Works with x11vnc but not TigerVNC (black screen).
- **FPS**: VNC capture runs at ~8-9 FPS (limited by `client.read()` at ~100ms). MJPEG emission is event-driven — frames are only sent when VNC has a new frame, with a MIN_FPS keepalive to prevent Kodi buffering.
- **Resize**: VNC native resolution is resized to 1280x720 (BILINEAR) before JPEG encoding. Kodi on Apple TV 3 cannot handle non-720p resolutions.
- **Overlay**: Bottom-left shows reconnection status when VNC disconnects. Bottom-right has two 4x4 flashing indicators: cyan (MJPEG frame) and magenta (VNC frame).
- **Fonts**: Uses DejaVu Sans in Docker, macOS Helvetica when running locally. Falls back to Pillow's default bitmap font.
- **`asyncio.wait_for()` must NOT be used on VNC reads** — cancelling mid-read corrupts the TCP stream. The server relies on TCP close for disconnect detection.
- **Apple TV UI**: The Apple TV 3 uses FrontRow/BackRow (not SpringBoard). Only `.frappliance` bundles appear on the home screen.
- **Kodi Python**: Kodi 14.2 ships its own Python 2.6 for add-ons. Not used here — all processing runs server-side.

## Files

- `mjpeg_vnc_viewer.py` — The FastAPI MJPEG VNC viewer server (in `~/mjpeg-vnc-viewer/` on LXC host)
- `README.md` — This document
