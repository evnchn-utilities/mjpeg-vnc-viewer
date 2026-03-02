#!/usr/bin/env python3
"""MJPEG VNC viewer — multi-stream 2×2 grid for Kodi on Apple TV 3.

Grabs frames from up to 4 VNC servers (via asyncvnc) and composites them
into a single 2×2 MJPEG stream.  Empty slots show a dark placeholder.
"""

import asyncio
import io
import os
import time
from contextlib import asynccontextmanager

import asyncvnc
import numpy as np
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from PIL import Image, ImageDraw, ImageFont

# ── Global config ──────────────────────────────────────────────────────
WIDTH = int(os.environ.get("WIDTH", 1280))
HEIGHT = int(os.environ.get("HEIGHT", 720))
MIN_FPS = int(os.environ.get("MIN_FPS", 3))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", 80))
PORT = int(os.environ.get("PORT", 8888))

MAX_STREAMS = 4
COLS, ROWS = 2, 2
CELL_W = WIDTH // COLS
CELL_H = HEIGHT // ROWS

# Parse VNC_TARGETS: comma-separated "host:port:label:password" (label/password optional)
# Example: "192.168.50.153:5900:RTX3060:secret,192.168.50.156:5900:Downstream:secret,"
VNC_PASSWORD = os.environ.get("VNC_PASSWORD", "")  # default password for all targets
_raw_targets = os.environ.get("VNC_TARGETS", "")
VNC_TARGETS: list[dict | None] = []
for entry in _raw_targets.split(","):
    entry = entry.strip()
    if not entry:
        VNC_TARGETS.append(None)
        continue
    parts = entry.split(":")
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else 5900
    label = parts[2] if len(parts) > 2 else host
    password = parts[3] if len(parts) > 3 else VNC_PASSWORD
    VNC_TARGETS.append({"host": host, "port": port, "label": label, "password": password})
# Pad to MAX_STREAMS
while len(VNC_TARGETS) < MAX_STREAMS:
    VNC_TARGETS.append(None)
VNC_TARGETS = VNC_TARGETS[:MAX_STREAMS]

# Fonts
try:
    FONT_OVL = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    FONT_LABEL = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
except Exception:
    try:
        FONT_OVL = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
        FONT_LABEL = FONT_OVL
    except Exception:
        FONT_OVL = ImageFont.load_default()
        FONT_LABEL = FONT_OVL

# ── Per-stream state ──────────────────────────────────────────────────
vnc_images: list[Image.Image | None] = [None] * MAX_STREAMS
vnc_connected: list[bool] = [False] * MAX_STREAMS
vnc_counters: list[int] = [0] * MAX_STREAMS
vnc_new_frames: list[asyncio.Event | None] = [None] * MAX_STREAMS
any_new_frame: asyncio.Event | None = None

mjpeg_counter = 0


