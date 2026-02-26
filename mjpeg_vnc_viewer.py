#!/usr/bin/env python3
"""MJPEG VNC viewer for Kodi on Apple TV 3.

Grabs frames from a VNC server (via asyncvnc) and re-serves them as MJPEG.
On disconnect, keeps showing the last desktop with a reconnection overlay.
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

WIDTH = int(os.environ.get("WIDTH", 1280))
HEIGHT = int(os.environ.get("HEIGHT", 720))
MIN_FPS = int(os.environ.get("MIN_FPS", 3))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", 80))
PORT = int(os.environ.get("PORT", 8888))

VNC_HOST = os.environ.get("VNC_HOST", "192.168.50.180")
VNC_PORT = int(os.environ.get("VNC_PORT", 25900))

# Try to load a nice font, fall back to default
try:
    FONT_OVL = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
except Exception:
    try:
        FONT_OVL = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 20)
    except Exception:
        FONT_OVL = ImageFont.load_default()

# Shared state — vnc_image is a PIL Image, updated from a thread
vnc_image: Image.Image | None = None
vnc_connected = False
mjpeg_counter = 0
vnc_counter = 0
vnc_new_frame: asyncio.Event | None = None  # set in lifespan, signalled by VNC capture


def get_frame() -> bytes:
    """Build the output JPEG: last desktop + overlays."""
    global mjpeg_counter
    mjpeg_counter += 1

    t0 = time.perf_counter()

    if vnc_image is not None:
        img = vnc_image.copy()
    else:
        img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))

    t_copy = time.perf_counter()

    draw = ImageDraw.Draw(img)

    # Bottom-left: reconnection message (only when disconnected)
    if not vnc_connected:
        msg = f"Reconnecting to {VNC_HOST}:{VNC_PORT}..."
        draw.text((8, HEIGHT - 28), msg, fill=(255, 80, 80), font=FONT_OVL)

    # Bottom-right: two 4x4 flashing indicators at ~50% opacity
    # Cyan = new MJPEG frame, Magenta = new VNC frame
    if mjpeg_counter % 2 == 0:
        draw.rectangle([WIDTH - 12, HEIGHT - 8, WIDTH - 8, HEIGHT - 4],
                        fill=(0, 128, 128))
    if vnc_counter % 2 == 0:
        draw.rectangle([WIDTH - 6, HEIGHT - 8, WIDTH - 2, HEIGHT - 4],
                        fill=(128, 0, 128))

    t_overlay = time.perf_counter()

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    jpeg_bytes = buf.getvalue()

    t_jpeg = time.perf_counter()

    if mjpeg_counter % 100 == 0:
        print(f"[MJPEG #{mjpeg_counter}] copy={1000*(t_copy-t0):.1f}ms "
              f"overlay={1000*(t_overlay-t_copy):.1f}ms "
              f"jpeg={1000*(t_jpeg-t_overlay):.1f}ms "
              f"total={1000*(t_jpeg-t0):.1f}ms "
              f"size={len(jpeg_bytes)//1024}KB", flush=True)

    return jpeg_bytes


def _process_vnc_frame(rgba: np.ndarray) -> Image.Image:
    """CPU-heavy work: numpy->PIL->resize. Runs in a thread."""
    img = Image.fromarray(rgba[:, :, :3], "RGB")
    if img.size != (WIDTH, HEIGHT):
        img = img.resize((WIDTH, HEIGHT), Image.Resampling.BILINEAR)
    return img


async def vnc_capture_task():
    """Async task: connect to VNC and continuously grab frames.

    CPU-heavy frame processing runs in a thread executor to keep the
    event loop responsive for MJPEG streaming.
    """
    global vnc_image, vnc_connected, vnc_counter
    retry_delay = 3

    while True:
        try:
            print(f"[VNC] Connecting to {VNC_HOST}:{VNC_PORT}...")
            async with asyncvnc.connect(VNC_HOST, VNC_PORT) as client:
                vnc_connected = True
                print(f"[VNC] Connected! Desktop: {client.video.name} "
                      f"({client.video.width}x{client.video.height}, mode={client.video.mode})")

                client.video.refresh()
                frame_n = 0
                while True:
                    t0 = time.perf_counter()
                    update = await client.read()
                    t_read = time.perf_counter()

                    if update is asyncvnc.UpdateType.VIDEO:
                        rgba = client.video.as_rgba()
                        t_rgba = time.perf_counter()

                        # Offload CPU-heavy work to thread
                        img = await asyncio.to_thread(_process_vnc_frame, rgba)
                        t_proc = time.perf_counter()

                        vnc_image = img
                        vnc_counter += 1
                        frame_n += 1
                        vnc_new_frame.set()

                        if frame_n % 20 == 0:
                            print(f"[CAP  #{frame_n}] read={1000*(t_read-t0):.1f}ms "
                                  f"as_rgba={1000*(t_rgba-t_read):.1f}ms "
                                  f"process={1000*(t_proc-t_rgba):.1f}ms "
                                  f"total={1000*(t_proc-t0):.1f}ms", flush=True)

                    client.video.refresh()
        except Exception as e:
            print(f"[VNC] Error: {type(e).__name__}: {e}")
        vnc_connected = False
        print(f"[VNC] Disconnected. Retrying in {retry_delay}s...")
        await asyncio.sleep(retry_delay)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global vnc_new_frame
    vnc_new_frame = asyncio.Event()
    task = asyncio.create_task(vnc_capture_task())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


async def mjpeg_generator():
    """Async generator: emit a frame on every VNC update, or at MIN_FPS keepalive."""
    keepalive_interval = 1.0 / MIN_FPS
    last_fps_time = time.perf_counter()
    n = 0
    while True:
        # Wait for a new VNC frame, or timeout for keepalive
        try:
            await asyncio.wait_for(vnc_new_frame.wait(), timeout=keepalive_interval)
            vnc_new_frame.clear()
        except asyncio.TimeoutError:
            pass  # No new VNC frame -- emit keepalive

        frame = get_frame()
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
            + frame + b"\r\n"
        )

        n += 1
        if n % 50 == 0:
            now = time.perf_counter()
            actual_fps = 50 / (now - last_fps_time)
            print(f"[STRM] actual MJPEG FPS={actual_fps:.1f} "
                  f"(VNC={vnc_counter} MJPEG={mjpeg_counter})", flush=True)
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
    return {
        "vnc_connected": vnc_connected,
        "vnc_target": f"{VNC_HOST}:{VNC_PORT}",
        "has_frame": vnc_image is not None,
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return """<html><body style="background:#000;color:white;font-family:sans-serif;text-align:center;padding:40px">
<h1>MJPEG VNC Viewer</h1>
<p><a href="/stream" style="color:#fdc">Stream (MJPEG)</a> |
   <a href="/snapshot" style="color:#fdc">Snapshot (JPEG)</a> |
   <a href="/status" style="color:#fdc">Status</a></p>
<hr><img src="/snapshot" style="max-width:100%">
</body></html>"""


if __name__ == "__main__":
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"MJPEG VNC viewer starting on port {PORT}")
    print(f"  VNC source: {VNC_HOST}:{VNC_PORT}")
    print(f"  Stream:     http://{local_ip}:{PORT}/stream")
    print(f"  Snapshot:   http://{local_ip}:{PORT}/snapshot")
    print(f"  Status:     http://{local_ip}:{PORT}/status")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
