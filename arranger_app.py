"""arranger_app.py - DX Arranger: your HTML panel + the style engine, together.

Run:   python arranger_app.py      (or double-click start.bat)

What it does
  * starts the sound engine (FluidSynth + your SoundFont, or the Windows synth)
  * serves dxarrangernew1.html at http://127.0.0.1:8765  (this computer only)
  * P1..P16 buttons open the style folders listed in config.json
  * the section / start / stop / tempo / volume buttons control the engine

Needs sty_play.py and sty_parse.py in the same folder. Style files are only read.
"""
import json
import os
import queue
import random
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sty_play as sp
import multipad as mp
import effects as fxmod

# Next to the .exe when packaged (PyInstaller), otherwise next to this script
APP_DIR = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STYLE_EXTS = (".sty", ".prs", ".bcs", ".sst", ".pcs", ".pst", ".fps")

DEFAULT_CFG = {
    "port": 8765,
    "html_file": "dxarrangernew1.html",
    "synth": "fluid",                     # "fluid" | "windows" | "vst" | "midiout" (a MIDI port, e.g. a VST drum plugin) | "null" (silent)
    "vst_path": "MT-PowerDrumKit.vst3",   # synth "vst": the VST3 instrument played inside the app (needs pedalboard + sounddevice)
    "vst_rate": 44100,                    # synth "vst": sample rate
    "vst_block": 512,                     # synth "vst": audio block in frames (256 = tighter but may crackle, 1024 = safer)
    "vst_gain": 1.0,                      # synth "vst": output level
    "vst_device": None,                   # synth "vst": part of the audio output's name, or null = Windows default
    "midi_out_name": "loopMIDI",          # synth "midiout": part of the MIDI output port name (see the list printed at start)
    "fluid_exe": sp.FLUID_EXE,
    "soundfont": sp.SF2,
    "fluid_driver": sp.FLUID_DRIVER,
    "fluid_gain": sp.FLUID_GAIN,
    "import_folder": os.path.join("Styles", "Imported"),   # styles imported from the panel / a phone
    "multipad_folder": "Multipad",        # drop pad files (.pad .mid) and samples (.wav .mp3) here; MENU > STYLES > MULTIPAD
    "multipad_repeat": True,              # pads loop until stopped (press again, or STOP). False = play once
    "drums_only": True,                   # play ONLY the rhythm parts (drums); bass/chords/pads are never sent
    "fx_geq": fxmod.DEFAULTS["fx_geq"],           # GRAPHIC EQ dB: kick, low, mid, high, air
    "fx_eq3m": fxmod.DEFAULTS["fx_eq3m"],         # EQ3-M dB: low, mid, high
    "fx_ezeq": fxmod.DEFAULTS["fx_ezeq"],         # EZ EQ tilt dB (+ brighter, - darker)
    "fx_drive": fxmod.DEFAULTS["fx_drive"],       # BL DRIVE amount 0-1
    "fx_studio": fxmod.DEFAULTS["fx_studio"],     # STUDIO REVERB: room, damping, width, level
    "fx_spring": fxmod.DEFAULTS["fx_spring"],     # SPRING REVERB: room, damping, width, level
    "shaker_level": 0.6,                  # shakers/maracas/cabasa volume: 0.6 = soft, 0.4 = very soft, 1 = as written
    "high_polish": 0.7,                   # 0-1: hats/shakers/cymbals get a played feel (beat strongest, 16ths softest). 0 = as written
    "soundfont_extra": [],                # more .sf2 files loaded on top of "soundfont" (e.g. a better drum kit); later ones win
    "fluid_polyphony": sp.FLUID_POLY,     # voices at once (512 = tails of cymbals/shakers never cut)
    "fluid_rate": sp.FLUID_RATE,          # sample rate (48000 = cleaner highs)
    "fluid_interp": sp.FLUID_INTERP,      # interpolation: 7 = best quality, 4 = lighter, 0 = FluidSynth default
    "fluid_reverb": list(sp.FLUID_REVERB),  # room, damping, width, level (soft-tailed "air"); null = FluidSynth default
    "fluid_chorus": None,                 # [voices, level, speed, depth] or null
    "drum_kit": None,                     # None = the style's own kit. A number 0-127 forces that General MIDI kit (0 = Standard)
    "drums_include_sub": True,            # drums_only: also play the 2nd rhythm part (channel 9) on the drum channel
    "fluid_bufsize": 0,                   # FluidSynth audio block (frames); 0 = default. Try 128 if beats feel loose
    "play_sub_rhythm": False,             # only used when drums_only is false
    # P1..P16 style folders. Relative paths are inside this app's folder. Right-click (or long-press)
    # a P button on the panel to point it at any other folder.
    "folders": {str(n): os.path.join("Styles", f"P{n:02d}") for n in range(1, 17)},
    # VOL-1..VOL-6 knobs -> MIDI channel numbers (0-based: 8 = channel 9). The panel does not label them.
    "vol_groups": {"VOL-1": [8, 9], "VOL-2": [10], "VOL-3": [11], "VOL-4": [12], "VOL-5": [13], "VOL-6": [14, 15]},
    # Percussion pads -> General MIDI drum notes (edit freely; the panel does not label them)
    "perc": {"PERC-1": 38, "PERC-2": 40, "PERC-3": 36, "PERC-4": 45, "PERC-5": 47, "PERC-6": 50,
             "PERC2-1": 49, "PERC2-2": 57, "PERC2-3": 51, "PERC2-4": 42, "PERC2-5": 46, "PERC2-6": 39},
}


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            for key, val in user.items():
                if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                    cfg[key].update(val)
                else:
                    cfg[key] = val
        except (OSError, ValueError) as err:
            print(f"! config.json could not be read ({err}); using defaults.")
    save_cfg(cfg)
    return cfg