# ── Frame rendering ──────────────────────────────────────────────────
def get_frame() -> bytes:
    """Composite the 2×2 grid and encode as JPEG."""
    global mjpeg_counter
    mjpeg_counter += 1

    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    for idx in range(MAX_STREAMS):
        col = idx % COLS
        row = idx // COLS
        x0 = col * CELL_W
        y0 = row * CELL_H

        target = VNC_TARGETS[idx]

        if target is None:
            # Empty slot — dark background with label
            draw.rectangle([x0, y0, x0 + CELL_W - 1, y0 + CELL_H - 1],
                           fill=(20, 20, 20))
            draw.text((x0 + 8, y0 + 8), "Empty", fill=(60, 60, 60), font=FONT_OVL)
        elif vnc_images[idx] is not None:
            # Paste the captured frame
            canvas.paste(vnc_images[idx], (x0, y0))
        else:
            # Target configured but no frame yet
            draw.rectangle([x0, y0, x0 + CELL_W - 1, y0 + CELL_H - 1],
                           fill=(10, 10, 10))

        # Draw label bar at bottom of each cell (if target configured)
        if target is not None:
            label = target["label"]
            bar_h = 22
            bar_y = y0 + CELL_H - bar_h
            draw.rectangle([x0, bar_y, x0 + CELL_W - 1, bar_y + bar_h - 1],
                           fill=(0, 0, 0, 180))
            # Connection status dot
            dot_color = (0, 200, 0) if vnc_connected[idx] else (200, 0, 0)
            draw.ellipse([x0 + 6, bar_y + 5, x0 + 16, bar_y + 15], fill=dot_color)
            # Label text
            draw.text((x0 + 22, bar_y + 3), label, fill=(220, 220, 220), font=FONT_LABEL)

            # Reconnecting overlay
            if not vnc_connected[idx]:
                msg = f"Reconnecting to {target['host']}:{target['port']}..."
                # Center the message in the cell
                draw.text((x0 + 8, y0 + CELL_H // 2 - 10), msg,
                          fill=(255, 80, 80), font=FONT_OVL)

    # Grid lines (1px gray)
    draw.line([(WIDTH // 2, 0), (WIDTH // 2, HEIGHT)], fill=(50, 50, 50), width=1)
    draw.line([(0, HEIGHT // 2), (WIDTH, HEIGHT // 2)], fill=(50, 50, 50), width=1)

    # Flashing indicators (bottom-right of full canvas)
    if mjpeg_counter % 2 == 0:
        draw.rectangle([WIDTH - 8, HEIGHT - 8, WIDTH - 4, HEIGHT - 4],
                       fill=(0, 128, 128))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


# ── VNC capture ──────────────────────────────────────────────────────
def _process_vnc_frame(rgba: np.ndarray) -> Image.Image:
    """CPU-heavy work: numpy→PIL→resize to cell size. Runs in a thread."""
    img = Image.fromarray(rgba[:, :, :3], "RGB")
    if img.size != (CELL_W, CELL_H):
        img = img.resize((CELL_W, CELL_H), Image.BILINEAR)
    return img


async def vnc_capture_task(idx: int):
    """Continuously capture from one VNC target."""
    target = VNC_TARGETS[idx]
    if target is None:
        return

    host = target["host"]
    port = target["port"]
    label = target["label"]
    password = target.get("password", "")
    retry_delay = 3

    while True:
        try:
            print(f"[VNC-{idx}] Connecting to {host}:{port} ({label})...", flush=True)
            async with asyncvnc.connect(host, port, password=password or None) as client:
                vnc_connected[idx] = True
                print(f"[VNC-{idx}] Connected! Desktop: "
                      f"{client.video.width}x{client.video.height}")

                client.video.refresh()
                frame_n = 0
                while True:
                    update = await client.read()
                    if update is asyncvnc.UpdateType.VIDEO:
                        rgba = client.video.as_rgba()
                        img = await asyncio.to_thread(_process_vnc_frame, rgba)
                        vnc_images[idx] = img
                        vnc_counters[idx] += 1
                        frame_n += 1
                        vnc_new_frames[idx].set()
                        any_new_frame.set()

                        if frame_n % 50 == 0:
                            print(f"[VNC-{idx}] {label}: {frame_n} frames captured",
                                  flush=True)

                    client.video.refresh()
        except Exception as e:
            print(f"[VNC-{idx}] {label}: {type(e).__name__}: {e}")
        vnc_connected[idx] = False
        print(f"[VNC-{idx}] {label}: Disconnected. Retrying in {retry_delay}s...")
        await asyncio.sleep(retry_delay)


# ── FastAPI app ──────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global any_new_frame
    any_new_frame = asyncio.Event()
    tasks = []
    for i in range(MAX_STREAMS):
        vnc_new_frames[i] = asyncio.Event()
        if VNC_TARGETS[i] is not None:
            tasks.append(asyncio.create_task(vnc_capture_task(i)))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


async def mjpeg_generator():
    """Emit a composited frame on every VNC update, or at MIN_FPS keepalive."""
    keepalive_interval = 1.0 / MIN_FPS
    last_fps_time = time.perf_counter()
    n = 0
    while True:
        try:
            await asyncio.wait_for(any_new_frame.wait(), timeout=keepalive_interval)
            any_new_frame.clear()
        except asyncio.TimeoutError:
            pass

        frame = get_frame()
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
            + frame + b"\r\n"
        )

        n += 1
        if n % 100 == 0:
            now = time.perf_counter()
            actual_fps = 100 / (now - last_fps_time)
            connected = sum(1 for c in vnc_connected if c)
            print(f"[STRM] FPS={actual_fps:.1f}  connected={connected}/{sum(1 for t in VNC_TARGETS if t)}",
                  flush=True)
            last_fps_time = now


@app.get("/stream")
async def stream():
    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/snapshot")
async def snapshot():
    return Response(content=get_frame(), media_type="image/jpeg")


@app.get("/status")
async def status():
    result = []
    for i, target in enumerate(VNC_TARGETS):
        if target is None:
            result.append({"slot": i, "configured": False})
        else:
            result.append({
                "slot": i,
                "configured": True,
                "label": target["label"],
                "vnc_target": f"{target['host']}:{target['port']}",
                "connected": vnc_connected[i],
                "has_frame": vnc_images[i] is not None,
                "frames": vnc_counters[i],
            })
    return {"streams": result}


@app.get("/", response_class=HTMLResponse)
async def index():
    rows = ""
    for i, target in enumerate(VNC_TARGETS):
        if target is None:
            rows += f"<tr><td>{i}</td><td>—</td><td>Empty</td></tr>"
        else:
            rows += (f"<tr><td>{i}</td><td>{target['label']}</td>"
                     f"<td>{target['host']}:{target['port']}</td></tr>")
    return f"""<html><body style="background:#000;color:white;font-family:sans-serif;text-align:center;padding:20px">
<h1>MJPEG VNC Viewer — 2×2 Grid</h1>
<p><a href="/stream" style="color:#fdc">Stream (MJPEG)</a> |
   <a href="/snapshot" style="color:#fdc">Snapshot (JPEG)</a> |
   <a href="/status" style="color:#fdc">Status (JSON)</a></p>
<table style="margin:10px auto;color:#ccc;border-collapse:collapse">
<tr style="color:#f80"><th style="padding:4px 12px">Slot</th><th style="padding:4px 12px">Label</th><th style="padding:4px 12px">Target</th></tr>
{rows}
</table>
<hr><img src="/snapshot" style="max-width:100%">
</body></html>"""


if __name__ == "__main__":
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"MJPEG VNC viewer (2×2 grid) starting on port {PORT}")
    for i, t in enumerate(VNC_TARGETS):
        if t:
            print(f"  Slot {i}: {t['label']} → {t['host']}:{t['port']}")
        else:
            print(f"  Slot {i}: (empty)")
    print(f"  Stream:   http://{local_ip}:{PORT}/stream")
    print(f"  Snapshot: http://{local_ip}:{PORT}/snapshot")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
