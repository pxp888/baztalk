# talk

Push-to-talk dictation: hold a key, speak, release, and the transcription is typed into whatever window has focus.
This exists because bazzite makes voxtype difficult to install without altering the OS. 



- Hold **F14** (or the spacebar) → recording starts
- Release → Whisper transcribes the audio
- The text is typed into the focused app via a **virtual (uinput) keyboard**

```
 F14/space  ┌───────────┐  start/stop  ┌──────────────┐  result  ┌──────────────────┐
  keyboard  │ typer.py  │ ──────────►  │  listen.py   │ ───────► │  typer.py types  │
  (watch)   │ (sudo)    │ ◄──────────  │  (mic+whisper)│          │  via "talk-typer"│
```

## Requirements

- Linux (needs `/dev/uinput`, `/dev/input`, `pactl`)
- Python deps installed in a venv: `faster-whisper`, `sounddevice`, `zmq`, `evdev`, `numpy`, `soxr`
  (`faster-whisper` also pulls ctranslate2 + a whisper model on first run)
  The default engine is Phonon-2, so also install `fermion-research` (Setup below); the `--engine whisper` path needs nothing extra
- A microphone (hardcoded to a device matching **`umik`** by default — edit the `MIC` constant in `listen.py` to override)
- Physical keyboard that exposes **F14** (check with `sudo evtest`)

## Setup

```sh
python -m venv .venv
.venv/bin/pip install faster-whisper sounddevice pyzmq evdev numpy soxr
# list audio devices to find your mic name:
.venv/bin/python -m sounddevice
# default engine: Phonon-2 (~164 MB, downloaded once on first run). On Linux install the CPU torch wheel first so pip skips the CUDA build.
.venv/bin/pip install --no-deps torch --index-url https://download.pytorch.org/whl/cpu && .venv/bin/pip install fermion-research
```

## Usage

1. **Start the listener** (no sudo):

   ```sh
   .venv/bin/python listen.py            # or: dist/listen
   .venv/bin/python listen.py --model tiny.en --no-vad   # options
   .venv/bin/python listen.py --engine whisper           # original faster-whisper engine
   ```

   It loads the Phonon-2 model (cached in `~/.cache/fermion` after first run), binds ports 5561/5562, and waits for a push-to-talk trigger.

2. **Start the typer** (needs sudo for `/dev/uinput`):

   ```sh
   sudo .venv/bin/python typer.py                 # or: dist/typer
   ```

   It types a short test sentence first ("talk-typer: typing works") — focus your target window before running to verify typing lands. Pass `--no-test` to skip, or `--text "..."` to change it.

3. **Talk.** Hold **F14** to record, release to stop. While recording, system audio is muted (PulseAudio) and restored after. The transcription prints to the listener's terminal and is typed into the focused window.

   In the listener's terminal, the **spacebar** also works as a push-to-talk trigger — same hold-to-release semantics. (Typing the result still requires `typer.py` to be running.)

Notes & limits:

- Recording is capped at 60 s in case the release keypress is missed.
- Transcription is CPU int8, so it's as fast as your CPU is (default model `base.en`).
- Default engine is Phonon-2 (~35 ms per clip on this CPU, runs at the AVX-512 VNNI kernel tier; check with `.venv/bin/fermion describe`). If you prefer faster-whisper: `--engine whisper` (then `--model`, `--no-vad` apply).
- `--no-vad`: VAD edge-trimming can eat the first/last phoneme of your speech; disable it if that happens at the cost of transcribing leading/trailing silence.
- The virtual keystrokes go to whatever window has keyboard focus. Wayland apps with secure input (password fields, browser credential bars) will ignore them.
- Uppercase/punctuation works via the registered key set; keys not in `MAP`/`SHIFTED` in `typer.py` are silently skipped.
- Only one `listen.py` can run at a time (ports 5561/5562 are single-instance locks).

## Troubleshooting

- **First/last character dropped:** apps or the compositor sometimes swallow edge keys. The typer already primes/flushes with inert shift taps and small inter-key delays; if it still happens, use `capture.py`:

  ```sh
  sudo .venv/bin/python capture.py    # then trigger a typing burst
  ```

  It logs every EV_KEY the virtual keyboard actually emits — if the key is missing there, typer dropped it; if it's present there but not in your app, the app/compositor swallowed it.

- **Can't find the mic:** `python -m sounddevice`, then set `MIC` in `listen.py` to a substring of the device name (or a direct index).

- **"another listen.py is already running":** exactly that — find and kill the old process.