def save_cfg(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def resolve(path):
    return path if os.path.isabs(path) else os.path.join(APP_DIR, path)


class NullOut:
    """Silent output for testing: records messages instead of playing them."""
    dry = False

    def __init__(self):
        self.log = []

    def send(self, m):
        self.log.append(bytes(m))

    def send_many(self, msgs):
        self.log.extend(bytes(m) for m in msgs)

    def set_gain(self, pct):
        pass

    def close(self):
        pass


class PartMixer:
    """Sits between the style and the synth and scales each part's channel volume (CC 7)."""

    def __init__(self, out, groups):
        self.out, self.groups = out, groups
        self.gain, self.raw = [1.0] * 16, [100] * 16
        self.shape = None                 # (note, velocity) -> velocity for drum hits: EQ / BL DRIVE from the EFFECTS menu

    @property
    def dry(self):
        return self.out.dry

    def send(self, m):
        self.out.send(self._scaled(m))

    def send_many(self, msgs):
        sp.send_all(self.out, [self._scaled(m) for m in msgs])

    def _scaled(self, m):
        if len(m) == 3 and m[0] & 0xF0 == 0xB0 and m[1] == 7:
            ch = m[0] & 0x0F
            self.raw[ch] = m[2]
            return bytes([m[0], 7, int(round(m[2] * self.gain[ch]))])
        if self.shape and len(m) == 3 and m[0] == 0x99 and m[2] > 0:       # a drum hit on channel 10
            return bytes([0x99, m[1], self.shape(m[1], m[2])])
        return m

    def set_reverb(self, params):
        if hasattr(self.out, "set_reverb"):
            self.out.set_reverb(params)

    def set_part(self, key, pct):
        for ch in self.groups.get(key, []):
            self.gain[ch] = max(0, min(100, pct)) / 100
            self.out.send(bytes([0xB0 | ch, 7, int(round(self.raw[ch] * self.gain[ch]))]))

    def set_gain(self, pct):
        self.out.set_gain(pct)

    def efx(self, on):
        if hasattr(self.out, "efx"):
            self.out.efx(on)

    def close(self):
        self.out.close()


class CmdSource:
    """Feeds panel commands to the playing loop."""

    def __init__(self, engine):
        self.e = engine

    def pop(self):
        out = []
        while True:
            try:
                c = self.e.q.get_nowait()
            except queue.Empty:
                break
            if c[0] == "load" and self.e.st["running"]:
                self.e.swap_async(c[1])        # keep playing; the new style arrives as a "swap" at the next bar
                continue
            if c[0] == "swap":
                if c[2] == self.e.swap_id:
                    out.append(("swap", c[1]))
                continue
            if c[0] in ("stop", "load", "quit"):
                if c[0] != "stop":
                    self.e.pending = c         # handled after the loop stops
                out.append(("stop",))
                break
            out.append(c)
        return out


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.q = queue.Queue()
        self.pending = None
        self.out = None
        self.master_pct = 100
        self.lock = threading.Lock()
        self.secs, self.get, self.setup = {}, None, []
        self.quarter, self.bar = 0.75, 3.0
        self.st = dict(synth="starting", message="Starting the sound engine...", error="", loaded=False,
                       name="", running=False, section="", main="Main A", intro=None, bpm=120.0,
                       base_bpm=120.0, auto_fill=True, available=[], rev=0, muted=False, efx=True, path="",
                       pads=[], pad_error="")
        self.swap_id = 0
        self.fx = fxmod.Effects(cfg, os.path.join(APP_DIR, "effects.json"))
        self.sync_info = {}                 # filled by the style player: lets the Multi Pads start on its beat
        self.pads = mp.PadEngine(lambda: self.out, self._pad_sync, lambda: self.st["bpm"],
                                 resolve(cfg["multipad_folder"]), os.path.join(APP_DIR, "pads.json"),
                                 repeat=bool(cfg.get("multipad_repeat", True)), on_change=self._pads_changed,
                                 shaker=max(0.0, min(1.5, float(cfg.get("shaker_level", 0.6)))))
        self.st["pads"] = self.pads.info()
        self.st["fx"] = dict(self.fx.on)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ---- multi pads ----------------------------------------------------------
    def _pad_sync(self):
        c = self.sync_info.get("clock")
        return (c, self.sync_info["quarter"]) if c is not None and self.st["running"] else None

    def _pads_changed(self):
        self._set(pads=self.pads.info(), pad_error=self.pads.error)

    # ---- state -------------------------------------------------------------
    def _set(self, **kw):
        with self.lock:
            self.st.update(kw)

    def snapshot(self):
        with self.lock:
            return dict(self.st)

    def _on_state(self, seg, bpm, main):
        self._set(section=seg, bpm=bpm, main=main)

    # ---- sound -------------------------------------------------------------
    def _init_synth(self):
        kind = self.cfg["synth"]
        try:
            if kind == "null":
                self.out = NullOut()
            elif kind == "windows":
                self.out = sp.Out(False)
            elif kind == "vst":
                import vst_out
                self.out = vst_out.VstOut(resolve(self.cfg["vst_path"]), self.cfg["vst_rate"], self.cfg["vst_block"],
                                          self.cfg["vst_gain"], self.cfg["vst_device"])
            elif kind == "midiout":
                print("MIDI outputs:", ", ".join(sp.midi_out_devices()) or "(none)")
                self.out = sp.MidiOut(False, sp.find_midi_out(self.cfg["midi_out_name"]))
            else:
                sp.FLUID_EXE, sp.FLUID_DRIVER = self.cfg["fluid_exe"], self.cfg["fluid_driver"]
                sp.FLUID_GAIN = float(self.cfg["fluid_gain"])
                sp.FLUID_BUFSIZE = int(self.cfg.get("fluid_bufsize", 0))
                sp.FLUID_RATE = int(self.cfg.get("fluid_rate", sp.FLUID_RATE))
                sp.FLUID_POLY = int(self.cfg.get("fluid_polyphony", sp.FLUID_POLY) or 0)
                sp.FLUID_EXTRA_SF2 = [str(f) for f in (self.cfg.get("soundfont_extra") or [])]
                sp.FLUID_INTERP = int(self.cfg.get("fluid_interp", sp.FLUID_INTERP) or 0)
                rv, ch = self.cfg.get("fluid_reverb"), self.cfg.get("fluid_chorus")
                sp.FLUID_REVERB = tuple(rv) if rv else None
                sp.FLUID_CHORUS = tuple(ch) if ch else None
                self.out = sp.FluidOut(False, self.cfg["soundfont"])
            self.out = PartMixer(self.out, {k: list(v) for k, v in self.cfg["vol_groups"].items()})
            self._set(synth=kind, message="Sound engine ready. Press a P button to pick a style.")
        except (SystemExit, Exception) as err:
            text = str(err).strip().splitlines()
            problem = text[0] if text else repr(err)
            try:
                self.out = PartMixer(sp.Out(False), {k: list(v) for k, v in self.cfg["vol_groups"].items()})
                self._set(synth="windows", message=f"{kind} failed ({problem}). Using the Windows synth.")
            except Exception as err2:
                self._set(synth="none", error=f"No sound: {problem} / {err2}")
        self.fx_apply()                       # effects chosen last time are active from the start

    def partvol(self, key, pct):
        if isinstance(self.out, PartMixer):
            self.out.set_part(key, pct)

    def master(self, pct):
        self.master_pct = max(0, min(100, int(pct)))
        if self.out and not self.st["muted"]:
            self.out.set_gain(self.master_pct)

    def toggle_mute(self):
        muted = not self.st["muted"]
        self._set(muted=muted)
        if self.out:
            self.out.set_gain(0 if muted else self.master_pct)

    def toggle_efx(self):
        on = not self.st["efx"]
        self._set(efx=on)
        if self.out and hasattr(self.out, "efx"):
            self.out.efx(on)

    def recall(self, reg):
        """Apply a saved registration: style, main, tempo, auto fill, volumes."""
        if reg.get("path") and os.path.isfile(reg["path"]) and reg["path"] != self.st.get("path"):
            self.q.put(("load", reg["path"]))
        if reg.get("main"):
            self.q.put(("main", reg["main"][-1]))
        if reg.get("bpm"):
            self.q.put(("tempo", reg["bpm"]))
        self._set(auto_fill=bool(reg.get("auto_fill", True)))
        for key, pct in (reg.get("vols") or {}).items():
            self.partvol(key, int(pct))
        self.master(int(reg.get("master", 100)))

    def perc(self, pad_id):
        note = self.cfg["perc"].get(pad_id)
        if note is None or not self.out:
            return
        self.out.send(bytes([0x99, int(note), 110]))
        threading.Timer(0.3, lambda: self.out.send(bytes([0x89, int(note), 0]))).start()

    def set_auto_fill(self, value=None):
        self._set(auto_fill=(not self.st["auto_fill"]) if value is None else bool(value))

    # ---- main loop (own thread) -------------------------------------------
    def _loop(self):
        self._init_synth()
        while True:
            c = self.pending or self.q.get()
            self.pending = None
            op = c[0]
            try:
                if op == "quit":
                    break
                elif op == "load":
                    self._load(c[1])
                elif op == "swap":
                    if c[2] == self.swap_id:
                        self._apply(c[1], keep_tempo=False)
                elif op == "start":
                    self._play()
                elif op == "main" and self.st["loaded"] and f"Main {c[1]}" in self.secs:
                    self._set(main=f"Main {c[1]}")
                elif op == "intro" and self.st["loaded"] and f"Intro {c[1]}" in self.secs:
                    self._set(intro=None if self.st["intro"] == c[1] else c[1])
                elif op == "tempo":
                    self._set(bpm=max(20.0, min(300.0, float(c[1]))))
            except Exception as err:             # keep the engine alive whatever happens
                self._set(error=f"{type(err).__name__}: {err}", running=False)
        if self.out:
            self.out.close()

    def _prepare(self, path):
        """Read a style and build everything needed to play it. Raises ValueError/SystemExit with a message."""
        ppq, events = sp.load(path)
        secs, (s0, s1) = sp.find_sections(events, ppq)
        if not secs:
            raise ValueError("No sections found in that file.")
        to_sec = sp.make_clock(events, ppq)
        drums = bool(self.cfg["drums_only"])
        with9 = bool(self.cfg["drums_include_sub"] if drums else self.cfg["play_sub_rhythm"])
        kit = self.cfg.get("drum_kit")
        amount = max(0.0, min(1.0, float(self.cfg.get("high_polish", 0.7) or 0)))
        shk = max(0.0, min(1.5, float(self.cfg.get("shaker_level", 0.6))))
        pol = (ppq, amount, shk) if drums and (amount > 0 or shk != 1.0) else None
        setup = sp.build(events, s0, s1, to_sec, with9, None, drums, kit)[0] if s0 is not None else []
        cache = {}

        def get(sec):
            if sec not in cache:
                cache[sec] = sp.build(events, *secs[sec], to_sec, with9, None, drums, kit, pol)
            return cache[sec]

        first_us = next((int.from_bytes(i[1], "big") for t, k, i in events
                         if k == "meta" and i[0] == 0x51 and len(i[1]) == 3), 500000)
        num, den = sp.time_sig(events)
        mains = [n for n in secs if n.startswith("Main")]
        return dict(secs=secs, get=get, setup=setup, base=60_000_000 / first_us, num=num, den=den, drums=drums,
                    main="Main A" if "Main A" in secs else (mains[0] if mains else next(iter(secs))),
                    name=os.path.splitext(os.path.basename(path))[0], path=path)

    def _apply(self, pkg, keep_tempo):
        """Make a prepared style the current one. keep_tempo=True (changing style while playing) leaves the tempo,
        the running state and the chosen Main alone."""
        self.secs, self.get, self.setup = pkg["secs"], pkg["get"], pkg["setup"]
        self.quarter = 60.0 / pkg["base"]
        self.bar = self.quarter * 4 * pkg["num"] / pkg["den"]
        name, base = pkg["name"], pkg["base"]
        if keep_tempo:
            main = self.st["main"] if self.st["main"] in pkg["secs"] else pkg["main"]
            self._set(loaded=True, name=name, path=pkg["path"], base_bpm=base, main=main,
                      available=list(pkg["secs"]), rev=self.st["rev"] + 1,
                      message=f"Now playing {name.upper()} (same tempo).")
        else:
            self._set(loaded=True, name=name, path=pkg["path"], base_bpm=base, bpm=base, main=pkg["main"],
                      intro=None, available=list(pkg["secs"]), section="", rev=self.st["rev"] + 1,
                      message=f"Loaded {name.upper()} ({base:.0f} BPM{', drums only' if pkg['drums'] else ''}). "
                              "Press the start button.")

    def _load(self, path):
        self._set(loaded=False, running=False, error="", message="Loading...")
        try:
            pkg = self._prepare(path)
        except (SystemExit, ValueError) as err:
            self._set(error=str(err), message="")
            return
        self._apply(pkg, keep_tempo=False)

    def swap_async(self, path):
        """Change style while playing: read it in the background (so the beat is never delayed), then hand it to
        the player, which switches at the next bar line at the current tempo."""
        self.swap_id += 1
        sid = self.swap_id
        self._set(message="Changing style at the next bar...")

        def work():
            try:
                pkg = self._prepare(path)
            except (SystemExit, ValueError, OSError) as err:
                self._set(error=str(err), message="")
                return
            self.q.put(("swap", pkg, sid))

        threading.Thread(target=work, daemon=True).start()

    def _play(self):
        st = self.snapshot()
        if not st["loaded"] or not self.out:
            self._set(error="Load a style first (press a P button, choose a file, press ENTER).")
            return
        intro = f"Intro {st['intro']}" if st["intro"] else None
        start = intro if intro in self.secs else st["main"]
        for ch in range(16):
            if ch not in self.pad_channels():
                self.out.send(bytes([0xB0 | ch, 123, 0]))
        for _, m in self.setup:
            self.out.send(m)
        self._set(running=True, error="", message="")
        held, stats = set(), [0.0, 0, 0]
        try:
            sp.run_live(self.out, self.get, self.secs, self.quarter, self.bar, st["base_bpm"], st["bpm"],
                        start, stats, held, keys=CmdSource(self), on_state=self._on_state,
                        auto_fill=lambda: self.st["auto_fill"], main=st["main"], sync=self.sync_info,
                        on_swap=lambda pkg: self._apply(pkg, keep_tempo=True))
        except KeyboardInterrupt:
            pass
        finally:
            self.sync_info.clear()
            self._set(running=False)             # pads running on the style's clock switch to their own
            for ch, note in held:
                self.out.send(bytes([0x80 | ch, note, 0]))
            for ch in range(16):                 # all-notes-off on the style's channels only: pads keep playing
                if ch not in self.pad_channels():
                    self.out.send(bytes([0xB0 | ch, 123, 0]))
            self._set(running=False, section="", intro=None)

    def fx_apply(self):
        """Push the EFFECTS menu state into the mixer (EQ / drive) and the synth (reverb)."""
        self._set(fx=dict(self.fx.on))
        out = self.out
        if out is None:                                   # synth still starting: _init_synth applies it when ready
            return
        out.shape = self.fx.shape()
        if hasattr(out, "set_reverb"):
            out.set_reverb(self.fx.reverb())

    def fx_toggle(self, ident):
        self.fx.toggle(ident)
        self.fx_apply()
        return dict(self.fx.on)

    def pad_channels(self):
        """MIDI channels the pads that are playing right now use (the style's stop must not cut them)."""
        used = set()
        for n, live in self.pads.live.items():
            if live:
                used.update(mp.POOL[n])
        return used

    def shutdown(self):
        self.pads.close()
        self.q.put(("quit",))
        self.thread.join(timeout=3)
        if self.out:
            self.out.close()


# ---- helpers for folders ---------------------------------------------------
def slot_folder(cfg, slot):
    if int(slot) == 0:
        return resolve(cfg["import_folder"])
    return resolve(cfg["folders"].get(str(slot), os.path.join("Styles", f"P{int(slot):02d}")))


def list_styles(folder):
    os.makedirs(folder, exist_ok=True)
    names = [n for n in os.listdir(folder) if n.lower().endswith(STYLE_EXTS) and
             os.path.isfile(os.path.join(folder, n))]
    return sorted(names, key=str.lower)


def pick_folder(initial):
    if getattr(sys, "frozen", False):         # packaged .exe: cannot start a second Python, so ask here
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(initialdir=initial, title="Choose the style folder")
            root.destroy()
            return path or ""
        except Exception:
            return ""
    code = ("import tkinter as tk\nfrom tkinter import filedialog\nr = tk.Tk()\nr.withdraw()\n"
            "r.attributes('-topmost', True)\n"
            "print(filedialog.askdirectory(initialdir=%r, title='Choose the style folder'))\n" % initial)
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=600)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


