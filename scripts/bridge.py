#!/usr/bin/env python3
"""Mirror Herdr agent state on a QMK keyboard over USB MIDI.

Stdlib only: talks to Herdr's Unix socket (newline-delimited JSON-RPC) and
sends MIDI via the platform backend — the ALSA rawmidi device node on Linux,
CoreMIDI through ctypes on macOS. No compiled binary, no dependencies.
"""

import ctypes
import glob
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time

MIDI_CHANNEL = 0xBE  # MIDI channel 15 (status channels are zero-based)
CC_WORKSPACE_PREV = 100
CC_WORKSPACE_NEXT = 101
CC_TAB_PREV = 102
CC_TAB_NEXT = 103
CC_PANE_LEFT = 104
CC_PANE_DOWN = 105
CC_PANE_UP = 106
CC_PANE_RIGHT = 107
CC_AGENT_PICKER = 108
CC_SCRATCHPAD = 109
CC_HEARTBEAT = 110
CC_STATE = 111
CC_SLOT_FIRST = 112
CC_ACCEPT = 124
CC_REJECT = 125
CC_PROMPT = 126
CC_CLEAR = 127
PROTOCOL = 1
EMPTY_SLOT = 7
SLOT_COUNT = 4
HEARTBEAT_SECONDS = 1.0
MIDI_POLL_SECONDS = 0.05
COMMAND_TIMEOUT_SECONDS = 2.0
MIDI_PACKET_DATA_SIZE = 256

STATUS_CODES = {"idle": 0, "working": 1, "blocked": 2, "done": 3, "unknown": 4}
STATUS_PRIORITY = ["blocked", "working", "done", "unknown", "idle"]

CARD_RE = re.compile(r"\s*(\d+)\s*\[\s*(\S+)\s*\]\s*:\s*(\S+)\s+-\s+(.*)$")


def log(message):
    print(f"qmk-herdr: {message}", file=sys.stderr, flush=True)


class BridgeError(Exception):
    pass


def build_packet_list(message):
    """Pack one MIDI message as CoreMIDI's 4-byte-aligned MIDIPacketList."""
    message = bytes(message)
    if len(message) > MIDI_PACKET_DATA_SIZE:
        raise ValueError("MIDI packet exceeds 256 bytes")
    packet = struct.pack("<QH", 0, len(message))
    packet += message.ljust(MIDI_PACKET_DATA_SIZE, b"\x00") + b"\x00\x00"
    return struct.pack("<I", 1) + packet


class MidiOut:
    """Minimal send-side MIDI interface shared by the platform backends."""

    def __init__(self, name):
        self.name = name

    def heartbeat(self):
        self.send(CC_HEARTBEAT, PROTOCOL)

    def send(self, control, value):
        raise NotImplementedError

    def receive(self):
        return []

    def close(self):
        pass


class MidiParser:
    """Parse a MIDI byte stream, including running status and realtime bytes."""

    def __init__(self):
        self.status = None
        self.data = bytearray()
        self.in_sysex = False

    def feed(self, chunk):
        messages = []
        for byte in chunk:
            if byte >= 0xF8:
                continue
            if self.in_sysex:
                if byte == 0xF7:
                    self.in_sysex = False
                continue
            if byte & 0x80:
                self.data.clear()
                if byte == 0xF0:
                    self.in_sysex = True
                    self.status = None
                elif byte >= 0xF0:
                    self.status = None
                else:
                    self.status = byte
                continue
            if self.status is None:
                continue
            self.data.append(byte)
            length = 1 if self.status & 0xF0 in (0xC0, 0xD0) else 2
            if len(self.data) == length:
                if self.status & 0xF0 == 0xB0:
                    messages.append((self.status, *self.data))
                self.data.clear()
        return messages


