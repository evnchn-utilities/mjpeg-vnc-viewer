"""VNC_TARGETS parsing: a password may itself contain ':'.

Run: python3 -m pytest tests/ -q   (needs the requirements.txt deps)
"""
import importlib
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def load_targets(spec):
    os.environ["VNC_TARGETS"] = spec
    os.environ.pop("VNC_PASSWORD", None)
    sys.path.insert(0, ROOT)
    try:
        sys.modules.pop("mjpeg_vnc_viewer", None)
        return importlib.import_module("mjpeg_vnc_viewer").VNC_TARGETS
    finally:
        sys.path.remove(ROOT)


def test_password_keeps_colons():
    t = load_targets("10.0.0.1:5900:lab:pa:ss:wd")[0]
    assert t == {"host": "10.0.0.1", "port": 5900, "label": "lab", "password": "pa:ss:wd"}


def test_plain_four_field_entry_unchanged():
    t = load_targets("10.0.0.1:5901:lab:secret")[0]
    assert (t["port"], t["label"], t["password"]) == (5901, "lab", "secret")


def test_missing_password_falls_back_to_default():
    t = load_targets("10.0.0.1:5900:lab")[0]
    assert t["password"] == ""