REG_PATH = os.path.join(APP_DIR, "registrations.json")


def reg_load():
    try:
        with open(REG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


ENGINE = None
CFG = None
PIN = ""
LOCAL = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
PIN_PAGE = ("<!DOCTYPE html><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<body style='background:#05070d;color:#cfeaff;font:18px system-ui;display:grid;place-items:center;"
            "height:100vh;margin:0'><form><p>DX ARRANGER</p><input name=pin inputmode=numeric autofocus "
            "placeholder='PIN' style='font-size:28px;width:140px;text-align:center'> "
            "<button style='font-size:24px'>OK</button><p style='font-size:13px'>The PIN is shown in the "
            "black window on the PC.</p></form>")
IOS_HEAD = ('<meta name="apple-mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
            '<meta name="apple-mobile-web-app-title" content="DX Arranger">'
            '<style>*{touch-action:manipulation;-webkit-tap-highlight-color:transparent;'
            '-webkit-touch-callout:none}</style>')


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "your-pc-address"


def is_authorized(ip, cookie, pin):
    """The PC itself is always allowed; phones need the PIN cookie."""
    if ip in LOCAL:
        return True
    return bool(pin) and f"dxpin={pin}" in [c.strip() for c in (cookie or "").split(";")]


class Handler(BaseHTTPRequestHandler):
    server_version = "DXArranger/1.0"

    def log_message(self, *args):
        pass

    def _send(self, body, ctype, code=200):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj), "application/json; charset=utf-8", code)

    def _auth_ok(self, u):
        if is_authorized(self.client_address[0], self.headers.get("Cookie"), PIN):
            return True
        if PIN and parse_qs(u.query).get("pin", [""])[0] == PIN:
            self.send_response(302)
            self.send_header("Set-Cookie", f"dxpin={PIN}; Path=/; Max-Age=31536000; SameSite=Lax")
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self._send(PIN_PAGE, "text/html; charset=utf-8", 401)
        return False

    def do_GET(self):
        u = urlparse(self.path)
        if not self._auth_ok(u):
            return
        if u.path in ("/", "/classic", "/new"):
            html_path = resolve(CFG["html_file"])                    # YOUR panel, unchanged look
            if u.path == "/new":
                html_path = os.path.join(APP_DIR, "panel.html")      # the optional redesign
            try:
                with open(html_path, encoding="utf-8") as f:
                    html = f.read()
            except OSError:
                return self._send(f"Cannot find {html_path}", "text/plain", 404)
            j = html.find("</head>")
            html = html[:j] + IOS_HEAD + html[j:] if j >= 0 else IOS_HEAD + html
            if "data-native-panel" not in html:                      # old page needs the bridge
                tag = '<script src="/bridge.js"></script>'
                i = html.rfind("</body>")
                html = html[:i] + tag + html[i:] if i >= 0 else html + tag
            return self._send(html, "text/html; charset=utf-8")
        if u.path == "/bridge.js":
            try:
                with open(os.path.join(APP_DIR, "bridge.js"), "rb") as f:
                    return self._send(f.read(), "application/javascript; charset=utf-8")
            except OSError:
                return self._send("bridge.js is missing", "text/plain", 404)
        if u.path == "/api/state":
            return self._json(ENGINE.snapshot())
        if u.path == "/api/folder":
            q = parse_qs(u.query)
            try:
                slot = int(q.get("slot", ["0"])[0])
            except ValueError:
                slot = 0
            if not 1 <= slot <= 16:
                return self._json({"error": "slot must be 1-16"}, 400)
            folder = slot_folder(CFG, slot)
            files = list_styles(folder)
            return self._json({"slot": slot, "path": folder, "folder_name": os.path.basename(folder),
                               "files": files,
                               "labels": [os.path.splitext(n)[0].upper() for n in files]})
        if u.path == "/api/padfolder":
            folder = resolve(CFG["multipad_folder"])
            files = mp.list_files(folder)
            return self._json({"path": folder, "files": files, "pads": ENGINE.pads.info(),
                               "labels": [os.path.splitext(n)[0].upper() for n in files]})
        self._send("Not found", "text/plain", 404)

    def do_POST(self):
        u = urlparse(self.path)
        if not is_authorized(self.client_address[0], self.headers.get("Cookie"), PIN):
            return self._json({"error": "PIN required"}, 401)
        if u.path == "/api/upload":                       # a style file chosen on the panel (or a phone)
            try:
                n = int(self.headers.get("Content-Length", "0"))
                name = os.path.basename(parse_qs(u.query).get("name", [""])[0])
            except ValueError:
                return self._json({"error": "bad request"}, 400)
            if not name.lower().endswith(STYLE_EXTS) or not 0 < n <= 5_000_000:
                return self._json({"error": "send a .STY file under 5 MB"}, 400)
            folder = slot_folder(CFG, 0)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, name)
            with open(path, "wb") as f:
                f.write(self.rfile.read(n))
            ENGINE.q.put(("load", path))                  # load it straight away
            return self._json({"ok": True, "file": name})
        if u.path == "/api/padupload":                    # a sample / pad file chosen on the panel (or a phone)
            try:
                n = int(self.headers.get("Content-Length", "0"))
                name = os.path.basename(parse_qs(u.query).get("name", [""])[0])
            except ValueError:
                return self._json({"error": "bad request"}, 400)
            if not name.lower().endswith(mp.PAD_EXTS) or not 0 < n <= 60_000_000:
                return self._json({"error": "send a .wav .mp3 .pad .mid file under 60 MB"}, 400)
            folder = resolve(CFG["multipad_folder"])
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, name), "wb") as f:
                f.write(self.rfile.read(n))
            return self._json({"ok": True, "file": name})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return self._json({"error": "bad request"}, 400)

        if u.path == "/api/padload":
            try:
                names = ENGINE.pads.load(str(body.get("name", "")), body.get("slot", 1))
            except (ValueError, OSError) as err:
                return self._json({"ok": False, "error": str(err)}, 400)
            return self._json({"ok": True, "names": names, "pads": ENGINE.pads.info()})

        if u.path == "/api/load":
            try:
                slot = int(body.get("slot", 0))
            except (TypeError, ValueError):
                slot = 0
            name = os.path.basename(str(body.get("name", "")))   # no folder tricks
            path = os.path.join(slot_folder(CFG, slot), name)
            if not 0 <= slot <= 16 or not os.path.isfile(path):
                return self._json({"error": "file not found"}, 404)
            ENGINE.q.put(("load", path))
            return self._json({"ok": True})

        if u.path == "/api/setfolder":
            if self.client_address[0] not in LOCAL:           # the folder window opens on the PC
                return self._json({"ok": False, "error": "change folders on the PC"})
            try:
                slot = int(body.get("slot", 0))
            except (TypeError, ValueError):
                slot = 0
            if not 1 <= slot <= 16:
                return self._json({"error": "slot must be 1-16"}, 400)
            chosen = pick_folder(slot_folder(CFG, slot))
            if not chosen:
                return self._json({"ok": False})
            CFG["folders"][str(slot)] = chosen
            save_cfg(CFG)
            return self._json({"ok": True, "path": chosen})

        if u.path == "/api/cmd":
            op, arg = body.get("op"), body.get("arg")
            if op in ("main", "intro", "ending"):
                if arg in ("A", "B", "C", "D"):
                    ENGINE.q.put((op, arg))
            elif op in ("fill", "break", "start", "stop"):
                ENGINE.q.put((op,))
                if op == "stop":
                    ENGINE.pads.stop_all()          # STOP = the beat AND every pad
            elif op == "tempo":
                try:
                    ENGINE.q.put(("tempo", float(arg)))
                except (TypeError, ValueError):
                    pass
            elif op == "autofill":
                ENGINE.set_auto_fill()
            elif op == "master":
                try:
                    ENGINE.master(int(arg))
                except (TypeError, ValueError):
                    pass
            elif op == "mute":
                ENGINE.toggle_mute()
            elif op == "efx":
                ENGINE.toggle_efx()
            elif op == "reg_save":
                regs, st = reg_load(), ENGINE.snapshot()
                regs[str(arg)] = {"path": st.get("path"), "main": st["main"], "bpm": st["bpm"],
                                  "auto_fill": st["auto_fill"], "vols": body.get("vols") or {},
                                  "master": body.get("master", 100)}
                with open(REG_PATH, "w", encoding="utf-8") as f:
                    json.dump(regs, f, indent=1)
            elif op == "reg_recall":
                reg = reg_load().get(str(arg))
                if not reg:
                    return self._json({"ok": False})
                ENGINE.recall(reg)
                return self._json({"ok": True, "vols": reg.get("vols") or {}, "master": reg.get("master", 100)})
            elif op == "partvol":
                try:
                    ENGINE.partvol(str(arg), int(body.get("value", 100)))
                except (TypeError, ValueError):
                    pass
            elif op == "perc":
                ENGINE.perc(str(arg))
            elif op == "pad":
                if str(arg) in ("1", "2", "3", "4"):
                    ENGINE.pads.trigger(int(arg))
            elif op == "padstop":
                ENGINE.pads.stop_all()
            elif op == "fx":
                if str(arg) in fxmod.IDS or arg == "alloff":
                    return self._json({"ok": True, "fx": ENGINE.fx_toggle(str(arg))})
            else:
                return self._json({"error": "unknown command"}, 400)
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)


