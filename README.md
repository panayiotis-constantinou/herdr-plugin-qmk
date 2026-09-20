# QMK Herdr

A Herdr plugin that mirrors agent state on a QMK keyboard over MIDI. A single Python script (`scripts/bridge.py`) talks to Herdr's local socket and sends MIDI through ALSA rawmidi on Linux, CoreMIDI on macOS, or an optional RtMidi sequencer destination for network bridges.

## Requirements

- Linux or macOS with the QMK keyboard connected over USB. The keyboard firmware must expose the USB MIDI interface.
- Linux writes to the matching `/dev/snd/midiC*D*` device node (granted to the active seat user by default).
- macOS sends through CoreMIDI. `python3` ships with the Xcode Command Line Tools; override the interpreter with `HERDR_QMK_PYTHON` if needed.
- The optional `rtmidi:` backend requires `python-rtmidi`; direct USB operation remains dependency-free. Set `HERDR_QMK_PYTHON`, or write the Python executable path to the plugin config file named `python`.

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

The Planck firmware receives the same MIDI CC protocol as local USB operation, so its LEDs and onboard speaker need no network-specific mode.

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

The keyboard turns the status LED red if bridge heartbeats stop for the firmware-configured timeout.

## Firmware

The matching firmware lives in the QMK fork under:

```text
keyboards/zsa/planck_ez/keymaps/manna-harbour_miryoku
```

Build it with:

```sh
qmk compile -kb zsa/planck_ez/glow -km manna-harbour_miryoku
```
