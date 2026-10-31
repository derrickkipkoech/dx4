DX ARRANGER FOR WINDOWS (no Python needed)
===========================================
START:   double-click Start-DX-Arranger.bat   (or DX-Arranger.exe)
PHONE:   double-click Start-for-phone.bat -> it prints an address and a PIN for your phone/tablet.
         Allow Windows Firewall for PRIVATE networks when asked.

FIRST TIME
 * Windows may say "Windows protected your PC" (the program is not code-signed):
   click More info > Run anyway.
 * FluidSynth and a SoundFont are needed for the good sound. Open config.json (created on first
   run) and set "fluid_exe" and "soundfont" to your own files. Without them the Windows
   built-in synth is used.
 * Put .STY files in the Styles\P01..P16 folders, or import them from the panel.

Everything else works as described in README.txt.

USING A VST DRUM PLUGIN (e.g. MT Power Drum Kit) INSTEAD OF FLUIDSYNTH
 1. Install loopMIDI (free) and create one port (default name "loopMIDI Port").
 2. Open the plugin's standalone program, or a VST host (SAVIHost, Reaper...), and set its MIDI input to that port.
 3. In config.json set:  "synth": "midiout"   and   "midi_out_name": "loopMIDI"
    (the start-up window lists every MIDI output; use any part of the name you want).
 4. Start DX Arranger. EFX, reverb/chorus and master volume are then the plugin's job; MUTE still silences it.

PLAYING THE VST DIRECTLY (no FluidSynth, no loopMIDI)
 1. Copy MT-PowerDrumKit.vst3 next to DX-Arranger.exe (or put its full path in "vst_path").
 2. Run MT Power Drum Kit's standalone ONCE and point it at your sound content folder (it remembers this).
 3. config.json:  "synth": "vst"
 4. Start the app. The window prints "VST plugin ready", or a warning if the plugin makes no sound.
 If it crackles raise "vst_block" to 1024; if the beat feels late try 256.
