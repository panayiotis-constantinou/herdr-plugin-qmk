# QMK Herdr

A Herdr plugin that mirrors agent state on a QMK keyboard and turns the keyboard's Herdr layer into session controls over MIDI. A single Python script (`scripts/bridge.py`) talks to Herdr's local socket and uses ALSA rawmidi on Linux, CoreMIDI on macOS, or an optional RtMidi sequencer port for network bridges.

## Requirements

- One or more QMK keyboards exposing USB MIDI, connected to the host or to the iPad via a duplex RTP-MIDI bridge.
- Linux talks to each keyboard's `/dev/snd/midiC*D*` rawmidi node (granted to the active seat user by default).
- Python 3.11+ is required for reading Herdr's TOML config.
- macOS finds keyboards through `python-rtmidi`; without it, it can only send status to CoreMIDI destinations named in `midi-port`, which cannot claim. `python3` ships with the Xcode Command Line Tools; override the interpreter with `HERDR_QMK_PYTHON` if needed.
- The optional `rtmidi:` backend requires `python-rtmidi`; USB operation on Linux remains dependency-free. Set `HERDR_QMK_PYTHON`, or write the Python executable path to the plugin config file named `python`.

## Install

```sh
herdr plugin install panayiotis-constantinou/herdr-plugin-qmk
```

Restart Herdr after installing so the startup hook runs.

### Several keyboards

The bridge finds keyboards on its own: every second it looks for USB MIDI keyboards (ALSA cards on Linux, duplex CoreMIDI ports on macOS) and sends each new one a heartbeat. Ports that answer with the Herdr echo within five seconds are kept; other MIDI gear is left alone until it is unplugged. Every keyboard mirrors the same slots, risk, and connection state, so any of them is safe to pick up.

One keyboard is **active**: it plays the chimes and can toggle Herdr's panel between Priority and Grouped. All keyboards mirror the panel's mode and first four agents; previous/next walks the full panel order. The others are **standby**, shown with a dim blue connection LED. To switch, press any Herdr control on a standby keyboard: that first press only makes it active and does nothing else, so it can never approve or reject on stale state. Sort on a standby keyboard only claims it, like any control; Sound stays local and never claims a board.

The bridge remembers the last active keyboard across restarts in `recent-boards` under the plugin state directory. If the active keyboard is unplugged or stops echoing, the most recently used remaining keyboard takes over, and the first one stays standby when it returns. `herdr plugin action invoke status --plugin panayiotis.qmk-herdr` lists the keyboards and marks the active one.

Keyboards reached through a sequencer port, such as the iPad over RTP-MIDI, cannot be found by probing; name them in `midi-port` with an `rtmidi:` prefix, separated by `|`. They then take part like any other keyboard:

```sh
printf '%s\n' 'rtmidi:qmk-herdr-ipad' > "$(herdr plugin config-dir panayiotis.qmk-herdr)/midi-port"
herdr plugin action invoke restart --plugin panayiotis.qmk-herdr
```

Plain device names from older configs (`Moonlander|Planck EZ`) are ignored with a log line on Linux.

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
- **Score** maintains an attention order for the most-urgent-agent key only. It never overrides panel/LED order or previous/next navigation.

Requests include agent metadata such as pane ID, project path, title, and status. The only terminal output sent is the last 40 lines of a **blocked** agent (`herdr agent read --source recent`), once per blocked episode; clipboard text and the output of agents that are not blocked are never sent. Without an API key, or when ranking fails, existing local behavior continues; completion-chime failures use the deterministic fallback after the two-second request timeout.

## iPad over RTP-MIDI

The Mosh connection does not carry USB MIDI. Run an RTP-MIDI bridge on the Herdr host and route MIDI in both directions on the iPad:

```text
qmk-herdr ↔ rtpmidid ↔ RTP-MIDI app ↔ midimittr ↔ Planck EZ
```

1. Grant **RTP-MIDI (Network MIDI)** Local Network access, enable its session, set the connection policy to **In Contacts**, and add the Herdr host on UDP port `5004`. Leave the session enabled, but do not initiate a connection to the host from the iPad: **the host must initiate**. If the host is already connected from the iPad's Contacts list, disconnect it there first. Two simultaneous invitations can cause `Invitation Rejected (NO)`.
2. In the free **midimittr** app, route `Network Session 1` → Planck EZ for LEDs and Planck EZ → `Network Session 1` for controls. Do not route either endpoint back to itself; midimittr advertises background operation.
3. Keep Tailscale connected. RTP-MIDI is unencrypted and uses adjacent UDP control/data ports `5004` and `5005`, so restrict both to the intended peer. Name the per-peer `rtmidi:` port exposed by rtpmidid in `midi-port`; USB keyboards on the host still work alongside it.

