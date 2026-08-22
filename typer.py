#!/usr/bin/env python3
"""Push-to-talk typing bridge (run as sudo).

HOLD F14 on any physical keyboard -> tells listen.py to start recording.
RELEASE F14 -> tells it to stop; listen.py transcribes and publishes the
text, which this program types via a virtual (uinput) keyboard.

Hold-to-talk: F14 down = start, F14 up = stop (not a toggle).

Needs root: opens /dev/uinput and reads /dev/input/eventN.
    sudo .venv/bin/python typer.py        # with listen.py already running

Note: the virtual keystrokes go to whatever window has keyboard focus.
Wayland apps with secure input (password fields, browsers) will ignore them.
"""
import argparse
import os
import select
import string
import time

import evdev
from evdev import ecodes as e
import zmq

CMD_URL = "tcp://127.0.0.1:5561"   # must match listen.py: CMD_PORT
OUT_URL = "tcp://127.0.0.1:5562"   # must match listen.py: OUT_PORT
PTT = e.KEY_F14                    # push-to-talk key
HOLD = 0.03      # s each key is held: some apps drop zero-length taps (first/last chars)
GAP = 0.06       # s between keys
WARMUP = 0.30    # s before the first key: lets an idle focused window wake up
TAIL = 0.10      # s after the last keyup

# US-layout char -> keycode (plus which keys need shift to type them)
def _key(*names):
    for n in names:
        v = getattr(e, n)
        if v is not None:
            return v
    raise AttributeError(f"none of {names} exist in evdev.ecodes")

MAP = {
    " ": _key("KEY_SPACE"), "\n": _key("KEY_ENTER"), "\t": _key("KEY_TAB"),
    "\x7f": _key("KEY_BACKSPACE"),
    ",": _key("KEY_COMMA"), ".": _key("KEY_DOT"), "/": _key("KEY_SLASH"),
    ";": _key("KEY_SEMICOLON"), "'": _key("KEY_APOSTROPHE"),
}
SHIFTED = {  # char -> unshifted key (pressing with shift)
    "!": _key("KEY_1"), "@": _key("KEY_2"), "#": _key("KEY_3"),
    "$": _key("KEY_4"), "%": _key("KEY_5"), "^": _key("KEY_6"),
    "&": _key("KEY_7"), "*": _key("KEY_8"), "(": _key("KEY_9"),
    ")": _key("KEY_0"), "_": _key("KEY_MINUS"), "+": _key("KEY_EQUAL"),
    "{": _key("KEY_LEFTBRACE", "KEY_BRACELEFT"),
    "}": _key("KEY_RIGHTBRACE", "KEY_BRACERIGHT"),
    "|": _key("KEY_BACKSLASH"), ":": _key("KEY_SEMICOLON"),
    '"': _key("KEY_APOSTROPHE"), "<": _key("KEY_COMMA"),
    ">": _key("KEY_DOT"), "?": _key("KEY_SLASH"),
    "~": _key("KEY_GRAVE"),
}
for ch, code in zip(string.ascii_lowercase, string.ascii_uppercase):
    MAP[ch] = _key(f"KEY_{code}")
    SHIFTED[ch.upper()] = _key(f"KEY_{code}")   # uppercase = lowercase key + shift
for d in "0123456789":
    MAP[d] = _key(f"KEY_{d}")

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--text", default="talk-typer: typing works",
                metavar="TEXT", help="text to type once on startup, to verify (default: built-in sentence)")
ap.add_argument("--no-test", action="store_true", help="skip the startup test typing")
args = ap.parse_args()

if os.geteuid() != 0:
    raise SystemExit("run as sudo (needs /dev/uinput and /dev/input access)")

# --- virtual keyboard -------------------------------------------------------
# Register an explicit full key set. (Mirroring an arbitrary physical device
# is fragile: if the first device in /dev/input is a headset/mouse with only
# a few keys, the kernel silently drops every key we don't register.)
all_keys = [c for c in range(1, 0x2E8)]          # KEY_ESC..KEY_MICMUTE
mouse_btns = [getattr(e, n) for n in
              ("BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE") if hasattr(e, n)]
kbd = evdev.UInput({e.EV_KEY: all_keys + mouse_btns}, name="talk-typer")
print(f"virtual keyboard: {kbd.devnode}  {kbd.name}")

# Watch every physical keycap for the PTT key (no grab(), so real keyboards
# keep working).
watchers = []
for path in evdev.list_devices():
    dev = evdev.InputDevice(path)              # as root, /dev/input is readable
    caps = dev.capabilities()
    if e.EV_KEY in caps and PTT in caps[e.EV_KEY]:
        watchers.append(dev)
        print(f"watching {dev.name} ({path})")
    else:
        dev.close()
if not watchers:
    raise SystemExit(f"no physical keyboard exposes {e.ecodes.get(PTT, PTT)}; "
                     "check your keyboard (see `sudo evtest`)")

# --- zmq --------------------------------------------------------------------
ctx = zmq.Context()
cmd_pub = ctx.socket(zmq.PUB)
cmd_pub.connect(CMD_URL)
res_sub = ctx.socket(zmq.SUB)
res_sub.setsockopt(zmq.SUBSCRIBE, b"")
res_sub.connect(OUT_URL)


def press(code, hold=HOLD):
    kbd.write(e.EV_KEY, code, 1); kbd.syn()
    time.sleep(hold)
    kbd.write(e.EV_KEY, code, 0); kbd.syn()


def tap(code):
    """Inert modifier tap: primes/drains the receiver's input pipeline, whose
    edges can otherwise swallow the real first/last key of a burst."""
    press(code, 0.02)


def type_text(text):
    print(f"typing: {text!r}")
    time.sleep(WARMUP)
    tap(e.KEY_LEFTSHIFT)      # primer before the first real key
    time.sleep(GAP)
    for ch in text:
        code = MAP.get(ch)
        if code is None and ch in SHIFTED:
            code, is_shift = SHIFTED[ch], True
        else:
            is_shift = False
        if code is None:
            continue
        if is_shift:
            kbd.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1); kbd.syn()
        press(code)
        if is_shift:
            kbd.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0); kbd.syn()
        time.sleep(GAP)
    tap(e.KEY_LEFTSHIFT)      # flusher after the last real key
    time.sleep(TAIL)


held = False
if args.text and not args.no_test:
    print(f"typing test text — focus your target window first: {args.text!r}")
    type_text(args.text)
print("ready — hold F14 to talk (Ctrl-C to quit)")
while True:
    watch_fds = {d.fileno(): d for d in watchers}
    res_fd = res_sub.fileno()
    try:
        r, _, _ = select.select(list(watch_fds) + [res_fd], [], [])
    except KeyboardInterrupt:
        print("\nbye")
        break
    except (OSError, zmq.ZMQError) as ex:
        print(f"\ndevice/zmq error: {ex} — quitting (restart the keyboard)")
        break
    for fd in r:
        if fd in watch_fds:
            for ev in watch_fds[fd].read():
                if not (ev.type == e.EV_KEY and ev.code == PTT and ev.value in (0, 1)):
                    continue
                if ev.value and not held:
                    held = True
                    cmd_pub.send(b"start")
                    print("F14 down — recording (release to stop)")
                elif not ev.value and held:
                    held = False
                    cmd_pub.send(b"stop")
                    print("F14 up — waiting for transcription...")
        if fd == res_fd:
            while res_sub.poll(0):
                parts = res_sub.recv_multipart()
                if parts and parts[0] == b"result":
                    type_text(parts[1].decode())
                    print("done")
