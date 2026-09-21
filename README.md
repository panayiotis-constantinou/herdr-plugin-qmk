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

## Optional TypeSafe automation

The bridge remains fully deterministic unless `TYPESAFE_API_KEY` is set. To enable the optional AI layer, export that variable before starting Herdr or store it in the plugin config directory:

```sh
config=$(herdr plugin config-dir panayiotis.qmk-herdr)
printf '%s' "$TYPESAFE_API_KEY" > "$config/typesafe-api-key"
chmod 600 "$config/typesafe-api-key"
herdr plugin action invoke restart --plugin panayiotis.qmk-herdr
```

`TYPESAFE_MODEL` optionally overrides the default `jev-latest` model. When enabled, TypeSafe runs bounded typed judgments in background threads:

- **Choice** routes CC 126 clipboard prompts to the most relevant live agent; low-confidence or failed requests fall back to the focused agent, while an explicit no-match leaves the clipboard unsent.
- **Noul** decides whether a completed-agent transition merits a sound. Blocked-agent sounds remain deterministic and immediate.
- **Score** breaks semantic ties between agents of the same status when more than four compete for LED slots; status priority remains deterministic.

Requests may include up to 4,000 characters of clipboard text plus agent metadata such as pane ID, project path, title, status, and workspace label. Terminal scrollback and agent conversations are not sent. Without an API key, or when ranking fails, existing local behavior continues; prompt and completion-chime failures use the deterministic fallback after the two-second request timeout.

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
| ---: | --- |
| 100–101 | Focus previous/next workspace, wrapping by displayed number |
| 102–103 | Focus previous/next tab in the focused workspace, wrapping by displayed number |
| 104–107 | Focus the pane left/down/up/right of the focused pane |
| 108 | Open the Lancodev Jump workspace/agent picker |
| 109 | Toggle the Herdr Floax floating scratch shell |
| 116 | Create and focus a workspace rooted at the focused pane's directory |
| 117 | Create and focus a tab in the focused workspace and directory |
| 118 | Toggle LazyGit in a split pane |
| 119 | Open the plugin command palette |
| 120 | Toggle zoom for the focused pane |
| 121 | Open the worktree diff in a Hunk tab |
| 124 | Send Enter to the agent in the focused pane |
| 125 | Send Escape to the agent in the focused pane |
| 126 | Submit clipboard text to the TypeSafe-selected agent, or the focused agent without TypeSafe |
| 127 | Send Ctrl-C to the agent in the focused pane |

Plugin-backed controls require Lancodev Jump, Herdr Floax, Herdr LazyGit, the command palette, and Hunk Diff to be installed and enabled.

Linux ALSA rawmidi and `rtmidi:` targets are duplex. Direct CoreMIDI remains status-output only; use an `rtmidi:` target on macOS for keyboard controls.

## Keyboard display

- Six center Planck EZ LEDs, or the six innermost covered Moonlander keys: orange comet while any agent is working.
- Planck EZ bottom-center LED: red disconnected, amber blocked, blue working, green done, dim white idle, purple unknown/overflow.
- Four Planck EZ outer-bottom LEDs: stable status-prioritized slots; TypeSafe can rank same-status overflow.
- Caps Lock, Scroll Lock, and mouse-jiggler indicators return when the spinner is idle.
- The Planck EZ speaker always cues connection and blocked transitions; TypeSafe can suppress low-value done cues.

The keyboard stops the working animation if bridge heartbeats time out.

## Firmware

The matching Miryoku firmware lives in the QMK fork under the Planck EZ and Moonlander keymaps.

Build both with:

```sh
qmk compile -kb zsa/planck_ez/glow -km manna-harbour_miryoku
qmk compile -kb zsa/moonlander -km manna-harbour_miryoku
```