Configure the host's RTP-MIDI bridge to connect to the iPad over Tailscale, and add the host's Tailscale address to the iPad app's contacts on UDP port `5004`. A LAN-only peer address will not work away from home. iPadOS may suspend network or MIDI apps in the background; verify recovery after locking the screen and changing networks rather than assuming background operation.

Flash the matching QMK firmware: its Herdr layer sends Note On/Off 100–111 and 116–127 on channel 15 instead of F13–F24, and only counts protocol 3 heartbeats as a connection. The RTP-MIDI connection is duplex; an LED-only route cannot carry keyboard controls. Flashing this firmware replaces the old Herdr Web F-key controls.

Useful actions:

```sh
herdr plugin action invoke status --plugin panayiotis.qmk-herdr
herdr plugin action invoke restart --plugin panayiotis.qmk-herdr
herdr plugin action invoke stop --plugin panayiotis.qmk-herdr
```

### Verify the complete path

An open `rtmidi:` port only proves the local MIDI endpoint exists. It does **not** prove the RTP peer, iPad routing, or keyboard is connected. The matching firmware echoes each protocol heartbeat as CC 116 value 3 on channel 15, and the bridge logs `found rtmidi:…` on the first echo; `no heartbeat echo from rtmidi:…` means the end-to-end round trip has been absent for five seconds. The warning clears when echoes resume; the bridge does not repeatedly restart a healthy local MIDI port to compensate for a sleeping iPad.

1. Check `systemctl --user status rtpmidid-qmk-herdr` and `journalctl --user -u rtpmidid-qmk-herdr -n 30`. Repeated control-port timeouts mean the iPad session is not reachable; fix that before debugging LEDs. Repeated `Invitation Rejected (NO)` means the iPad is reachable but refuses fractal: either it already holds its own session to fractal (it dialed out, which rtpmidid exposes as an unused `iPad` port), or its policy does not match fractal. In the RTP-MIDI app, disconnect the host if the iPad initiated the session, leave the iPad's own session enabled, and check that the contact uses the host's Tailscale address and port `5004`; the host's next retry (every 30 seconds) should then connect.
2. With the Planck connected to the iPad, enable both midimittr routes. Its space bar LED should turn from dim red to dim green when matching heartbeats arrive (enable RGB first).
3. In a disposable Herdr workspace, use previous/next tab on the keyboard's Herdr layer. The remote session must change tabs: this checks the return path, not just LED output.
4. Observe working/blocked/done feedback and speaker cues with sounds enabled. Stop the iPad MIDI route: the Planck's space bar LED should turn red and its slot LEDs go dark within five seconds, and the bridge should warn after five seconds. Restore the route and check for `keyboard heartbeat echo restored`.
5. Repeat after locking/unlocking the iPad, changing Wi-Fi/cellular, unplugging/replugging the Planck, and restarting rtpmidid. After each recovery, verify both LED updates **and** a harmless previous/next tab action; if either fails, check Tailscale, the RTP-MIDI session, and both midimittr routes.

## Develop

