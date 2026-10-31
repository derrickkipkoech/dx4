"""vst_out.py - plays DX Arranger's MIDI through a VST3 instrument plugin (e.g. MT Power Drum Kit)
inside the app itself: no FluidSynth, no loopMIDI, no separate host.

Needs:  pip install pedalboard sounddevice     (both are bundled into the Windows .exe by the build)
Used when config.json has  "synth": "vst"  and  "vst_path" pointing at the .vst3 file.

How the timing works: every MIDI message is stamped with the moment it arrived. The audio callback renders the
plugin one small block at a time and places each message inside the NEXT block at the same offset it arrived
at. So there is a fixed delay of one block (512 frames = ~11 ms) but no jitter.
"""
import os
import sys
import threading
import time


class VstOut:
    dry = False

    def __init__(self, path, rate=44100, block=512, gain=1.0, device=None):
        try:
            import numpy as np
            import sounddevice as sd
            from pedalboard import load_plugin
        except ImportError as err:
            sys.exit(f"VST hosting needs the 'pedalboard' and 'sounddevice' packages ({err}).")
        self.np, self.sd = np, sd
        if not os.path.exists(path):
            sys.exit(f"VST plugin not found:\n  {path}\nSet \"vst_path\" in config.json.")
        try:
            self.plugin = load_plugin(path)
        except Exception as err:
            sys.exit(f"Could not load the plugin: {err}")
        if not getattr(self.plugin, "is_instrument", True):
            sys.exit("That plugin is an effect, not an instrument (it makes no sound from MIDI).")
        self.rate, self.block = int(rate), int(block)
        self.base_gain, self.master = float(gain), 1.0
        self.lock = threading.Lock()
        self.pending = []                     # (bytes, arrival time)
        self.error = ""
        self._first = True
        self._prev = time.perf_counter()
        self._self_test()
        self.stream = sd.OutputStream(samplerate=self.rate, blocksize=self.block, channels=2, dtype="float32",
                                      device=self._find_device(device), callback=self._callback, latency="low")
        self.stream.start()

    # ---- helpers ------------------------------------------------------------------
    def _find_device(self, name):
        if not name:
            return None
        for i, d in enumerate(self.sd.query_devices()):
            if d["max_output_channels"] > 0 and str(name).lower() in d["name"].lower():
                return i
        print(f"! audio output '{name}' not found; using the default. Available: " +
              ", ".join(d["name"] for d in self.sd.query_devices() if d["max_output_channels"] > 0))
        return None

    def _render(self, msgs, seconds, reset):
        return self.plugin(msgs, duration=seconds, sample_rate=self.rate, num_channels=2,
                           buffer_size=self.block, reset=reset)

    def _self_test(self):
        """Play one kick offline. A silent result almost always means the plugin cannot find its sound content."""
        try:
            a = self._render([(bytes([0x99, 36, 110]), 0.0)], 0.6, True)
            if float(self.np.abs(a).max()) < 1e-4:
                print("! The plugin produced no sound for a test kick. Its sound content was probably not found: run "
                      "MT Power Drum Kit's standalone once and point it at your content folder, then restart.")
            else:
                print("VST plugin ready:", getattr(self.plugin, "name", "plugin"))
        except Exception as err:
            print("! Plugin self-test failed:", err)

    # ---- audio thread -------------------------------------------------------------
    def _callback(self, outdata, frames, tinfo, status):
        now = time.perf_counter()
        prev, self._prev = self._prev, now
        with self.lock:
            batch, self.pending = self.pending, []
        dur = frames / self.rate
        msgs = [(m, min(max(t - prev, 0.0), dur * 0.999)) for m, t in batch]
        try:
            audio = self._render(msgs, dur, self._first)
            self._first = False
            block = audio.T * (self.base_gain * self.master)
            n = min(frames, block.shape[0])
            outdata[:n] = block[:n]
            if n < frames:
                outdata[n:] = 0
        except Exception as err:               # never let the audio thread die
            outdata.fill(0)
            if str(err) != self.error:
                self.error = str(err)
                print("! plugin error:", err)

    # ---- the interface the engine uses (same as the other outputs) -----------------
    def send(self, m):
        now = time.perf_counter()
        with self.lock:
            self.pending.append((bytes(m), now))

    def send_many(self, msgs):
        now = time.perf_counter()
        with self.lock:
            self.pending.extend((bytes(m), now) for m in msgs)

    def set_gain(self, pct):
        self.master = max(0, min(100, pct)) / 100.0
        if pct <= 0:                           # MUTE: also stop any ringing notes
            self.send_many([bytes([0xB0 | ch, 120, 0]) for ch in range(16)])

    def close(self):
        try:
            self.send_many([bytes([0xB0 | ch, 123, 0]) for ch in range(16)])
            time.sleep(self.block / self.rate * 2)
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
