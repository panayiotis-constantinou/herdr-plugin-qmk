# QMK Herdr

A Herdr plugin that mirrors agent state on a QMK keyboard and turns the keyboard's Herdr layer into session controls over MIDI. A single Python script (`scripts/bridge.py`) talks to Herdr's local socket and uses ALSA rawmidi on Linux, CoreMIDI on macOS, or an optional RtMidi sequencer port for network bridges.

## Requirements

- A QMK keyboard exposing USB MIDI, connected to the host or to the iPad via a duplex RTP-MIDI bridge.
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

- **Choice** caches implementation, review, research, planning, operations, documentation, or general role tags when agent metadata changes. Roles improve routing and attention ranking but never alter agent state.
- **Noul** batches nearby completed-agent transitions and decides whether one sound is useful. Blocked-agent sounds remain deterministic and immediate.
- **Score** maintains an attention order from agent task metadata. It breaks same-status LED-slot ties and makes note 122 visit the most useful agent next; status priority and the no-TypeSafe order remain deterministic.

Requests include agent metadata such as pane ID, project path, title, and status; clipboard text, terminal scrollback, and agent conversations are not sent. Without an API key, or when ranking fails, existing local behavior continues; completion-chime failures use the deterministic fallback after the two-second request timeout.

## iPad over RTP-MIDI

The Mosh connection does not carry USB MIDI. Run an RTP-MIDI bridge on the Herdr host and route MIDI in both directions on the iPad:

```text
qmk-herdr ↔ rtpmidid ↔ RTP-MIDI app ↔ midimittr ↔ Planck EZ
```

1. Grant **RTP-MIDI (Network MIDI)** Local Network access, enable its session, set the connection policy to **In Contacts**, and add/select the Herdr host on UDP port `5004`.
2. In the free **midimittr** app, route `Network Session 1` → Planck EZ for LEDs and Planck EZ → `Network Session 1` for controls. Do not route either endpoint back to itself; midimittr advertises background operation.
3. Keep Tailscale connected. RTP-MIDI is unencrypted and uses adjacent UDP control/data ports `5004` and `5005`, so restrict both to the intended peer. Configure `rtmidi:` with the per-peer sequencer port name exposed by rtpmidid.

On Panix/fractal, `rtpmidid-qmk-herdr.service` initiates the connection to the iPad's Tailscale address (`100.64.0.3:5004`) under the stable peer name `qmk-herdr-ipad`. Add fractal (`100.64.0.1:5004`) to the iPad app's contacts and allow incoming connections from it. Keep the iPad session enabled and Tailscale connected; a LAN-only peer address will not work away from home. The Panix `midi-port` setting intentionally selects only `rtmidi:qmk-herdr-ipad`, not a keyboard attached to the server.

Flash the matching QMK firmware: its Herdr layer sends Note On/Off 100–109 and 116–127 on channel 15 instead of F13–F24, and only counts protocol 2 heartbeats as a connection. The RTP-MIDI connection is duplex; an LED-only route cannot carry keyboard controls. Flashing this firmware replaces the old Herdr Web F-key controls.

Useful actions:

```sh
herdr plugin action invoke status --plugin panayiotis.qmk-herdr
herdr plugin action invoke restart --plugin panayiotis.qmk-herdr
herdr plugin action invoke stop --plugin panayiotis.qmk-herdr
```

### Verify the complete path

A running plugin or a bridge log saying `connected to MIDI` only proves the local MIDI endpoint opened. It does **not** prove the RTP peer, iPad routing, or keyboard is connected.

1. Check `systemctl --user status rtpmidid-qmk-herdr` and `journalctl --user -u rtpmidid-qmk-herdr -n 30`. Repeated control-port timeouts mean the iPad session is not reachable; fix that before debugging LEDs.
2. With the Planck connected to the iPad, enable both midimittr routes. Its bottom-center LED should leave disconnected red when matching heartbeats arrive (enable RGB first).
3. In a disposable Herdr workspace, use previous/next tab on the keyboard's Herdr layer. The remote session must change tabs: this checks the return path, not just LED output.
4. Observe working/blocked/done feedback and speaker cues with sounds enabled. Stop the iPad MIDI route: the Planck should turn red and stop the spinner within five seconds. Restore the route and verify recovery, including while the terminal app is foregrounded.

## Develop

```sh
python3 scripts/bridge.py --self-test
herdr plugin link --enabled .
herdr plugin action invoke restart --plugin panayiotis.qmk-herdr
```

The process log is `qmk-herdr.log` under `HERDR_PLUGIN_STATE_DIR`; Herdr exposes action output with `herdr plugin log`.

## Keyboard controls

The bridge uses MIDI channel 15 and dispatches Note On with velocity `127`; Note Off is ignored. Notes avoid CC 100/101 (RPN select) and CC 120–127 (channel mode messages), which MIDI routers may filter or act on. Status still goes to the keyboard as CC 110–115.

| Note | Action |
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
| 122 | Focus the next live agent in TypeSafe attention order, or Herdr's list order without a confident ranking |
| 123 | Open the command palette |
| 124 | Send Enter to the agent in the focused pane |
| 125 | Send Escape to the agent in the focused pane |
| 126 | Submit clipboard text directly to the focused agent (no TypeSafe request) |
| 127 | Send Ctrl-C to the agent in the focused pane |

Plugin-backed controls require Lancodev Jump, Herdr Floax, Herdr LazyGit, the command palette, and Hunk Diff to be installed and enabled.

Linux ALSA rawmidi and `rtmidi:` targets are duplex. Direct CoreMIDI remains status-output only; use an `rtmidi:` target on macOS for keyboard controls.

## Keyboard display

- Six center Planck EZ LEDs, or the six innermost covered Moonlander keys: orange comet while any agent is working.
- Planck EZ bottom-center LED: red disconnected, amber blocked, blue working, green done, dim white idle, bright white unknown/overflow.
- Four Planck EZ outer-bottom LEDs: stable status-prioritized slots; TypeSafe can rank same-status agents by attention value.
- Caps Lock, Scroll Lock, and mouse-jiggler indicators return when the spinner is idle.
- The keyboard speaker always cues connection and blocked transitions; TypeSafe can suppress low-value done cues.

The keyboard stops the working animation if bridge heartbeats time out.

## Firmware

The matching Miryoku firmware lives in the [QMK fork](https://github.com/panayiotis-constantinou/qmk_firmware) under `keyboards/zsa/planck_ez/keymaps/manna-harbour_miryoku` and the equivalent Moonlander keymap. The local Panix checkout is `~/Projects/qmk_firmware`; shared protocol handling is in `users/manna-harbour_miryoku/herdr.c`. Uncommitted firmware changes must be included in the build; a stock/Oryx image does not implement this protocol. Per-key RGB requires the Planck EZ **Glow** variant.

Status uses MIDI channel 15 CC messages, not SysEx: CC 110 value 2 is the heartbeat, CC 111 carries the aggregate state/flags, and CC 112–115 carry the four agent slots. The firmware renders RGB and plays speaker cues locally; the server does not stream audio over MIDI.

Build both with:

```sh
qmk compile -kb zsa/planck_ez/glow -km manna-harbour_miryoku
qmk compile -kb zsa/moonlander -km manna-harbour_miryoku
```
