#!/usr/bin/env python3
"""Push-to-talk: spacebar (or F14 via typer.py over ZMQ) to start, release to stop
-> Whisper prints the text (and publishes it to typer.py so it can type it).

Mic: hardcoded to the UMIK (MIC below); override by editing that constant.
ZMQ: always on; typer.py connects to CMD_PORT/OUT_PORT below.
"""
import argparse
import os
import select
import subprocess
import sys
import time
from contextlib import contextmanager

import zmq

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

WPSR = 16000  # whisper's rate
MIC = "umik"  # fuzzy device name
MAX_SECS = 600  # safety cap in case the stop keypress is missed
CMD_PORT = 5561  # typer -> listen commands (this process binds SUB)
OUT_PORT = 5562  # listen -> typer results (this process binds PUB)
# next to this file, so it works from any cwd (e.g. systemd units without WorkingDirectory)
WHISTLE_CACT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "whistle.cact")


@contextmanager
def quiet_terminal():
    """cbreak + no-echo for the whole session; restores on exit (even on Ctrl-C)."""
    import termios, tty
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except (OSError, termios.error):
        yield  # not a tty: nothing to change
        return
    tty.setcbreak(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _sink_mute(v):
    subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", v], check=False)


@contextmanager
def system_muted():
    """Mute system audio while recording, restore the previous state after."""
    out = subprocess.run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                         capture_output=True, text=True).stdout.split()
    prev = out[-1] if out else "0"   # "Mute: yes|no" -> yes/no
    _sink_mute("1")
    print(f"system audio muted (was: {prev})")
    try:
        yield
    finally:
        _sink_mute(prev)


def wait_start(cmd):
    """Drop stale messages, then block until a zmq 'start' or a spacebar arrives.

    Stale = sent while we were busy (recording/transcribing/typer typing),
    so they are ignored rather than replayed.
    """
    while cmd.poll(0):
        cmd.recv()
    while True:
        if cmd.poll(100) and cmd.recv() == b"start":
            return
        if select.select([sys.stdin], [], [], 0)[0]:
            if sys.stdin.read(1) == " ":
                return


def resample(x, src, dst):
    """3:1 average-pool downsample (48k -> 16k), good enough for speech."""
    r, rem = divmod(src, dst)
    if rem or len(x) < r:
        return x
    return np.pad(x, (0, (-len(x)) % r), mode="edge").reshape(-1, r).mean(axis=1)


def resolve_device(q):
    """q = None (system default), int/str index, or name substring."""
    if q is not None and not isinstance(q, int) and q.isdigit():
        q = int(q)
    if q is None or isinstance(q, int):
        return q
    hits = [d["index"] for d in sd.query_devices()
            if q.lower() in d["name"].lower() and d["max_input_channels"] > 0]
    if not hits:
        raise SystemExit(f"no input device matching {q!r}; run `python -m sounddevice`")
    hits.sort(key=lambda i: "alsa" not in sd.query_devices(i)["name"].lower())  # prefer direct ALSA over JACK/PipeWire mirrors
    return hits[0]


def record(max_secs=MAX_SECS, cmd=None):
    device = resolve_device(MIC)
    sr = int(sd.query_devices(device, "input")["default_samplerate"])
    block = max(64, sr // 30)
    chunks = []
    with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                        blocksize=block, device=device) as s:
        print("recording... (release F14 / press space to stop)")
        t0 = time.time()
        while time.time() - t0 < max_secs:
            chunks.append(s.read(block)[0][:, 0].copy())
            if cmd is not None and cmd.poll(0):
                stops = False
                while cmd.poll(0):
                    stops = stops or cmd.recv() == b"stop"
                if stops:
                    break
                continue
            if select.select([sys.stdin], [], [], 0)[0]:
                if sys.stdin.read(1) == " ":  # ignore stray keys, stop on space
                    break
    print(f"recorded {time.time() - t0:.1f}s")
    return resample(np.concatenate(chunks), sr, WPSR)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="base.en", help="whisper model name")
    ap.add_argument("--engine", choices=("whisper", "whistle"), default="whistle",
                    help="transcription engine: whistle (default, needs cactus-needle) or whisper")
    ap.add_argument("--no-vad", action="store_true",
                    help="disable VAD (whisper only; its edge-trimming can eat the first/last phoneme of your speech)")
    args = ap.parse_args()

    if args.engine == "whisper":
        print(f"loading whisper model ({args.model})...")
        model = WhisperModel(args.model, device="cpu", compute_type="int8")
    else:
        os.environ.setdefault("NEEDLE_TELEMETRY", "0"); os.environ.setdefault("DO_NOT_TRACK", "1")
        try:
            import needle  # cactus-needle
        except ImportError:
            raise SystemExit("cactus-needle not installed; run `.venv/bin/pip install cactus-needle soxr` or use --engine whisper")
        if not os.path.exists(WHISTLE_CACT):
            raise SystemExit(f"{WHISTLE_CACT} not found; run `.venv/bin/needle download Cactus-Compute/whistle --out models` (or edit WHISTLE_CACT)")
        print("loading whistle model...")

    ctx = zmq.Context()
    cmd = ctx.socket(zmq.SUB)   # typer: PUB connect -> us
    cmd.setsockopt(zmq.SUBSCRIBE, b"")
    pub = ctx.socket(zmq.PUB)   # we: PUB bind -> typer SUB connect
    try:
        cmd.bind(f"tcp://127.0.0.1:{CMD_PORT}")
        pub.bind(f"tcp://127.0.0.1:{OUT_PORT}")
    except zmq.ZMQError:
        raise SystemExit(f"another listen.py is already running (ports {CMD_PORT}/{OUT_PORT} in use); kill it first")

    with quiet_terminal():
        first = True
        try:
            while True:
                if first:
                    print("ready — spacebar / F14 (typer.py) hold-to-talk")
                    first = False
                else:
                    print("ready")
                wait_start(cmd)
                with system_muted():
                    audio = record(cmd=cmd)
                if len(audio) < WPSR * 0.2:
                    print("(too short to transcribe)")
                    continue
                print("transcribing...")
                if args.engine == "whisper":
                    segs, _ = model.transcribe(audio, vad_filter=not args.no_vad)
                    text = " ".join(s.text.strip() for s in segs)
                else:
                    text = needle.transcribe(audio, weights=WHISTLE_CACT)["text"]
                print(text)
                pub.send_multipart([b"result", text.encode()])
        except KeyboardInterrupt:
            print("\nbye")


if __name__ == "__main__":
    main()
