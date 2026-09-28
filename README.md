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
- **Blocked agents** get one request per blocked episode: a Choice of why the agent is waiting (permission prompt, question, or error) and a Score of how risky approving it is. The keyboard blinks each blocked slot in a rhythm for its reason, and colors Accept green, peach, or red for the focused agent's permission prompt; high risk makes Accept need a double tap. Any real chance of a destructive action counts as high risk even when TypeSafe is unsure.
- **Score** maintains an attention order from agent task metadata. While slots are sorted by criticality, it breaks same-status LED-slot ties and makes note 122 visit the most useful agent next; status priority and the no-TypeSafe order remain deterministic.

Requests include agent metadata such as pane ID, project path, title, and status. The only terminal output sent is the last 40 lines of a **blocked** agent (`herdr agent read --source recent`), once per blocked episode; clipboard text and the output of agents that are not blocked are never sent. Without an API key, or when ranking fails, existing local behavior continues; completion-chime failures use the deterministic fallback after the two-second request timeout.

## iPad over RTP-MIDI

The Mosh connection does not carry USB MIDI. Run an RTP-MIDI bridge on the Herdr host and route MIDI in both directions on the iPad:

```text
qmk-herdr ↔ rtpmidid ↔ RTP-MIDI app ↔ midimittr ↔ Planck EZ
```

1. Grant **RTP-MIDI (Network MIDI)** Local Network access, enable its session, set the connection policy to **In Contacts**, and add the Herdr host on UDP port `5004`. Leave the session enabled, but do not initiate a connection to the host from the iPad: **the host must initiate**. If the host is already connected from the iPad's Contacts list, disconnect it there first. Two simultaneous invitations can cause `Invitation Rejected (NO)`.
2. In the free **midimittr** app, route `Network Session 1` → Planck EZ for LEDs and Planck EZ → `Network Session 1` for controls. Do not route either endpoint back to itself; midimittr advertises background operation.
3. Keep Tailscale connected. RTP-MIDI is unencrypted and uses adjacent UDP control/data ports `5004` and `5005`, so restrict both to the intended peer. Configure `midi-port` to **only** the per-peer `rtmidi:` port exposed by rtpmidid (not a local-keyboard fallback).

Configure the host's RTP-MIDI bridge to connect to the iPad over Tailscale, and add the host's Tailscale address to the iPad app's contacts on UDP port `5004`. A LAN-only peer address will not work away from home. iPadOS may suspend network or MIDI apps in the background; verify recovery after locking the screen and changing networks rather than assuming background operation.

Flash the matching QMK firmware: its Herdr layer sends Note On/Off 100–111 and 116–127 on channel 15 instead of F13–F24, and only counts protocol 2 heartbeats as a connection. The RTP-MIDI connection is duplex; an LED-only route cannot carry keyboard controls. Flashing this firmware replaces the old Herdr Web F-key controls.

Useful actions:

```sh
herdr plugin action invoke status --plugin panayiotis.qmk-herdr
herdr plugin action invoke restart --plugin panayiotis.qmk-herdr
herdr plugin action invoke stop --plugin panayiotis.qmk-herdr
```

### Verify the complete path

A running plugin or a bridge log saying `connected to MIDI` only proves the local MIDI endpoint opened. It does **not** prove the RTP peer, iPad routing, or keyboard is connected. The matching firmware echoes each protocol heartbeat as CC 116 value 2 on channel 15; `no keyboard heartbeat echo` in the bridge log means the end-to-end round trip has been absent for five seconds. The warning clears when echoes resume; the bridge does not repeatedly restart a healthy local MIDI port to compensate for a sleeping iPad.