def main():
    global ENGINE, CFG, PIN
    CFG = load_cfg()
    os.chdir(APP_DIR)                         # fluid_log.txt and relative folders live beside the app
    lan = bool(CFG.get("lan")) or os.environ.get("DX_LAN") == "1" or "--phone" in sys.argv
    CFG["lan"] = lan
    PIN = str(CFG.get("pin") or random.randint(1000, 9999)) if lan else ""
    for slot in range(0, 17):
        os.makedirs(slot_folder(CFG, slot), exist_ok=True)
    os.makedirs(resolve(CFG["multipad_folder"]), exist_ok=True)
    ENGINE = Engine(CFG)
    port = int(CFG["port"])
    try:
        server = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", port), Handler)
    except OSError as err:
        sys.exit(f"Could not start the panel server on port {port}: {err}\n"
                 f"Close the other copy of this program, or change \"port\" in config.json.")
    url = f"http://127.0.0.1:{port}/"
    print("DX Arranger is running.")
    print(f"  Panel:   {url}   (opening your browser now)")
    if lan:
        print(f"  Phone:   http://{lan_ip()}:{port}/   PIN {PIN}   (same Wi-Fi; sound plays on this PC)")
    print(f"  Styles:  {os.path.join(APP_DIR, 'Styles')}  (P1..P16 folders; edit config.json to move them)")
    print(f"  Pads:    {resolve(CFG['multipad_folder'])}  (samples and pad files for the Multi Pads)")
    print("  Stop:    press Ctrl+C in this window")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down...")
        ENGINE.shutdown()


if __name__ == "__main__":
    main()
