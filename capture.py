#!/usr/bin/env python3
"""Log what actually arrives at the talk-typer virtual keyboard.

Run as sudo, then trigger a typing burst (e.g. `sudo .venv/bin/python typer.py
--text 'this is a test'`, or an F14 session). Every EV_KEY the kernel receives
is printed with its code and press(1)/release(0) value — so if the first/last
key is missing HERE, typer dropped it on the way in; if it's present here but
missing in the target app, the app (or compositor) is swallowing it.

    sudo .venv/bin/python capture.py            # watches for ~12s
"""
import select
import time

import evdev
from evdev import ecodes as e

CODE2NAME = {v: k for k, v in e.ecodes.items()}

dev = None
for path in evdev.list_devices():
    d = evdev.InputDevice(path)
    if d.name == "talk-typer":
        dev, path = d, path
        break
    d.close()
if dev is None:
    raise SystemExit("talk-typer not found — start typer.py first")

print(f"capturing {path} ({dev.name}) — trigger the typing now, ~12 s window", flush=True)
t0 = time.time()
while time.time() - t0 < 12:
    if not select.select([dev.fileno()], [], [], 0.2)[0]:
        continue
    for ev in dev.read():
        if ev.type != e.EV_KEY:
            continue
        name = CODE2NAME.get(ev.code, ev.code)
        print(f"{time.time() - t0:8.3f}s  {name:<18} {'down' if ev.value else 'up'}", flush=True)
