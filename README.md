# qmk-herdr

A small foreground bridge that mirrors the current Herdr session on a Planck EZ Glow running the matching QMK keymap. It uses Herdr's local socket API and USB MIDI: no daemon, system service, or Herdr plugin.

## Run

Start it from a shell pane inside Herdr so `HERDR_SOCKET_PATH` is available:

```sh
nix run
```

The first MIDI output containing `Planck EZ` is selected. Pass a different case-insensitive substring when needed:

```sh
nix run . -- "Planck EZ Glow"
```

The process stays in the foreground; stop it with Ctrl-C.

## Install

With Nix:

```sh
nix profile install .
```

Without Nix, download the archive for Linux or macOS from a tagged GitHub release, unpack it, and place `qmk-herdr` on `PATH`. Release artifacts are produced after this local repository is published and a `v*` tag is pushed.

Linux builds use ALSA (`libasound`). macOS uses CoreMIDI.

## Keyboard display

- Six center LEDs: blue comet while any agent is working.
- Bottom-center LED: red disconnected, amber blocked, blue working, green done, dim white idle, purple unknown/overflow.
- Four outer-bottom LEDs: stable slots for the first four live agents.
- Caps Lock, Scroll Lock, and mouse-jiggler indicators return when the spinner is idle.
- The speaker plays one coalesced cue for connection, blocked, and done transitions.

The keyboard turns the status LED red if bridge heartbeats stop for three seconds.

## Firmware

The matching firmware lives in the QMK fork under:

```text
keyboards/zsa/planck_ez/keymaps/manna-harbour_miryoku
```

Build it with:

```sh
qmk compile -kb zsa/planck_ez/glow -km manna-harbour_miryoku
```
