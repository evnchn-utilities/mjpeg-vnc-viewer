"""A second MJPEG client must not miss a frame that a first client already consumed."""

import asyncio
import importlib
import os
import sys
import time

os.environ["MIN_FPS"] = "1"  # keepalive = 1 s, so a missed frame is a visible 1 s stall
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
mod = importlib.import_module("mjpeg_vnc_viewer")


async def _two_clients_one_frame():
    async with mod.lifespan(mod.app):
        a, b = mod.mjpeg_generator(), mod.mjpeg_generator()
        await b.__anext__()  # b takes the keepalive frame, then sits suspended at its yield
        ta = asyncio.create_task(a.__anext__())
        await asyncio.sleep(0.01)  # a is now waiting for a new frame
        mod.any_new_frame.set()  # capture task publishes one frame
        await ta  # a wakes and consumes it
        t0 = time.perf_counter()
        await b.__anext__()  # b must see that same frame straight away
        elapsed = time.perf_counter() - t0
        await a.aclose()
        await b.aclose()
        return elapsed


def test_second_client_gets_frame_without_keepalive_stall():
    elapsed = asyncio.run(_two_clients_one_frame())
    assert elapsed < 0.5, f"client b waited {elapsed:.2f}s: it missed the frame a consumed"
