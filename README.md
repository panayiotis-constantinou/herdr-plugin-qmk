# QMK Herdr

A Herdr plugin that mirrors agent state on a QMK keyboard over USB MIDI. A single stdlib-Python script (`scripts/bridge.py`) is the whole bridge: it talks to Herdr's local socket and sends MIDI through the platform backend — the ALSA rawmidi device node on Linux, CoreMIDI via ctypes on macOS. No compiled binary, no build step, no dependencies.

## Requirements

- Linux or macOS with the QMK keyboard connected over USB. The keyboard firmware must expose the USB MIDI interface.
- Linux writes to the matching `/dev/snd/midiC*D*` device node (granted to the active seat user by default).
- macOS sends through CoreMIDI. `python3` ships with the Xcode Command Line Tools; override the interpreter with `HERDR_QMK_PYTHON` if needed.

## Install

```sh
herdr plugin install panayiotis-constantinou/herdr-plugin-qmk
```

Restart Herdr after installing so the startup hook runs. The bridge matches the first MIDI device whose name contains `Planck EZ` (ALSA card on Linux, CoreMIDI destination on macOS).

For a different device name, write the case-insensitive substring to the plugin config directory, then restart the bridge:

```sh
printf '%s\n' 'Moonlander' > "$(herdr plugin config-dir panayiotis.qmk-herdr)/midi-port"
herdr plugin action invoke restart --plugin panayiotis.qmk-herdr
```

Useful actions:

```sh
herdr plugin action invoke status --plugin panayiotis.qmk-herdr
herdr plugin action invoke restart --plugin panayiotis.qmk-herdr
herdr plugin action invoke stop --plugin panayiotis.qmk-herdr
```

## Develop

```sh
python3 scripts/bridge.py --self-test
herdr plugin link --enabled .
herdr plugin action invoke restart --plugin panayiotis.qmk-herdr
```

The process log is `qmk-herdr.log` under `HERDR_PLUGIN_STATE_DIR`; Herdr exposes action output with `herdr plugin log`.

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
