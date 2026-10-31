"""sty_play.py - Stage 5: first audible player. Loops ONE section.

Usage:
  python sty_play.py file.STY                 -> list the sections
  python sty_play.py file.STY "Main A"        -> loop Main A (Ctrl+C stops)
  python sty_play.py file.STY "Main A" --dry  -> print messages, no sound
  add --with9 to also play MIDI channel 9 (Sub Rhythm)
  add --drums to play ONLY the drums. Yamaha styles keep their drums on TWO channels (9 and 10);
      both are played, on the drum channel, so the whole beat is heard and nothing else.
      Add --no9 to use channel 10 alone.
  add --only=10,11 to play only the channels you list (1-16)
  add --bpm=100 to play at 100 beats per minute (default: the style's own)
  add --notes to list which notes each channel plays (no sound): finds drum
      notes that the Windows synth may not have
  add --synth=fluid to use FluidSynth + your SoundFont (set the two paths below)
  add --live to play like an arranger: keys A B C D = Main (with its fill-in),
      F = fill-in, K = break, E = ending, + / - = tempo, Q = quit.
      A tempo change is heard at once (mid-bar, the beat does not move). Fills start on the next beat.
  Timing report: after Ctrl+C it prints how late notes were sent.

Needs sty_parse.py in the SAME folder. Reads the file only.
Sound: the Windows built-in "Microsoft GS Wavetable Synth" (no install).
HONEST LIMITS of this stage:
  * plays the notes AS WRITTEN (source key C Maj7): no chord following yet
  * General MIDI sounds, not Yamaha voices, so it will sound different
  * Yamaha SysEx is not sent; channel 9 is muted by default because
    GM only has drums on channel 10
"""
import math
import os
import socket
import struct
import subprocess
import sys
import threading
import time

from sty_parse import parse_track

sys.setswitchinterval(0.001)   # let the timing loop run again within 1 ms when other threads (web panel) were busy

SETUP = {"SFF1", "SFF2", "SInt"}

# ---- FluidSynth settings (edit if your paths differ) ----
FLUID_EXE = r"C:\Users\Marcus peter\Desktop\lynth WINDOWS\fluidsynth-v2.6.1-win10-x64-cpp11\bin\fluidsynth.exe"
SF2 = r"C:\Users\Marcus peter\Desktop\lynth WINDOWS\fluidsynth-v2.6.1-win10-x64-cpp11\bin\GeneralUser_GS_v1.471.sf2"
FLUID_DRIVER = "dsound"   # Windows audio driver; try "wasapi" or "waveout" if no sound
FLUID_GAIN = 0.7          # lower this if the sound crackles/clips
FLUID_PORT = 9800
FLUID_RATE = 88200        # sample rate; 48000 keeps cymbals, hats and shakers cleaner than 44100
FLUID_INTERP = 7          # interpolation quality: 7 = best (brighter, cleaner highs), 4 = lighter on the CPU, 0 = leave alone
FLUID_REVERB = (0.0, 0.0, 0.0, 0.0)   # room size, damping (higher = softer tail), width, level; None = leave FluidSynth's own
FLUID_CHORUS = None       # (voices, level, speed, depth) or None = leave FluidSynth's own
FLUID_POLY = 128          # simultaneous voices: long cymbal and shaker tails are no longer cut short
FLUID_EXTRA_SF2 = []      # more SoundFonts loaded AFTER the main one; later files win, so a drum-kit .sf2 here replaces the drums
FLUID_BUFSIZE = 128         # audio block size in frames; 0 = FluidSynth's own default. If beats still feel loose, try 128
TEMPO_STEP = 1.0          # BPM change for each + or - key in live mode


def load(path):
    if not os.path.isfile(path):                  # allow  "beat"  for  beat.STY
        for ext in (".sty", ".STY", ".Sty", ".prs", ".bcs", ".sst", ".pcs", ".pst", ".fps"):
            if os.path.isfile(path + ext):
                path += ext
                break
        else:
            sys.exit(f"File not found: {path}\nPut the style file in this folder or give its full name, e.g. beat.STY")
    with open(path, "rb") as f:  # read-only
        data = f.read()
    if data[:4] != b"MThd":
        sys.exit("Not a MIDI-based file.")
    hlen = struct.unpack(">I", data[4:8])[0]
    ppq = struct.unpack(">H", data[12:14])[0]
    pos = 8 + hlen
    if data[pos:pos + 4] != b"MTrk":
        sys.exit("No MTrk after header.")
    size = struct.unpack(">I", data[pos + 4:pos + 8])[0]
    return ppq, parse_track(data[pos + 8:pos + 8 + size])