class AlsaMidiOut(MidiOut):
    """Duplex handle on the ALSA rawmidi node of a USB MIDI card."""

    def __init__(self, name):
        super().__init__(name)
        self.fd = None
        self.parser = MidiParser()

    def open(self):
        needle = self.name.lower()
        cards = []
        try:
            with open("/proc/asound/cards") as cards_file:
                for line in cards_file:
                    match = CARD_RE.match(line.rstrip("\n"))
                    if not match:
                        continue
                    index, _card_id, _module, long_name = match.groups()
                    cards.append((int(index), long_name.strip()))
        except OSError as error:
            raise BridgeError(f"cannot list ALSA cards: {error}") from error
        matches = [c for c in cards if needle in c[1].lower()]
        if not matches:
            available = ", ".join(c[1] for c in cards) or "none"
            raise BridgeError(
                f"no ALSA MIDI card matching {self.name!r}; available: {available}"
            )
        index, long_name = matches[0]
        nodes = sorted(glob.glob(f"/dev/snd/midiC{index}D*"))
        if not nodes:
            raise BridgeError(f"card {long_name!r} exposes no rawmidi device")
        fd = None
        try:
            fd = os.open(nodes[0], os.O_RDWR | os.O_NONBLOCK)
            self.fd = fd
        except OSError as error:
            if fd is not None:
                os.close(fd)
            raise BridgeError(f"cannot open {nodes[0]}: {error}") from error

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.parser = MidiParser()

    def send(self, control, value):
        if self.fd is None:
            raise BridgeError("MIDI device is closed")
        message = bytes((MIDI_CHANNEL, control, value))
        if os.write(self.fd, message) != len(message):
            raise BridgeError("incomplete MIDI write")

    def receive(self):
        if self.fd is None:
            raise BridgeError("MIDI device is closed")
        try:
            return self.parser.feed(os.read(self.fd, 4096))
        except BlockingIOError:
            return []


class CoreMidiOut(MidiOut):
    """macOS backend: sends MIDIPacketLists through CoreMIDI via ctypes."""

    UTF8 = 0x08000100  # kCFStringEncodingUTF8

    _cm: ctypes.CDLL
    _cf: ctypes.CDLL
    _client = 0
    _port = 0
    _endpoint = 0

    def __init__(self, name):
        super().__init__(name)

    def _cf_string(self, text):
        self._cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self._cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        return self._cf.CFStringCreateWithCString(
            None, text.encode("utf-8"), self.UTF8
        )

    def _display_name(self, midi_object, selector):
        out = ctypes.c_void_p()
        status = self._cm.MIDIObjectGetStringProperty(
            midi_object, selector, ctypes.byref(out)
        )
        if status != 0:
            return ""
        buffer = ctypes.create_string_buffer(256)
        ok = self._cf.CFStringGetCString(out, buffer, 256, self.UTF8)
        self._cf.CFRelease(out)
        return buffer.value.decode("utf-8") if ok else ""

    def _declare_signatures(self):
        cm, cf = self._cm, self._cf
        cm.MIDIClientCreate.restype = ctypes.c_int32
        cm.MIDIClientCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        cm.MIDIOutputPortCreate.restype = ctypes.c_int32
        cm.MIDIOutputPortCreate.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        cm.MIDIGetNumberOfDestinations.restype = ctypes.c_uint32
        cm.MIDIGetDestination.restype = ctypes.c_uint32
        cm.MIDIGetDestination.argtypes = [ctypes.c_uint32]
        cm.MIDIObjectGetStringProperty.restype = ctypes.c_int32
        cm.MIDIObjectGetStringProperty.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        cm.MIDISend.restype = ctypes.c_int32
        cm.MIDISend.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_char_p,
        ]
        cm.MIDIClientDispose.restype = ctypes.c_int32
        cm.MIDIClientDispose.argtypes = [ctypes.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFStringGetCString.restype = ctypes.c_ubyte
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]

    def open(self):
        try:
            self._cm = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreMIDI.framework/CoreMIDI"
            )
            self._cf = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        except OSError as error:
            raise BridgeError(f"cannot load CoreMIDI: {error}") from error
        self._declare_signatures()
        cm, cf = self._cm, self._cf

        client = ctypes.c_uint32()
        client_name = self._cf_string("qmk-herdr")
        status = cm.MIDIClientCreate(client_name, None, None, ctypes.byref(client))
        cf.CFRelease(client_name)
        if status != 0:
            raise BridgeError(f"MIDIClientCreate failed: {status}")
        self._client = client.value
        port = ctypes.c_uint32()
        port_name = self._cf_string("qmk-herdr")
        status = cm.MIDIOutputPortCreate(client, port_name, ctypes.byref(port))
        cf.CFRelease(port_name)
        if status != 0:
            self.close()
            raise BridgeError(f"MIDIOutputPortCreate failed: {status}")
        self._port = port.value

        selector = ctypes.c_void_p.in_dll(cm, "kMIDIPropertyDisplayName")
        needle = self.name.lower()
        names = []
        endpoint = 0
        for index in range(cm.MIDIGetNumberOfDestinations()):
            candidate = cm.MIDIGetDestination(index)
            name = self._display_name(candidate, selector)
            names.append(name)
            if not endpoint and needle in name.lower():
                endpoint = candidate
        if not endpoint:
            available = ", ".join(names) or "none"
            raise BridgeError(
                f"no CoreMIDI destination matching {self.name!r};"
                f" available: {available}"
            )
        self._endpoint = endpoint

    def close(self):
        if self._client:
            self._cm.MIDIClientDispose(self._client)
            self._client = 0
            self._port = 0
            self._endpoint = 0

    def send(self, control, value):
        if not self._endpoint:
            raise BridgeError("MIDI destination is closed")
        packet_list = build_packet_list((MIDI_CHANNEL, control, value))
        status = self._cm.MIDISend(self._port, self._endpoint, packet_list)
        if status != 0:
            raise BridgeError(f"MIDISend failed: {status}")


