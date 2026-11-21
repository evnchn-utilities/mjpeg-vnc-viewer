"""A VNC host that goes silent (power cut, no TCP RST) must not stay "connected" forever.

An idle-but-alive host must stay connected: it ignores incremental refresh
requests while nothing changes, but answers a full (non-incremental) one.
"""

import asyncio
import importlib
import os
import sys

os.environ["VNC_READ_TIMEOUT"] = "0.3"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.pop("mjpeg_vnc_viewer", None)
mod = importlib.import_module("mjpeg_vnc_viewer")

W = H = 4


async def _fake_rfb(reader, writer, answer_full_refresh):
    writer.write(b"RFB 003.008\n")
    await reader.readexactly(12)
    writer.write(b"\x01\x01")  # one auth type: None
    await reader.readexactly(1)
    writer.write(b"\x00\x00\x00\x00")  # auth ok
    await reader.readexactly(1)  # ClientInit
    pixfmt = bytes([32, 24, 0, 1, 0, 255, 0, 255, 0, 255, 16, 8, 0]) + b"\x00\x00\x00"
    writer.write(W.to_bytes(2, "big") + H.to_bytes(2, "big") + pixfmt + b"\x00\x00\x00\x01x")
    try:
        while True:
            msg = (await reader.readexactly(1))[0]
            if msg == 0:  # SetPixelFormat
                await reader.readexactly(19)
            elif msg == 2:  # SetEncodings
                await reader.readexactly(1)
                n = int.from_bytes(await reader.readexactly(2), "big")
                await reader.readexactly(4 * n)
            elif msg == 3:  # FramebufferUpdateRequest
                incremental = (await reader.readexactly(9))[0]
                if answer_full_refresh and not incremental:
                    writer.write(b"\x00\x00\x00\x01" + b"\x00" * 4
                                 + W.to_bytes(2, "big") + H.to_bytes(2, "big")
                                 + b"\x00\x00\x00\x00" + b"\x00" * (W * H * 4))
            else:
                return
    except (asyncio.IncompleteReadError, ConnectionError):
        return


async def _connected_after(answer_full_refresh, wait):
    server = await asyncio.start_server(
        lambda r, w: _fake_rfb(r, w, answer_full_refresh), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    mod.VNC_TARGETS[0] = {"host": "127.0.0.1", "port": port, "label": "t", "password": ""}
    mod.vnc_new_frames[0] = asyncio.Event()
    mod.vnc_connected[0] = False
    task = asyncio.create_task(mod.vnc_capture_task(0))
    try:
        for _ in range(100):  # wait for the first connect
            if mod.vnc_connected[0]:
                break
            await asyncio.sleep(0.02)
        assert mod.vnc_connected[0], "never connected to the fake server"
        await asyncio.sleep(wait)
        return mod.vnc_connected[0]
    finally:
        task.cancel()
        server.close()


def test_silent_host_is_marked_disconnected():
    assert not asyncio.run(_connected_after(answer_full_refresh=False, wait=1.0))


def test_idle_host_that_answers_full_refresh_stays_connected():
    assert asyncio.run(_connected_after(answer_full_refresh=True, wait=1.5))