def time_sig(events):
    for t, k, i in events:
        if k == "meta" and i[0] == 0x58 and len(i[1]) >= 2:
            return i[1][0], 2 ** i[1][1]
    return 4, 4


def find_sections(events, ppq=None):
    """Return ({name: (start_tick, end_tick)}, (setup_start, setup_end)).
    With ppq given, every section length is snapped to a whole number of bars (when it is within one
    beat of it), so a marker or track end that is a few ticks out can never make a loop drift off the beat."""
    marks = [(t, i[1].decode("latin-1")) for t, k, i in events if k == "meta" and i[0] == 6]
    end_tick = max(t for t, _, _ in events)
    secs, setup_start, setup_end = {}, None, None
    for n, (t, name) in enumerate(marks):
        nxt = marks[n + 1][0] if n + 1 < len(marks) else end_tick
        if name in SETUP:
            setup_start = t if setup_start is None else setup_start
            setup_end = nxt
        else:
            secs[name] = (t, nxt)
    if ppq:
        num, den = time_sig(events)
        bar_ticks = max(1, ppq * 4 * num // den)
        for name, (a, b) in list(secs.items()):
            bars = max(1, round((b - a) / bar_ticks))
            snapped = a + bars * bar_ticks
            if snapped == b:
                continue
            if abs(snapped - b) <= ppq:
                secs[name] = (a, snapped)
            else:
                print(f"! '{name}' is {(b - a) / bar_ticks:.2f} bars long, not a whole number of bars: "
                      f"it cannot loop exactly on the beat.")
    return secs, (setup_start, setup_end)


def make_clock(events, ppq):
    pts, us, sec, last = [(0, 0.0, 500000)], 500000, 0.0, 0
    for t, k, i in events:
        if k == "meta" and i[0] == 0x51 and len(i[1]) == 3:
            sec += (t - last) * us / 1e6 / ppq
            last, us = t, int.from_bytes(i[1], "big")
            pts.append((t, sec, us))

    def to_sec(t):
        base = pts[0]
        for p in pts:                       # pts are in time order: keep the LAST one at or before t
            if p[0] <= t:
                base = p
        return base[1] + (t - base[0]) * base[2] / 1e6 / ppq
    return to_sec


def msg_bytes(kind, chan, data):
    return bytes([kind | (chan - 1)]) + bytes(data)


SHAKERS = {69, 70, 82}   # cabasa, maracas, shaker
HIGH_PERC = {42, 44, 46, 51, 53, 54, 59, 69, 70, 73, 74, 75, 80, 81, 82, 83}   # hats, ride, tambourine, shakers...


def polish_velocity(note, vel, tick, ppq, amount):
    """Give hats, shakers and other high percussion a played feel: the beat strongest, the "and" softer, the 16ths
    softest, a touch less harsh overall, and a tiny fixed variation (the same on every loop, so the groove is steady).
    Only velocity changes, never timing, so nothing can go off the beat."""
    ph = (tick % ppq) / ppq
    if min(ph, 1 - ph) < 0.06:
        f = 1.0
    elif abs(ph - 0.5) < 0.06:
        f = 0.88
    elif min(abs(ph - 0.25), abs(ph - 0.75)) < 0.06:
        f = 0.74
    else:
        f = 0.80
    f *= 0.94
    h = ((tick * 2654435761) ^ (note * 40503)) & 0xFFFF
    f *= 1 + (h / 65535.0 - 0.5) * 0.12
    return max(1, min(127, int(round(vel + (vel * f - vel) * amount))))


def build(events, start, end, to_sec, with9, only=None, drums=False, kit=None, polish=None):
    """drums=True: nothing but the rhythm parts. Channel 10 plays as written; with with9=True the notes of
    channel 9 (the second rhythm part) are played on channel 10 too, because only channel 10 is a drum channel
    in General MIDI / FluidSynth. All melodic parts (bass, chords, pad, phrase) are dropped."""
    base, out = to_sec(start), []
    for t, k, i in events:
        if k != "chan" or not (start <= t < end):
            continue
        kind, chan, data = i
        if drums:
            if chan == 9 and with9 and kind in (0x80, 0x90):
                chan = 10
            elif chan != 10:
                continue
            if kit is not None and chan == 10 and kind == 0xC0:
                continue                                # drum kit forced (kit=N): ignore the style's own choice
        elif only is not None:
            if chan not in only:
                continue
        elif chan == 9 and not with9:
            continue
        if polish and drums and chan == 10 and kind == 0x90 and data[1] > 0:
            note, vel = data[0], data[1]
            if note in HIGH_PERC and polish[1] > 0:
                vel = polish_velocity(note, vel, t - start, polish[0], polish[1])
            if note in SHAKERS and len(polish) > 2:          # soft shakers: polish[2] = level, 1.0 = as written
                vel = max(1, min(127, int(round(vel * polish[2]))))
            data = [note, vel]
        out.append((to_sec(t) - base, msg_bytes(kind, chan, data)))
    if drums and kit is not None:
        out.insert(0, (0.0, bytes([0xC9, int(kit)])))   # General MIDI drum kit number on channel 10
    return out, to_sec(end) - base


def boost_timing():
    """Windows only: 1 ms timer resolution and high process priority, so beats are sent on time."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
        ctypes.windll.winmm.timeBeginPeriod(1)
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k.SetPriorityClass(k.GetCurrentProcess(), 0x80)
    except Exception:
        pass


def send_all(out, msgs):
    """Send messages that belong to the same moment in ONE write when the output can (keeps a kick and a hat together)."""
    if not msgs:
        return
    many = getattr(out, "send_many", None)
    if many:
        many(msgs)
    else:
        for m in msgs:
            out.send(m)


def midi_out_devices():
    """Names of the Windows MIDI output devices, in device-number order (empty off Windows)."""
    try:
        import ctypes as c

        class CAPS(c.Structure):
            _fields_ = [("wMid", c.c_ushort), ("wPid", c.c_ushort), ("ver", c.c_uint), ("name", c.c_wchar * 32),
                        ("tech", c.c_ushort), ("voices", c.c_ushort), ("notes", c.c_ushort),
                        ("mask", c.c_ushort), ("support", c.c_uint)]
        w = c.windll.winmm
        names = []
        for i in range(w.midiOutGetNumDevs()):
            caps = CAPS()
            if w.midiOutGetDevCapsW(c.c_size_t(i), c.byref(caps), c.sizeof(caps)) == 0:
                names.append(caps.name)
            else:
                names.append(f"device {i}")
        return names
    except Exception:
        return []


def find_midi_out(name):
    """Device number of the first MIDI output whose name contains `name` (case-insensitive). Exits with the list if none."""
    devs = midi_out_devices()
    for i, n in enumerate(devs):
        if name.lower() in n.lower():
            return i
    sys.exit(f"No MIDI output named '{name}'.\nAvailable: " + (", ".join(devs) or "(none)") +
             "\nInstall loopMIDI, create a port, then set \"midi_out_name\" in config.json.")


class Out:
    def __init__(self, dry, device=0xFFFFFFFF):
        self.dry, self.n = dry, 0
        if not dry:
            import ctypes
            w = self.winmm = ctypes.windll.winmm
            c = ctypes
            w.midiOutOpen.argtypes = [c.POINTER(c.c_void_p), c.c_uint, c.c_size_t, c.c_size_t, c.c_uint]
            w.midiOutShortMsg.argtypes = [c.c_void_p, c.c_uint]
            w.midiOutReset.argtypes = [c.c_void_p]
            w.midiOutClose.argtypes = [c.c_void_p]
            w.timeBeginPeriod.argtypes = [c.c_uint]
            w.timeEndPeriod.argtypes = [c.c_uint]
            self.h = c.c_void_p()
            r = w.midiOutOpen(c.byref(self.h), device, 0, 0, 0)
            if r != 0:
                sys.exit(f"Could not open the Windows MIDI output (error {r}). If it is a loopMIDI port, another program may already be using it.")
            self.winmm.timeBeginPeriod(1)
            k = ctypes.windll.kernel32           # ask Windows to treat us as high priority
            k.GetCurrentProcess.restype = ctypes.c_void_p
            k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            k.SetPriorityClass(k.GetCurrentProcess(), 0x80)

    def send(self, m):
        if self.dry:
            if self.n < 40:
                print("  ", " ".join(f"{b:02X}" for b in m))
            self.n += 1
            return
        v = m[0] | (m[1] << 8 if len(m) > 1 else 0) | (m[2] << 16 if len(m) > 2 else 0)
        self.winmm.midiOutShortMsg(self.h, v)

    def send_many(self, msgs):
        for m in msgs:
            self.send(m)

    def set_gain(self, pct):                      # not available on the Windows synth
        pass

    def close(self):
        if not self.dry:
            self.winmm.midiOutReset(self.h)
            self.winmm.midiOutClose(self.h)
            self.winmm.timeEndPeriod(1)


class MidiOut(Out):
    """Plays through a chosen MIDI port (e.g. a loopMIDI port that a VST host / the plugin's standalone listens on).
    Everything is sent unchanged; the master level and effects belong to the plugin, so they are not touched here."""
    def set_gain(self, pct):
        if pct <= 0 and not self.dry:             # MUTE: silence what is sounding (all sound off on every channel)
            for ch in range(16):
                self.send(bytes([0xB0 | ch, 120, 0]))


def to_fluid(m):
    """Turn raw MIDI bytes into a FluidSynth shell command (channels are 0-15).
    Bank-select (CC 0 / 32) is skipped so Yamaha banks cannot pick a missing preset;
    programs are then looked up as plain General MIDI. Pitch bend is not sent yet."""
    st, ch = m[0] & 0xF0, m[0] & 0x0F
    if st == 0x90 and len(m) > 2:
        return f"noteon {ch} {m[1]} {m[2]}" if m[2] > 0 else f"noteoff {ch} {m[1]}"
    if st == 0x80 and len(m) > 1:
        return f"noteoff {ch} {m[1]}"
    if st == 0xB0 and len(m) > 2 and m[1] not in (0, 32):
        return f"cc {ch} {m[1]} {m[2]}"
    if st == 0xC0 and len(m) > 1:
        return f"prog {ch} {m[1]}"
    return None


def fluid_args(sf2):
    args = [FLUID_EXE, "-s", "-i", "-a", FLUID_DRIVER, "-g", str(FLUID_GAIN), "-r", str(int(FLUID_RATE))]
    if FLUID_POLY:
        args += ["-o", f"synth.polyphony={int(FLUID_POLY)}"]
    if FLUID_BUFSIZE > 0:
        args += ["-z", str(int(FLUID_BUFSIZE))]
    extra = []
    for f in FLUID_EXTRA_SF2:
        if os.path.isfile(f):
            extra.append(f)
        else:
            print("! extra SoundFont not found, skipped:", f)
    return args + [sf2] + extra


class FluidOut:
    def __init__(self, dry, sf2):
        self.dry, self.n, self.proc, self.sock = dry, 0, None, None
        self.lock = threading.Lock()
        if dry:
            return
        boost_timing()
        self.sock = self._connect(0.3)           # already running? reuse it
        if self.sock is None:
            for what, path in (("FluidSynth program", FLUID_EXE), ("SoundFont", sf2)):
                if not os.path.isfile(path):
                    sys.exit(f"{what} not found:\n  {path}\nEdit the path at the top of sty_play.py.")
            flags = 0x08000000 if os.name == "nt" else 0   # no extra window
            log = open("fluid_log.txt", "w")      # FluidSynth's own messages go here
            self.proc = subprocess.Popen(
                fluid_args(sf2),
                cwd=os.path.dirname(FLUID_EXE), stdout=log, stderr=subprocess.STDOUT,
                creationflags=flags)
            for _ in range(80):                   # wait up to ~20 s while the SoundFont loads
                time.sleep(0.25)
                if self.proc.poll() is not None:
                    sys.exit("FluidSynth stopped by itself.\n" + self._log_tail() +
                             "\nClose any other FluidSynth window and check the paths at the top.")
                self.sock = self._connect(0.3)
                if self.sock:
                    break
            else:
                self.proc.terminate()
                sys.exit("FluidSynth did not start in time.\nWhat FluidSynth said (fluid_log.txt):\n"
                         + self._log_tail() + "\nIf Windows showed a firewall question, answer Allow, "
                         "then try again.")
        threading.Thread(target=self._drain, daemon=True).start()
        self._cmd("reset")
        self.tune()

    @staticmethod
    def _log_tail():
        try:
            with open("fluid_log.txt", errors="replace") as f:
                lines = f.read().strip().splitlines()[-12:]
            return "\n".join("  " + x for x in lines) if lines else "  (it printed nothing)"
        except OSError:
            return "  (no log)"

    @staticmethod
    def _connect(timeout):
        for host in ("127.0.0.1", "::1"):         # IPv4 first, then IPv6
            try:
                c = socket.create_connection((host, FLUID_PORT), timeout=timeout)
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                c.settimeout(None)
                return c
            except OSError:
                continue
        return None

    def _drain(self):                             # FluidSynth answers every command; read and discard
        try:
            while self.sock.recv(4096):
                pass
        except OSError:
            pass

    def _cmd(self, text):
        try:
            with self.lock:                       # several threads may send (style + pads)
                self.sock.sendall((text + "\n").encode())
        except OSError:
            sys.exit("Lost the connection to FluidSynth.")

    def tune(self):
        """Sound quality: best interpolation and a gentle, soft-tailed reverb. Commands an older FluidSynth does not
        know are ignored (it just answers with an error line)."""
        cmds = []
        if FLUID_INTERP:
            cmds.append(f"interp {int(FLUID_INTERP)}")
        if FLUID_REVERB:
            room, damp, width, level = FLUID_REVERB
            cmds += ["reverb on", f"rev_setroomsize {room}", f"rev_setdamp {damp}", f"rev_setwidth {width}",
                     f"rev_setlevel {level}"]
        if FLUID_CHORUS:
            nr, level, speed, depth = FLUID_CHORUS
            cmds += ["chorus on", f"cho_set_nr {nr}", f"cho_set_level {level}", f"cho_set_speed {speed}",
                     f"cho_set_depth {depth}"]
        if cmds:
            self._cmd("\n".join(cmds))

    def set_reverb(self, params):
        """(room, damping, width, level) from the EFFECTS menu; None goes back to the normal reverb setting."""
        room, damp, width, level = params or FLUID_REVERB or (0.0, 0.0, 0.0, 0.0)
        cmd = f"rev_setroomsize {room}\nrev_setdamp {damp}\nrev_setwidth {width}\nrev_setlevel {level}"
        if self.dry:
            print("  reverb:", cmd.replace("\n", " "))
            return
        self._cmd(cmd)

    def efx(self, on):
        """Reverb + chorus on/off."""
        if not self.dry and self.sock:
            flag = "on" if on else "off"
            self._cmd(f"reverb {flag}")
            self._cmd(f"chorus {flag}")

    def set_gain(self, pct):
        """Master volume 0-100 % of FLUID_GAIN."""
        if not self.dry and self.sock:
            self._cmd(f"gain {FLUID_GAIN * max(0, min(100, pct)) / 100:.3f}")

    def send(self, m):
        cmd = to_fluid(m)
        if cmd is None:
            return
        if self.dry:
            if self.n < 40:
                print("  ", cmd)
            self.n += 1
            return
        self._cmd(cmd)

    def send_many(self, msgs):
        cmds = [c for c in map(to_fluid, msgs) if c]
        if not cmds:
            return
        if self.dry:
            for c in cmds:
                if self.n < 40:
                    print("  ", c)
                self.n += 1
            return
        self._cmd("\n".join(cmds))               # one write: all hits of this instant reach FluidSynth together

    def close(self):
        if self.dry or self.sock is None:
            return
        try:
            if self.proc:
                self._cmd("quit")
                self.proc.terminate()
            else:
                self._cmd("reset")                # leave a shared server quiet
            self.sock.close()
        except OSError:
            pass


def wait_until(t, poll=None):
    """Wait until time t (a perf_counter value, or a function that returns one and may change while we wait,
    which is how a tempo change takes effect at once). If poll() returns True, stop early and return True."""
    target = t if callable(t) else (lambda: t)
    while True:
        if poll is not None and poll():
            return True
        d = target() - time.perf_counter()
        if d <= 0:
            return False
        if d > 0.003:
            time.sleep(0.001)          # look for a button press ~1000 times a second
        else:
            time.sleep(0)              # last 3 ms: spin, so the hit lands exactly on the beat


class TempoClock:
    """Maps the style's own time ("song position", in seconds at the style's tempo) to real time.
    Changing the tempo re-anchors at the current position, so the beat never jumps.
    The state is one tuple, so other threads (the Multi Pads) can read it while the tempo changes."""

    def __init__(self, base_bpm, bpm, t_real, pos=0.0):
        self.base = base_bpm
        self.s = (t_real, pos, base_bpm / bpm)       # (real time, song position, real seconds per style second)

    def real(self, pos):
        t, p, k = self.s
        return t + (pos - p) * k

    def pos_at(self, t_real):
        t, p, k = self.s
        return p + (t_real - t) / k

    def set_bpm(self, bpm, now):
        self.s = (now, self.pos_at(now), self.base / bpm)


EPS = 1e-4     # seconds: events this close to a cut belong to the section that STARTS there (no double or missing hit)


def group(body, off=0.0, tol=0.0005):
    """[(time, msg)] -> [(time, [msgs])]: events within 0.5 ms share one write. Events before `off` are skipped."""
    out = []
    for at, m in body:
        if at < off - EPS:
            continue
        if out and at - out[-1][0] <= tol:
            out[-1][1].append(m)
        else:
            out.append((at, [m]))
    return out


def track(held, m):
    st, ch = m[0] & 0xF0, m[0] & 0x0F
    if st == 0x90 and m[2] > 0:
        held.add((ch, m[1]))
    elif st == 0x80 or (st == 0x90 and m[2] == 0):
        held.discard((ch, m[1]))


class Keys:
    """Collects key presses in the background (live mode)."""

    def __init__(self):
        self.q = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            if os.name == "nt":
                import msvcrt
                while True:
                    self.q.append(msvcrt.getwch().lower())
            else:
                while True:
                    c = sys.stdin.read(1)
                    if not c:
                        break
                    if not c.isspace():
                        self.q.append(c.lower())
        except Exception:
            pass

    def pop(self):
        out = []
        while self.q:
            out.append(self.q.pop(0))
        return out


def play_segment(out, body, length, t0, stats, held):
    """Play one section once, starting at time t0. Returns the time the next one starts."""
    for at, msgs in group(body):
        wait_until(t0 + at)
        late = time.perf_counter() - (t0 + at)
        stats[0], stats[2] = max(stats[0], late), stats[2] + len(msgs)
        stats[1] += late > 0.015
        send_all(out, msgs)
        for m in msgs:
            track(held, m)
    wait_until(t0 + length)
    send_all(out, [bytes([0x80 | ch, note, 0]) for ch, note in held])   # cut notes hanging over the boundary
    held.clear()
    return t0 + length


def _norm_cmd(key):
    """Turn a key press or an app command into a tuple like ('main','B')."""
    if isinstance(key, tuple):
        return key
    if key == "q":
        return ("stop",)
    if key in ("+", "="):
        return ("tempo_delta", TEMPO_STEP)
    if key in ("-", "_"):
        return ("tempo_delta", -TEMPO_STEP)
    if key in ("a", "b", "c", "d"):
        return ("main", key.upper())
    if key == "f":
        return ("fill",)
    if key == "k":
        return ("break",)
    if key == "e":
        return ("ending", "A")
    return None


def run_live(out, get, secs, quarter, bar, base_bpm, bpm, name, stats, held,
             keys=None, on_state=None, auto_fill=None, main=None, sync=None, on_swap=None):
    """Arranger-style live playing. "Style seconds" are seconds at the style's own tempo.
    Everything is scheduled on ONE song-position clock (TempoClock), so:
      * a tempo change is heard at once, in the middle of a bar, and the beat does not move;
      * fill-in / break start at the NEXT BEAT from the matching point inside the fill, and end on the bar line;
      * pressing another Main during a fill makes the new fill take over at the next beat;
      * Intro / Ending start at the next bar.
    A ("swap", package) command changes to another style WITHOUT stopping: at the next bar line the new style's
    section (same name if it has one) takes over, at the same tempo and on the same beat grid.
    keys: object with pop() -> list of keys/tuples (default: the keyboard).
    on_state(seg=, bpm=, main=) is called whenever the section or tempo changes."""
    keys = keys or Keys()
    auto_fill = auto_fill or (lambda: True)
    cur, queue, ending = "Main A", [], False
    if name.startswith("Main"):
        cur = name
    else:
        queue = [(name, 0.0)]
        tail = name.split()[-1]
        if "Main " + tail[-1] in secs:          # Intro A -> Main A, Fill In BB -> Main B
            cur = "Main " + tail[-1]
        ending = name.startswith("Ending")
    if main and main in secs:
        cur = main
    beats_per_bar = max(1, round(bar / quarter))
    print("LIVE: A B C D = Main (with fill)   F = fill   K = break   E = ending   "
          "+ / - = tempo   Q = quit")
    bpm = max(20.0, min(300.0, float(bpm)))
    clock = TempoClock(base_bpm, bpm, time.perf_counter() + 0.2)
    if sync is not None:                       # lets the Multi Pads start exactly on the style's beat
        sync["clock"], sync["quarter"] = clock, quarter
    G0 = 0.0                      # song position where the section now playing began
    shown, played, offs = None, 0, []
    swap = {"pkg": None}          # a new style waiting for the next bar line
    while True:
        seg, off = queue.pop(0) if queue else (cur, 0.0)
        if swap["pkg"] is not None:                          # change of style: same tempo, same grid
            pkg, swap["pkg"] = swap["pkg"], None
            r = pkg["base"] / base_bpm                       # the new style's seconds -> ours (our tempo stays)
            new_get, new_secs = pkg["get"], pkg["secs"]

            def get(n, _g=new_get, _r=r):
                body, length = _g(n)
                return [(at * _r, m) for at, m in body], length * _r
            secs = new_secs
            bar = quarter * 4 * pkg["num"] / pkg["den"]
            beats_per_bar = max(1, round(bar / quarter))
            first_main = next((n for n in secs if n.startswith("Main")), next(iter(secs)))
            cur = cur if cur in secs else first_main
            if seg not in secs:
                seg = cur
            send_all(out, [m for _, m in pkg["setup"]])                      # the new style's drum kit and levels
            if on_swap:
                on_swap(pkg)
        if seg != shown:
            print(f"  -> {seg}" + (f"  (from beat {off / quarter + 1:.0f})" if off else ""))
            shown = seg
        if on_state:
            on_state(seg=seg, bpm=bpm, main=cur)
        body, length = get(seg)
        is_main = seg.startswith("Main")
        can_interrupt = is_main or seg.startswith("Fill In")   # a fill can be taken over by another fill
        cut = {"G": None}          # song position where this section is cut short (None = plays to its end)
        G_end = G0 + (length - off)

        def poll():
            nonlocal bpm, cur, ending
            changed = False
            for raw in keys.pop():
                c = _norm_cmd(raw)
                if c is None:
                    continue
                op = c[0]
                if op == "stop":
                    raise KeyboardInterrupt
                if op in ("tempo_delta", "tempo"):
                    bpm = c[1] if op == "tempo" else bpm + c[1]
                    bpm = max(20.0, min(300.0, float(bpm)))
                    clock.set_bpm(bpm, time.perf_counter())      # applies immediately, mid-bar
                    changed = True
                    continue
                if op == "swap":
                    swap["pkg"] = c[1]
                    if is_main and not queue:                # in a Main: change at the next bar line
                        pos = off + (clock.pos_at(time.perf_counter()) - G0)
                        cut_un = (math.floor(pos / bar + 1e-6) + 1) * bar
                        queue[:] = [(seg, 0.0)]
                        if cut_un < length - 1e-6:
                            cut["G"] = G0 + (cut_un - off)
                    continue                                 # in a fill/intro/ending: change when it ends
                target, mode = None, "beat"
                if op == "main" and f"Main {c[1]}" in secs:
                    cur = f"Main {c[1]}"
                    changed = True
                    if auto_fill():
                        target = f"Fill In {c[1]}{c[1]}"
                elif op == "fill" and is_main:
                    target = f"Fill In {cur[-1]}{cur[-1]}"
                elif op == "break" and is_main:
                    target = "Fill In BA"
                elif op == "ending" and f"Ending {c[1]}" in secs:
                    target, mode = f"Ending {c[1]}", "bar"
                    ending = True
                elif op == "intro" and f"Intro {c[1]}" in secs:
                    target, mode = f"Intro {c[1]}", "bar"
                else:
                    continue
                if target not in secs or not can_interrupt:
                    if mode == "bar" and target in secs:
                        queue[:] = [(target, 0.0)]
                    continue                      # no such fill, or not interruptible: apply at section end
                if target == seg:
                    continue                      # same fill is already playing
                pos = off + (clock.pos_at(time.perf_counter()) - G0)   # where we are inside this section
                if mode == "bar":
                    cut_un = (math.floor(pos / bar + 1e-6) + 1) * bar
                    new_off = 0.0
                else:
                    nb = math.floor(pos / quarter + 1e-6) + 1
                    cut_un = nb * quarter
                    new_off = (nb % beats_per_bar) * quarter
                queue[:] = [(target, new_off)]
                cut["G"] = None
                if cut_un < length - 1e-6:
                    cut["G"] = G0 + (cut_un - off)
            if changed and on_state:
                on_state(seg=seg, bpm=bpm, main=cur)
            return cut["G"] is not None and clock.real(cut["G"]) <= time.perf_counter()

        # controllers/programs before our start point + notes left over from the last section: ONE write
        send_all(out, offs + [m for at, m in body if at < off - EPS and m[0] & 0xF0 in (0xB0, 0xC0, 0xE0)])
        offs = []
        interrupted = False
        for at, msgs in group(body, off):
            G = G0 + (at - off)
            if cut["G"] is not None and G >= cut["G"] - EPS:
                break
            if wait_until(lambda G=G: clock.real(G), poll):
                interrupted = True
                break
            if cut["G"] is not None and G >= cut["G"] - EPS:
                break                  # a cut was set while we waited: this hit belongs to the NEXT section
            late = time.perf_counter() - clock.real(G)
            stats[0], stats[2] = max(stats[0], late), stats[2] + len(msgs)
            stats[1] += late > 0.015
            send_all(out, msgs)
            for m in msgs:
                track(held, m)
        if not interrupted:
            wait_until(lambda: clock.real(cut["G"] if cut["G"] is not None else G_end), poll)
        G0 = cut["G"] if cut["G"] is not None else G_end        # the next section starts exactly here
        offs = [bytes([0x80 | ch, note, 0]) for ch, note in held]   # cut notes hanging over the boundary
        held.clear()
        send_all(out, offs)
        offs = []
        played += 1
        if ending and seg.startswith("Ending") and not queue:
            print("  (ending finished)")
            return
        if out.dry and played >= 8:
            return


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        sys.exit(__doc__)
    ppq, events = load(args[0])
    secs, (s0, s1) = find_sections(events, ppq)
    if len(args) == 1:
        print("Sections in this style:")
        for name, (a, b) in secs.items():
            print(f"  {name:<12} ticks {a}..{b}  ({(b - a) / ppq / 4:.1f} bars if 4/4)")
        return
    name = args[1]
    if name not in secs:
        sys.exit(f"No section '{name}'. Run without a name to list sections.")
    to_sec = make_clock(events, ppq)
    drums = "--drums" in flags
    with9 = ("--with9" in flags) or (drums and "--no9" not in flags)
    only = None
    for f in flags:
        if f.startswith("--only="):
            only = {int(x) for x in f[7:].split(",")}
    kit = next((int(f[6:]) for f in flags if f.startswith("--kit=")), None)
    shk = next((float(f[10:]) for f in flags if f.startswith("--shaker=")), 0.6)     # --shaker=0.4 softer, 1 = as written
    pol = (ppq, 0.7, shk) if drums and "--nopolish" not in flags else None
    setup, _ = build(events, s0, s1, to_sec, with9, only, drums, kit) if s0 is not None else ([], 0)
    cache = {}

    def get(sec):
        if sec not in cache:
            cache[sec] = build(events, *secs[sec], to_sec, with9, only, drums, kit, pol)
        return cache[sec]

    first_us = next((int.from_bytes(i[1], "big") for t, k, i in events
                     if k == "meta" and i[0] == 0x51 and len(i[1]) == 3), 500000)
    base_bpm = 60_000_000 / first_us
    bpm = next((float(f[6:]) for f in flags if f.startswith("--bpm=")), base_bpm)
    if not 20 <= bpm <= 300:
        sys.exit("--bpm must be between 20 and 300.")
    body, length = get(name)
    print(f"Style tempo {base_bpm:.1f} BPM, playing at {bpm:.1f} BPM.")

    if "--notes" in flags:
        counts = {}
        for _, m in body:
            if m[0] & 0xF0 == 0x90 and m[2] > 0:
                key = ((m[0] & 0x0F) + 1, m[1])
                counts[key] = counts.get(key, 0) + 1
        kits = [m[1] for _, m in setup if m[0] == 0xC9]
        banks = [(m[1], m[2]) for _, m in setup if m[0] == 0xB9 and m[1] in (0, 32)]
        print(f"\nDrum kit chosen by the style on channel 10: program {kits[-1] if kits else 'none'}"
              f"{'  bank (MSB/LSB) ' + str(banks) if banks else ''}")
        print("  (General MIDI kits: 0 Standard, 8 Room, 16 Power, 24 Electronic, 25 TR-808, 32 Jazz, 40 Brush,"
              " 48 Orchestra, 56 SFX. 48 and 56 have no normal kick: try --kit=0)")
        print("\nNote-ons per channel in this section (note number x count):")
        for ch in sorted({c for c, _ in counts}):
            items = sorted((n, c) for (cc, n), c in counts.items() if cc == ch)
            print(f"  ch {ch}: " + ", ".join(f"{n}x{c}" for n, c in items))
            if ch in (9, 10):
                if not any(n in (35, 36) for n, _ in items):
                    print("      no kick note (35/36) on this channel; kick may be on the other drum channel or on another note")
                odd = [n for n, _ in items if not 35 <= n <= 81]
                if odd:
                    print(f"      notes outside the General MIDI drum range 35-81 "
                          f"(Windows synth may be silent on these): {odd}")
        return
    sf2 = next((f[6:] for f in flags if f.startswith("--sf2=")), SF2)
    if "--synth=fluid" in flags:
        out = FluidOut("--dry" in flags, sf2)
    else:
        out = Out("--dry" in flags)
    held, stats = set(), [0.0, 0, 0]            # worst delay, late count, notes sent
    live = "--live" in flags
    try:
        for _, m in setup:                      # voices/volumes first
            out.send(m)
        t0 = time.perf_counter() + 0.2
        if not live:
            print(f"Playing '{name}': {len(body)} messages, loop length {length * base_bpm / bpm:.2f} s. "
                  f"Ctrl+C to stop.")
            k = base_bpm / bpm
            for _ in range(1 if out.dry else 10**9):
                t0 = play_segment(out, [(a * k, m) for a, m in body], length * k, t0, stats, held)
        else:
            num, den = 4, 4
            for t, kk, i in events:
                if kk == "meta" and i[0] == 0x58 and len(i[1]) >= 2:
                    num, den = i[1][0], 2 ** i[1][1]
                    break
            quarter = 60.0 / base_bpm
            run_live(out, get, secs, quarter, quarter * 4 * num / den, base_bpm, bpm, name, stats, held)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        out.close()
        if not out.dry and stats[2]:
            print(f"Timing: {stats[2]} notes sent, worst delay {stats[0] * 1000:.1f} ms, "
                  f"{stats[1]} later than 15 ms.")


if __name__ == "__main__":
    main()