class RtMidiOut(MidiOut):
    """Use a named CoreMIDI/ALSA sequencer port via python-rtmidi."""

    def __init__(self, name):
        super().__init__(name)
        self.midi_out = None
        self.midi_in = None
        self.opened = False

    def open(self):
        if self.midi_out is None:
            try:
                import rtmidi
            except ImportError as error:
                raise BridgeError("rtmidi backend requires python-rtmidi") from error
            self.midi_out = rtmidi.MidiOut()
            self.midi_in = rtmidi.MidiIn()

        output_ports = self.midi_out.get_ports()
        input_ports = self.midi_in.get_ports()
        output_match = next(
            (index for index, port in enumerate(output_ports) if self.name.lower() in port.lower()),
            None,
        )
        input_match = next(
            (index for index, port in enumerate(input_ports) if self.name.lower() in port.lower()),
            None,
        )
        if output_match is None or input_match is None:
            raise BridgeError(
                f"no duplex sequencer MIDI port matching {self.name!r}; "
                f"outputs: {', '.join(output_ports) or 'none'}; "
                f"inputs: {', '.join(input_ports) or 'none'}"
            )
        try:
            self.midi_out.open_port(output_match)
            self.midi_in.open_port(input_match)
            self.midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        except Exception as error:
            self.close()
            raise BridgeError(f"cannot open sequencer port matching {self.name!r}: {error}") from error
        self.opened = True

    def close(self):
        if self.midi_out is not None:
            self.midi_out.close_port()
        if self.midi_in is not None:
            self.midi_in.close_port()
        self.midi_out = None
        self.midi_in = None
        self.opened = False

    def send(self, control, value):
        if self.midi_out is None or not self.opened:
            raise BridgeError("MIDI sequencer destination is closed")
        self.midi_out.send_message([MIDI_CHANNEL, control, value])

    def receive(self):
        if self.midi_in is None or not self.opened:
            raise BridgeError("MIDI sequencer source is closed")
        messages = []
        while True:
            event = self.midi_in.get_message()
            if event is None:
                return messages
            message, _delta = event
            if len(message) == 3 and message[0] & 0xF0 == 0xB0:
                messages.append(tuple(message))