```sh
python3 scripts/bridge.py --self-test
python3 scripts/test_panel_sort.py
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
| 122 | Focus the next live agent in Herdr's built-in panel order |
| 123 | Open the command palette (sent by older firmware; current firmware uses 119) |
| 124 | Send Enter to the agent in the focused pane |
| 125 | Send Escape to the agent in the focused pane |
| 126 | Submit clipboard text directly to the focused agent (no TypeSafe request) |
| 127 | Send Ctrl-C to the agent in the focused pane |

Plugin-backed controls require Lancodev Jump, Herdr Floax, Herdr LazyGit, the command palette, and Hunk Diff to be installed and enabled.

Linux ALSA rawmidi and `rtmidi:` targets are duplex. Direct CoreMIDI remains status-output only; use an `rtmidi:` target on macOS for keyboard controls.

## Keyboard display

- Planck EZ space bar LED (Moonlander: the right's spare key beside K): dim green for the active keyboard, dim blue for a standby one, dim red disconnected.
- Four agent slots (Planck EZ: the center block's top two rows, top left first; Moonlander: the left's spare column beside the index finger, top to bottom): the first four agent entries in Herdr's built-in panel order, excluding group headers. Priority puts blocked, done, working, idle, then unknown first, with newest state changes first within each status and stable layout-order ties. Grouped follows workspace/tab/pane layout order. Previous/next (notes 110/122) walks the full list; most urgent (111) retains its independent urgency/AI behavior.
- Each agent on the board gets its own color (blue, green, peach, or mauve) and keeps it while it stays there. Idle is dim, working breathes, blocked blinks in a rhythm for its reason, done is bright, and unknown is steady at half brightness in its assigned color.
- Sort mode on one RGB LED (Planck EZ: the inner bottom-left LED 37, with Scroll Lock moved to center-block LED 29; Moonlander: the right's innermost number-row key): dim yellow for Priority, dim teal for Grouped, dark while disconnected. All boards mirror Herdr. The active board's top-left Herdr-layer key requests a toggle; LEDs change only when the bridge acknowledges it.
- The active keyboard's speaker always cues blocked transitions, and every keyboard cues its own connection; TypeSafe can suppress low-value done cues.

The keyboard stops the working animation if bridge heartbeats time out.

## Sorting synchronization

The bridge watches `HERDR_CONFIG_PATH` (default `~/.config/herdr/config.toml`). Herdr's panel toggle persists `ui.agent_panel_sort`; keyboard toggles atomically update that setting and call `herdr server reload-config`. On reconnect Herdr's saved mode wins. Reloading config reapplies other saved settings too. A config shared by multiple Herdr sessions shares the persisted mode.

The config must be writable and not a symlink. Panix's Home Manager activation converts its generated config into a writable file and preserves only the panel-sort preference across generations. Invalid configs and failed reloads are reported; the keyboard does not invent an independent mode. A keyboard toggle also clears any agent view, such as a `herdr-projects focus`, because a view hides the panel sort. A view set elsewhere cannot be read back, so the keyboard keeps showing the panel's own order until the view is cleared.

Protocol 3 is required on **both** bridge and keyboards: protocol 2 boards are deliberately not considered connected, avoiding old heartbeat sort echoes overwriting Herdr's mode. Do not restart the new bridge until the new firmware has been flashed.

## Colors in Herdr

The bridge publishes each agent's name, the same label as Herdr's built-in `agent` field, as pane metadata (source `qmk-herdr`). Agents on the board carry it in the token for their keyboard color, `$qmk_blue`, `$qmk_green`, `$qmk_peach`, or `$qmk_mauve`; agents off the board carry it in `$qmk_agent`. Herdr styles tokens per occurrence, not per value, so each agent carries exactly one of the five and the others are cleared. Names expire after 30 seconds unless the bridge refreshes them, so a stopped bridge leaves no stale colors.

Put the five tokens where the built-in `agent` would go, with the firmware's Catppuccin hues:

```toml
[ui.sidebar.agents]
rows = [
  ["workspace", "tab"],
  [
    "state_icon",
    { token = "$qmk_blue", fg = "#89b4fa" },
    { token = "$qmk_green", fg = "#a6e3a1" },
    { token = "$qmk_peach", fg = "#fab387" },
    { token = "$qmk_mauve", fg = "#cba6f7" },
    "$qmk_agent"
  ],
]
```

Without the bridge running, this row shows no agent names; keep the built-in `agent` on machines without a keyboard.

## Firmware

The matching Miryoku firmware lives in the [QMK fork](https://github.com/panayiotis-constantinou/qmk_firmware) under `keyboards/zsa/planck_ez/keymaps/manna-harbour_miryoku` and the equivalent Moonlander keymap. The local Panix checkout is `~/Projects/qmk_firmware`; shared protocol handling is in `users/manna-harbour_miryoku/herdr.c`. Uncommitted firmware changes must be included in the build; a stock/Oryx image does not implement this protocol. Per-key RGB requires the Planck EZ **Glow** variant.

Status uses MIDI channel 15 CC messages, not SysEx: CC 110 value 3 is the heartbeat, CC 111 carries the aggregate state/flags, and CC 112–115 carry the four agent slots (bits 0–2 status, bits 3–4 blocked reason: 0 unknown, 1 permission, 2 question, 3 error, bits 5–6 the agent's color index), and CC 117 carries the focused agent's approval risk (0 none, 1 pending, 2 unknown, 3–5 low/medium/high). Older firmware ignores the extra bits and CC 117. The firmware returns CC 116 value 3 as a round-trip receipt. CC 118 is duplex: the bridge sends the current mode (0 Priority, 1 Grouped) to all boards, and a keyboard sends only toggle requests, never heartbeat echoes. The active keyboard's request changes the mode; a standby keyboard's only claims it. Mirroring mode updates does not write EEPROM. After each heartbeat the bridge sends CC 119: 1 to the active keyboard, 0 to standby ones, whose CC 111 carries no chime flags. Older firmware ignores CC 119 and stays green; the bridge still swallows its claiming press. The firmware renders RGB and plays speaker cues locally; the server does not stream audio over MIDI.

Build both with:

```sh
qmk compile -kb zsa/planck_ez/glow -km manna-harbour_miryoku
qmk compile -kb zsa/moonlander -km manna-harbour_miryoku
```