1. Check `systemctl --user status rtpmidid-qmk-herdr` and `journalctl --user -u rtpmidid-qmk-herdr -n 30`. Repeated control-port timeouts mean the iPad session is not reachable; fix that before debugging LEDs. Repeated `Invitation Rejected (NO)` means the iPad is reachable but refuses fractal: either it already holds its own session to fractal (it dialed out, which rtpmidid exposes as an unused `iPad` port), or its policy does not match fractal. In the RTP-MIDI app, disconnect the host if the iPad initiated the session, leave the iPad's own session enabled, and check that the contact uses the host's Tailscale address and port `5004`; the host's next retry (every 30 seconds) should then connect.
2. With the Planck connected to the iPad, enable both midimittr routes. Its bottom-center LED should turn from dim red to dim green when matching heartbeats arrive (enable RGB first).
3. In a disposable Herdr workspace, use previous/next tab on the keyboard's Herdr layer. The remote session must change tabs: this checks the return path, not just LED output.
4. Observe working/blocked/done feedback and speaker cues with sounds enabled. Stop the iPad MIDI route: the Planck's bottom-center LED should turn red and its slot LEDs go dark within five seconds, and the bridge should warn after five seconds. Restore the route and check for `keyboard heartbeat echo restored`.
5. Repeat after locking/unlocking the iPad, changing Wi-Fi/cellular, unplugging/replugging the Planck, and restarting rtpmidid. After each recovery, verify both LED updates **and** a harmless previous/next tab action; if either fails, check Tailscale, the RTP-MIDI session, and both midimittr routes.

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
| 110 | Focus the previous live agent in the same order as note 122 |
| 111 | Focus the most urgent agent: the top of the TypeSafe attention order, or blocked, then working, then done agents without a confident ranking |
| 116 | Create and focus a workspace rooted at the focused pane's directory |
| 117 | Create and focus a tab in the focused workspace and directory |
| 118 | Toggle LazyGit in a split pane |
| 119 | Open the plugin command palette |
| 120 | Toggle zoom for the focused pane |
| 121 | Open the worktree diff in a Hunk tab |
| 122 | Focus the next live agent in TypeSafe attention order, or Herdr's list order without a confident ranking |
| 123 | Open the command palette (sent by older firmware; current firmware uses 119) |
| 124 | Send Enter to the agent in the focused pane |
| 125 | Send Escape to the agent in the focused pane |
| 126 | Submit clipboard text directly to the focused agent (no TypeSafe request) |
| 127 | Send Ctrl-C to the agent in the focused pane |

Plugin-backed controls require Lancodev Jump, Herdr Floax, Herdr LazyGit, the command palette, and Hunk Diff to be installed and enabled.

Linux ALSA rawmidi and `rtmidi:` targets are duplex. Direct CoreMIDI remains status-output only; use an `rtmidi:` target on macOS for keyboard controls.

## Keyboard display

- Planck EZ bottom-center LED (Moonlander: past the jiggler indicator): dim green connected, dim red disconnected.
- Four Planck EZ outer-bottom LEDs (Moonlander: left number row): the first four agents in the keyboard's sort mode, first on the left. Criticality order puts blocked before working, done, unknown, and idle, and TypeSafe can rank same-status agents by attention value; recency order puts the latest state change first. Previous/next agent (notes 110/122) walk the same order; most urgent (note 111) always follows criticality.
- Each agent on the board gets its own color (blue, green, peach, or mauve) and keeps it while it stays there. Idle is dim, working breathes, blocked blinks in a rhythm for its reason, done is bright, and unknown shows white.
- Sort-mode LEDs (Planck EZ: the small LEDs below the space bar; Moonlander: the first small LED on each half): left lit for criticality, right lit for recency, both dark while disconnected. Toggle the mode with the top-left key of the Herdr layer.
- The keyboard speaker always cues connection and blocked transitions; TypeSafe can suppress low-value done cues.

The keyboard stops the working animation if bridge heartbeats time out.

## Firmware

The matching Miryoku firmware lives in the [QMK fork](https://github.com/panayiotis-constantinou/qmk_firmware) under `keyboards/zsa/planck_ez/keymaps/manna-harbour_miryoku` and the equivalent Moonlander keymap. The local Panix checkout is `~/Projects/qmk_firmware`; shared protocol handling is in `users/manna-harbour_miryoku/herdr.c`. Uncommitted firmware changes must be included in the build; a stock/Oryx image does not implement this protocol. Per-key RGB requires the Planck EZ **Glow** variant.

Status uses MIDI channel 15 CC messages, not SysEx: CC 110 value 2 is the heartbeat, CC 111 carries the aggregate state/flags, and CC 112–115 carry the four agent slots (bits 0–2 status, bits 3–4 blocked reason: 0 unknown, 1 permission, 2 question, 3 error, bits 5–6 the agent's color index), and CC 117 carries the focused agent's approval risk (0 none, 1 pending, 2 unknown, 3–5 low/medium/high). Older firmware ignores the extra bits and CC 117. The firmware returns CC 116 value 2 as a round-trip receipt, followed by CC 118 with its sort mode (0 criticality, 1 recency), also sent when the mode is toggled; without CC 118 the bridge sorts by criticality. The firmware renders RGB and plays speaker cues locally; the server does not stream audio over MIDI.

Build both with:

```sh
qmk compile -kb zsa/planck_ez/glow -km manna-harbour_miryoku
qmk compile -kb zsa/moonlander -km manna-harbour_miryoku
```