class FallbackMidiOut(MidiOut):
    """Use the first available destination from a pipe-separated target list."""

    def __init__(self, targets):
        super().__init__(" | ".join(target.name for target in targets))
        self.targets = targets
        self.active = None

    def open(self):
        errors = []
        for target in self.targets:
            try:
                target.open()
                self.active = target
                return
            except Exception as error:
                target.close()
                errors.append(str(error))
        raise BridgeError("; ".join(errors))

    def close(self):
        if self.active is not None:
            self.active.close()
            self.active = None

    def send(self, control, value):
        if self.active is None:
            raise BridgeError("all MIDI destinations are closed")
        self.active.send(control, value)

    def receive(self):
        if self.active is None:
            raise BridgeError("all MIDI destinations are closed")
        return self.active.receive()


def make_midi_out(name):
    if "|" in name:
        targets = [make_midi_out(target.strip()) for target in name.split("|") if target.strip()]
        if not targets:
            raise BridgeError("MIDI target list is empty")
        return FallbackMidiOut(targets)
    if name.startswith("rtmidi:"):
        target = name.removeprefix("rtmidi:").strip()
        if not target:
            raise BridgeError("rtmidi target is empty")
        return RtMidiOut(target)
    if sys.platform == "darwin":
        return CoreMidiOut(name)
    return AlsaMidiOut(name)


