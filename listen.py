#!/usr/bin/env python3
"""Push-to-talk: spacebar to start, speak, spacebar to stop -> Whisper prints the text.

Mic: hardcoded to the UMIK (MIC below); override by editing that constant.
"""
import argparse
import select
import sys
import time
from contextlib import contextmanager

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

WPSR = 16000  # whisper's rate
MIC = "umik"  # fuzzy device name
MAX_SECS = 60  # safety cap in case the stop keypress is missed


@contextmanager
def quiet_terminal():
    """cbreak + no-echo for the whole session; restores on exit (even on Ctrl-C)."""
    import termios, tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def wait_space():
    while select.select([sys.stdin], [], [], 0)[0]:  # drop stale keys
        sys.stdin.read(1)
    sys.stdin.read(1)  # blocking: the space itself


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


def record(max_secs=MAX_SECS):
    device = resolve_device(MIC)
    sr = int(sd.query_devices(device, "input")["default_samplerate"])
    block = max(64, sr // 30)
    chunks = []
    with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                        blocksize=block, device=device) as s:
        print("recording... press space to stop")
        t0 = time.time()
        while time.time() - t0 < max_secs:
            chunks.append(s.read(block)[0][:, 0].copy())
            if select.select([sys.stdin], [], [], 0)[0]:
                if sys.stdin.read(1) == " ":  # ignore stray keys, stop on space
                    break
    print(f"recorded {time.time() - t0:.1f}s")
    return resample(np.concatenate(chunks), sr, WPSR)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="base.en", help="whisper model name")
    args = ap.parse_args()

    print(f"loading whisper model ({args.model})...")
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    with quiet_terminal():
        first = True
        try:
            while True:
                print("ready — press spacebar to start, speak, spacebar to stop"
                      if first else "ready")
                first = False
                wait_space()
                audio = record()
                if len(audio) < WPSR * 0.2:
                    print("(too short to transcribe)")
                    continue
                print("transcribing...")
                segs, _ = model.transcribe(audio, vad_filter=True)
                print(" ".join(s.text.strip() for s in segs))
        except KeyboardInterrupt:
            print("\nbye")


if __name__ == "__main__":
    main()
