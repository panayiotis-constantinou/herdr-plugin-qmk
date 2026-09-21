# QMK Herdr

A Herdr plugin that mirrors agent state on a QMK keyboard and turns the keyboard's Herdr layer into session controls over MIDI. A single Python script (`scripts/bridge.py`) talks to Herdr's local socket and uses ALSA rawmidi on Linux, CoreMIDI on macOS, or an optional RtMidi sequencer port for network bridges.

## Requirements

- Linux or macOS with the QMK keyboard connected over USB. The keyboard firmware must expose the USB MIDI interface.
- Linux writes to the matching `/dev/snd/midiC*D*` device node (granted to the active seat user by default).
- macOS sends through CoreMIDI. `python3` ships with the Xcode Command Line Tools; override the interpreter with `HERDR_QMK_PYTHON` if needed.
- The optional `rtmidi:` backend requires `python-rtmidi`; direct USB operation remains dependency-free. Set `HERDR_QMK_PYTHON`, or write the Python executable path to the plugin config file named `python`.

## Install

```sh
herdr plugin install panayiotis-constantinou/herdr-plugin-qmk
```

Restart Herdr after installing so the startup hook runs. The bridge uses the first available MIDI device whose name contains `Moonlander` or `Planck EZ` (ALSA card on Linux, CoreMIDI destination on macOS).

For a different device name, write the case-insensitive substring to the plugin config directory, then restart the bridge:

```sh
printf '%s\n' 'Moonlander' > "$(herdr plugin config-dir panayiotis.qmk-herdr)/midi-port"
herdr plugin action invoke restart --plugin panayiotis.qmk-herdr
```

Prefix an ALSA sequencer/CoreMIDI destination with `rtmidi:`. Separate targets with `|` to use the first available one:

```sh
printf '%s\n' 'Planck EZ|rtmidi:qmk-herdr-ipad' > "$(herdr plugin config-dir panayiotis.qmk-herdr)/midi-port"
```

## iPad over RTP-MIDI

The Mosh connection does not carry USB MIDI. Run an RTP-MIDI bridge on the Herdr host and make this one-way route on the iPad:

```text
qmk-herdr → rtpmidid → RTP-MIDI app → midimittr → Planck EZ
```

1. Grant **RTP-MIDI (Network MIDI)** Local Network access, enable its session, set the connection policy to **In Contacts**, and add/select the Herdr host on UDP port `5004`.
2. In the free **midimittr** app, enable `Network Session 1` only as a source and the Planck EZ only as a destination. midimittr advertises background operation.
3. Keep Tailscale connected. RTP-MIDI is unencrypted and uses adjacent UDP control/data ports `5004` and `5005`, so restrict both to the intended peer. Configure `rtmidi:` with the per-peer sequencer port name exposed by rtpmidid.

The keyboard firmware receives the same MIDI CC protocol as local USB operation, so its LEDs, controls, and onboard speaker need no network-specific mode.

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

## Keyboard controls

The bridge uses MIDI channel 15 and dispatches only CC value `127`:

| CC | Action |
|---:|---|
| 100–101 | Focus previous/next workspace, wrapping by displayed number |
| 102–103 | Focus previous/next tab in the focused workspace, wrapping by displayed number |
| 104–107 | Focus the pane left/down/up/right of the focused pane |
| 108 | Open the Lancodev Jump workspace/agent picker |
| 109 | Toggle the Herdr Floax floating scratch shell |
| 124 | Send Enter to the agent in the focused pane |
| 125 | Send Escape to the agent in the focused pane |
| 126 | Submit non-empty clipboard text to the agent in the focused pane |
| 127 | Send Ctrl-C to the agent in the focused pane |

Linux ALSA rawmidi and `rtmidi:` targets are duplex. Direct CoreMIDI remains status-output only; use an `rtmidi:` target on macOS for keyboard controls.

## Keyboard display

- Six center Planck EZ LEDs, or the six innermost covered Moonlander keys: orange comet while any agent is working.
- Planck EZ bottom-center LED: red disconnected, amber blocked, blue working, green done, dim white idle, purple unknown/overflow.
- Four Planck EZ outer-bottom LEDs: stable slots for the first four live agents.
- Caps Lock, Scroll Lock, and mouse-jiggler indicators return when the spinner is idle.
- The Planck EZ speaker plays one coalesced cue for connection, blocked, and done transitions.

The keyboard stops the working animation if bridge heartbeats time out.

## Firmware

The matching Miryoku firmware lives in the QMK fork under the Planck EZ and Moonlander keymaps.

Build both with:

```sh
qmk compile -kb zsa/planck_ez/glow -km manna-harbour_miryoku
qmk compile -kb zsa/moonlander -km manna-harbour_miryoku
```
