# Apple TV 3 VNC Viewer — 2×2 Grid

Stream up to 4 remote Linux desktops (via VNC) to a jailbroken Apple TV 3 running Kodi, composited into a single 2×2 MJPEG grid.

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
  ┌──────────────┐
  │  VNC Host 1  │──┐
  └──────────────┘  │
  ┌──────────────┐  │   VNC        ┌───────────────────┐    MJPEG     ┌───────────────────┐
  │  VNC Host 2  │──┼─────────────▶│  Docker            │────────────▶│  Apple TV 3       │
  └──────────────┘  │  :5900 each  │  mjpeg-vnc-viewer  │  /stream    │  (Kodi 14.2)      │
  ┌──────────────┐  │              │  :8888              │             │  192.168.50.138    │
  │  VNC Host 3  │──┤              │                     │             └───────────────────┘
  └──────────────┘  │              │  Composites up to   │                      │
  ┌──────────────┐  │              │  4 streams into a   │                      │  HDMI
  │  VNC Host 4  │──┘              │  2×2 grid           │                      ▼
  └──────────────┘                 └───────────────────┘                 ┌──────────┐
                                                                        │    TV    │
                                                                        └──────────┘
```

1. **VNC servers** (x11vnc, etc.) run on up to 4 machines across the network.
2. **mjpeg-vnc-viewer** (Docker container) connects to each VNC target, captures frames, resizes each to a quarter of the output resolution, composites them into a 2×2 grid, and serves a single MJPEG stream.
3. **Kodi** on the Apple TV opens the MJPEG stream URL and displays the grid fullscreen.

## Network

| Host | IP | Ports |
|------|----|-------|
| MJPEG server (N5095) | 192.168.50.180 | SSH: 22, MJPEG: 8888 |
| VNC target: RTX3060 | 192.168.50.153 | VNC: 5900 |
| VNC target: Downstream | 192.168.50.156 | VNC: 5900 |
| VNC target: N5095 | 192.168.50.180 | VNC: 5900 |
| VNC target: Colorful | 192.168.50.141 | VNC: 5900 |
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

Configuration is in `.env` (see `.env.example`):
```env
VNC_TARGETS=192.168.50.153:5900:RTX3060:yourpassword,192.168.50.156:5900:Downstream:yourpassword,192.168.50.180:5900:N5095:yourpassword,192.168.50.141:5900:Colorful:yourpassword
VNC_PASSWORD=            # default password (overridden per-target above)
WIDTH=1920
HEIGHT=1080
MIN_FPS=3
JPEG_QUALITY=80
PORT=8888
```

`VNC_TARGETS` is a comma-separated list of `host:port:label:password` entries. Port, label, and password are optional (defaults: 5900, hostname, `VNC_PASSWORD`). Empty entries become blank slots. Up to 4 targets are displayed in the 2×2 grid.

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
- **Multi-stream**: Each VNC target runs as an independent async capture task with its own reconnection loop. Frames are composited into a 2×2 grid on every MJPEG emission.
- **FPS**: VNC capture runs at ~8-9 FPS per stream (limited by `client.read()` at ~100ms). MJPEG emission is event-driven — a frame is sent whenever any VNC stream updates, with a MIN_FPS keepalive to prevent Kodi buffering.
- **Resize**: Each VNC stream is resized to cell size (WIDTH/2 × HEIGHT/2, BILINEAR) before compositing.
- **Overlay**: Each cell shows a label bar with a green/red connection status dot. Disconnected cells show a red "Reconnecting" message. Bottom-right of the full canvas has a cyan flashing indicator for MJPEG frames.
- **Fonts**: Uses DejaVu Sans / DejaVu Sans Bold in Docker, macOS Helvetica when running locally. Falls back to Pillow's default bitmap font.
- **`asyncio.wait_for()` must NOT be used on VNC reads** — cancelling mid-read corrupts the TCP stream. The server relies on TCP close for disconnect detection.
- **Memory limit**: Docker container is capped at 256 MB (`mem_limit` in docker-compose.yml).
- **Apple TV UI**: The Apple TV 3 uses FrontRow/BackRow (not SpringBoard). Only `.frappliance` bundles appear on the home screen.
- **Kodi Python**: Kodi 14.2 ships its own Python 2.6 for add-ons. Not used here — all processing runs server-side.

## Files

- `mjpeg_vnc_viewer.py` — The FastAPI MJPEG VNC viewer server
- `docker-compose.yml` — Docker Compose service definition (with 256 MB memory limit)
- `Dockerfile` — Python 3.12-slim image with DejaVu fonts
- `.env.example` — Example environment configuration
- `requirements.txt` — Python dependencies (asyncvnc, FastAPI, Pillow, etc.)
- `README.md` — This document