def run_herdr(*args):
    try:
        process = subprocess.run(
            ["herdr", *args], capture_output=True, text=True, check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise BridgeError(f"herdr {' '.join(args)} timed out") from error
    if process.returncode:
        message = process.stderr.strip() or process.stdout.strip() or "unknown error"
        raise BridgeError(f"herdr {' '.join(args)} failed: {message}")
    try:
        response = json.loads(process.stdout)
        return response["result"]
    except (ValueError, KeyError, TypeError) as error:
        raise BridgeError(f"malformed herdr response: {error}") from error


def read_clipboard():
    commands = [
        ("wl-paste", "--no-newline"),
        ("xclip", "-selection", "clipboard", "-o"),
        ("pbpaste",),
    ]
    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            process = subprocess.run(
                command, capture_output=True, text=True, check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            continue
        if process.returncode == 0 and process.stdout.strip():
            return process.stdout.rstrip("\x00")
    raise BridgeError("prompt requires non-empty clipboard text")


class HerdrController:
    def __init__(self, tracker, command=run_herdr, clipboard=read_clipboard):
        self.tracker = tracker
        self.command = command
        self.clipboard = clipboard

    def _focused_workspace(self):
        workspaces = self.command("workspace", "list")["workspaces"]
        return next((item for item in workspaces if item.get("focused")), None)

    def _focused_pane(self):
        workspace = self._focused_workspace()
        if workspace is None:
            raise BridgeError("Herdr has no focused workspace")
        panes = self.command("pane", "list", "--workspace", workspace["workspace_id"])["panes"]
        pane = next((item for item in panes if item.get("focused")), None)
        if pane is None:
            raise BridgeError("Herdr has no focused pane")
        return pane

    def _cycle(self, kind, key, delta, workspace=None):
        args = [kind, "list"]
        if workspace is not None:
            args += ["--workspace", workspace]
        items = sorted(self.command(*args)[f"{kind}s"], key=lambda item: item["number"])
        if not items:
            raise BridgeError(f"Herdr has no {kind}s")
        current = next((index for index, item in enumerate(items) if item.get("focused")), None)
        if current is None:
            raise BridgeError(f"Herdr has no focused {kind}")
        target = items[(current + delta) % len(items)][key]
        self.command(kind, "focus", target)

    def handle(self, control, value):
        if value != 127:
            return False
        if control in (CC_WORKSPACE_PREV, CC_WORKSPACE_NEXT):
            self._cycle(
                "workspace",
                "workspace_id",
                -1 if control == CC_WORKSPACE_PREV else 1,
            )
        elif control in (CC_TAB_PREV, CC_TAB_NEXT):
            workspace = self._focused_workspace()
            if workspace is None:
                raise BridgeError("Herdr has no focused workspace")
            self._cycle(
                "tab",
                "tab_id",
                -1 if control == CC_TAB_PREV else 1,
                workspace["workspace_id"],
            )
        elif CC_PANE_LEFT <= control <= CC_PANE_RIGHT:
            directions = {
                CC_PANE_LEFT: "left",
                CC_PANE_DOWN: "down",
                CC_PANE_UP: "up",
                CC_PANE_RIGHT: "right",
            }
            pane = self._focused_pane()
            self.command(
                "pane", "focus", "--direction", directions[control], "--pane", pane["pane_id"]
            )
        elif control == CC_AGENT_PICKER:
            self.command("plugin", "action", "invoke", "open", "--plugin", "lancodev.jump")
        elif control == CC_SCRATCHPAD:
            self.command("plugin", "action", "invoke", "toggle", "--plugin", "herdr-floax")
        elif control in (CC_ACCEPT, CC_REJECT, CC_CLEAR):
            keys = {CC_ACCEPT: "enter", CC_REJECT: "esc", CC_CLEAR: "ctrl+c"}
            self.command("agent", "send-keys", self._focused_pane()["pane_id"], keys[control])
        elif control == CC_PROMPT:
            self.command(
                "agent", "prompt", self._focused_pane()["pane_id"], self.clipboard()
            )
        else:
            return False
        return True

    def poll(self, midi):
        for status, control, value in midi.receive():
            if status != MIDI_CHANNEL:
                continue
            try:
                if self.handle(control, value):
                    log(f"control CC {control}")
            except Exception as error:
                log(f"control CC {control} failed: {error}")


class Tracker:
    def __init__(self):
        self.slots = [None] * SLOT_COUNT
        self.previous = {}

    def update(self, agents, notify):
        agents = sorted(
            agents, key=lambda a: (a.get("state_change_seq", 0), a["pane_id"])
        )
        live = {a["pane_id"] for a in agents}
        self.slots = [s if s in live else None for s in self.slots]
        for agent in agents:
            pid = agent["pane_id"]
            if pid in self.slots:
                continue
            if None in self.slots:
                self.slots[self.slots.index(None)] = pid

        current = {a["pane_id"]: a["agent_status"] for a in agents}

        def changed_to(status):
            return notify and any(
                now == status
                and pid in self.previous
                and self.previous[pid] != status
                for pid, now in current.items()
            )

        def any_is(status):
            return any(a["agent_status"] == status for a in agents)

        aggregate = next(
            (s for s in STATUS_PRIORITY if any_is(s)), "idle"
        )
        frame = {
            "aggregate": aggregate,
            "slots": [
                STATUS_CODES.get(current[s], EMPTY_SLOT)
                if s is not None
                else EMPTY_SLOT
                for s in self.slots
            ],
            "any_working": any_is("working"),
            "overflow": len(agents) > SLOT_COUNT,
            "chime_done": changed_to("done"),
            "chime_blocked": changed_to("blocked"),
        }
        self.previous = current
        return frame

    def send_frame(self, midi, frame):
        midi.heartbeat()
        for index, status in enumerate(frame["slots"]):
            midi.send(CC_SLOT_FIRST + index, status)
        value = STATUS_CODES.get(frame["aggregate"], 4)
        value |= frame["any_working"] << 3
        value |= frame["overflow"] << 4
        value |= frame["chime_done"] << 5
        value |= frame["chime_blocked"] << 6
        midi.send(CC_STATE, value)


def snapshot(socket_path):
    request = json.dumps(
        {"id": "qmk-herdr-snapshot", "method": "session.snapshot", "params": {}}
    ).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(HEARTBEAT_SECONDS)
        sock.connect(socket_path)
        sock.sendall(request + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise BridgeError("snapshot connection closed")
            data += chunk
        try:
            response = json.loads(data.split(b"\n", 1)[0])
            return response["result"]["snapshot"]["agents"]
        except (ValueError, KeyError, TypeError) as error:
            raise BridgeError(f"malformed snapshot response: {error}") from error


def subscription_request(agents):
    subscriptions = [
        {"type": "pane.agent_detected"},
        {"type": "pane.closed"},
        {"type": "pane.exited"},
    ] + [
        {"type": "pane.agent_status_changed", "pane_id": a["pane_id"]}
        for a in agents
    ]
    return json.dumps(
        {
            "id": "qmk-herdr-subscribe",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
    ).encode()


def watch_session(socket_path, midi, tracker, controller):
    initial = snapshot(socket_path)
    subscribed = {a["pane_id"] for a in initial}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(socket_path)
        sock.settimeout(HEARTBEAT_SECONDS)
        sock.sendall(subscription_request(initial) + b"\n")

        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise BridgeError("event stream closed before ack")
            data += chunk
        ack_line, data = data.split(b"\n", 1)
        try:
            ack = json.loads(ack_line)
        except ValueError as error:
            raise BridgeError(f"malformed subscription ack: {error}") from error
        if ack.get("result", {}).get("type") != "subscription_started":
            raise BridgeError(f"Herdr rejected event subscription: {ack}")

        agents = snapshot(socket_path)
        changed = {a["pane_id"] for a in agents} != subscribed
        tracker.send_frame(midi, tracker.update(agents, notify=False))
        last_heartbeat = time.monotonic()
        if changed:
            raise BridgeError("Herdr agent set changed; resubscribing")

        sock.settimeout(MIDI_POLL_SECONDS)
        while True:
            controller.poll(midi)
            while b"\n" in data:
                line, data = data.split(b"\n", 1)
                if not line.strip():
                    continue
                agents = snapshot(socket_path)
                changed = {a["pane_id"] for a in agents} != subscribed
                tracker.send_frame(midi, tracker.update(agents, notify=True))
                last_heartbeat = time.monotonic()
                if changed:
                    raise BridgeError("Herdr agent set changed; resubscribing")
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                    midi.heartbeat()
                    last_heartbeat = time.monotonic()
                continue
            if not chunk:
                raise BridgeError("Herdr event stream closed")
            data += chunk


def run(socket_path, port_name):
    midi = make_midi_out(port_name)
    tracker = Tracker()
    controller = HerdrController(tracker)
    last_error = None
    midi_unavailable = True
    while True:
        opened = False
        try:
            midi.open()
            opened = True
            if midi_unavailable:
                log(f"connected to MIDI matching {port_name!r}")
            midi_unavailable = False
            watch_session(socket_path, midi, tracker, controller)
        except Exception as error:  # any failure becomes a logged retry, never a dead daemon
            midi.close()
            if not opened:
                midi_unavailable = True
            message = f"{error}; reconnecting"
            if message != last_error:
                log(message)
                last_error = message
            time.sleep(1)


def self_test():
    tracker = Tracker()
    first = tracker.update(
        [
            {"pane_id": "p2", "agent_status": "working", "state_change_seq": 2},
            {"pane_id": "p1", "agent_status": "idle", "state_change_seq": 1},
        ],
        notify=False,
    )
    assert first["slots"] == [0, 1, EMPTY_SLOT, EMPTY_SLOT], first
    assert not first["chime_done"]

    second = tracker.update(
        [
            {"pane_id": "p2", "agent_status": "done", "state_change_seq": 3},
            {"pane_id": "p1", "agent_status": "blocked", "state_change_seq": 4},
        ],
        notify=True,
    )
    assert second["slots"] == [2, 3, EMPTY_SLOT, EMPTY_SLOT], second
    assert second["aggregate"] == "blocked"
    assert second["chime_done"] and second["chime_blocked"], second

    overflow = tracker.update(
        [
            {"pane_id": f"p{i}", "agent_status": "idle", "state_change_seq": i}
            for i in range(1, 6)
        ],
        notify=True,
    )
    assert overflow["overflow"] and overflow["slots"] == [0, 0, 0, 0], overflow

    request = subscription_request([{"pane_id": "w1:p1"}])
    assert request.count(b'"type"') == 4
    assert b'"pane_id": "w1:p1"' in request

    parser = MidiParser()
    assert parser.feed((MIDI_CHANNEL, CC_WORKSPACE_PREV)) == []
    assert parser.feed((0xF8, 127, CC_WORKSPACE_NEXT, 127)) == [
        (MIDI_CHANNEL, CC_WORKSPACE_PREV, 127),
        (MIDI_CHANNEL, CC_WORKSPACE_NEXT, 127),
    ]
    assert parser.feed((0xF0, 1, 2, 0xF7, MIDI_CHANNEL, CC_ACCEPT, 127)) == [
        (MIDI_CHANNEL, CC_ACCEPT, 127)
    ]
    assert parser.feed((0xF1, 1, MIDI_CHANNEL, CC_REJECT, 127)) == [
        (MIDI_CHANNEL, CC_REJECT, 127)
    ]

    expected = [MIDI_CHANNEL, CC_HEARTBEAT, PROTOCOL]
    packet_list = build_packet_list(expected)
    num_packets, stamp, length = struct.unpack_from("<IQH", packet_list, 0)
    assert num_packets == 1
    assert stamp == 0
    assert length == 3
    assert list(packet_list[14:17]) == expected
    assert len(packet_list) == 4 + 8 + 2 + MIDI_PACKET_DATA_SIZE + 2

    frame = dict(overflow)
    frame.update(aggregate="done", any_working=False, overflow=False, chime_done=True, chime_blocked=False)

    class FakeMidi:
        def __init__(self):
            self.sent = []

        def heartbeat(self):
            self.sent.append("hb")

        def send(self, control, value):
            self.sent.append((control, value))

    fake = FakeMidi()
    tracker.send_frame(fake, frame)
    assert fake.sent[0] == "hb"
    value = STATUS_CODES["done"] | 1 << 5
    expected_state = (CC_STATE, value)
    assert fake.sent[-1] == expected_state, fake.sent[-1]

    class FakeRtMidiOut:
        instances = []

        def __init__(self):
            self.opened = None
            self.sent = []
            self.closed = False
            self.instances.append(self)

        def get_ports(self):
            return ["unrelated", "qmk-herdr-ipad"]

        def open_port(self, index):
            self.opened = index

        def send_message(self, message):
            self.sent.append(message)

        def close_port(self):
            self.closed = True

    class FakeRtMidiIn:
        instances = []

        def __init__(self):
            self.opened = None
            self.events = [([MIDI_CHANNEL, CC_ACCEPT, 127], 0.0)]
            self.closed = False
            self.instances.append(self)

        def get_ports(self):
            return ["unrelated", "qmk-herdr-ipad"]

        def open_port(self, index):
            self.opened = index

        def ignore_types(self, **kwargs):
            pass

        def get_message(self):
            return self.events.pop(0) if self.events else None

        def close_port(self):
            self.closed = True

    previous_rtmidi = sys.modules.get("rtmidi")
    sys.modules["rtmidi"] = type(
        "FakeRtMidiModule", (), {"MidiOut": FakeRtMidiOut, "MidiIn": FakeRtMidiIn}
    )
    try:
        output = make_midi_out("rtmidi:HERDR-IPAD")
        output.open()
        output.send(CC_HEARTBEAT, PROTOCOL)
        assert output.receive() == [(MIDI_CHANNEL, CC_ACCEPT, 127)]
        output.close()
        output_instance = FakeRtMidiOut.instances[-1]
        input_instance = FakeRtMidiIn.instances[-1]
        assert output_instance.opened == 1 and input_instance.opened == 1
        assert output_instance.sent == [[MIDI_CHANNEL, CC_HEARTBEAT, PROTOCOL]]
        assert output_instance.closed and input_instance.closed

        missing = make_midi_out("rtmidi:missing")
        before = len(FakeRtMidiOut.instances)
        for _ in range(2):
            try:
                missing.open()
            except BridgeError:
                pass
            else:
                raise AssertionError("missing RtMidi port opened")
        assert len(FakeRtMidiOut.instances) == before + 1
    finally:
        if previous_rtmidi is None:
            del sys.modules["rtmidi"]
        else:
            sys.modules["rtmidi"] = previous_rtmidi

    commands = []

    def fake_herdr(*args):
        if args == ("workspace", "list"):
            return {"workspaces": [
                {"workspace_id": "w1", "number": 1, "focused": True},
                {"workspace_id": "w2", "number": 2, "focused": False},
            ]}
        if args == ("tab", "list", "--workspace", "w1"):
            return {"tabs": [
                {"tab_id": "w1:t1", "number": 1, "focused": True},
                {"tab_id": "w1:t2", "number": 2, "focused": False},
            ]}
        if args == ("pane", "list", "--workspace", "w1"):
            return {"panes": [{"pane_id": "w1:p1", "focused": True}]}
        if args == ("agent", "list"):
            return {"agents": [{"pane_id": "w1:p1"}, {"pane_id": "w2:p3"}]}
        commands.append(args)
        return {}

    tracker.slots = ["w1:p2", None, None, None]
    controller = HerdrController(tracker, command=fake_herdr, clipboard=lambda: "test prompt")
    assert not controller.handle(CC_ACCEPT, 0)
    for control in (
        CC_WORKSPACE_PREV, CC_WORKSPACE_NEXT, CC_TAB_PREV, CC_TAB_NEXT,
        CC_PANE_LEFT, CC_PANE_DOWN, CC_PANE_UP, CC_PANE_RIGHT,
        CC_AGENT_PICKER, CC_SCRATCHPAD, CC_ACCEPT, CC_REJECT, CC_PROMPT, CC_CLEAR,
    ):
        assert controller.handle(control, 127)
    assert ("workspace", "focus", "w2") in commands
    assert ("tab", "focus", "w1:t2") in commands
    assert ("plugin", "action", "invoke", "open", "--plugin", "lancodev.jump") in commands
    assert ("plugin", "action", "invoke", "toggle", "--plugin", "herdr-floax") in commands
    assert ("agent", "prompt", "w1:p1", "test prompt") in commands
    assert ("agent", "send-keys", "w1:p1", "enter") in commands
    assert ("agent", "send-keys", "w1:p1", "esc") in commands
    assert ("agent", "send-keys", "w1:p1", "ctrl+c") in commands

    fallback = make_midi_out("Planck EZ|rtmidi:qmk-herdr-ipad")
    assert isinstance(fallback, FallbackMidiOut)
    assert isinstance(fallback.targets[0], CoreMidiOut if sys.platform == "darwin" else AlsaMidiOut)
    assert isinstance(fallback.targets[1], RtMidiOut)
    print("self-test ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
        return
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        log("HERDR_SOCKET_PATH is missing; run qmk-herdr inside a Herdr pane")
        sys.exit(1)
    run(socket_path, sys.argv[1] if len(sys.argv) > 1 else "Moonlander|Planck EZ")


if __name__ == "__main__":
    main()
