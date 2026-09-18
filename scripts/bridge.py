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
import socket
import struct
import sys
import time

MIDI_CHANNEL = 0xBF
CC_HEARTBEAT = 110
CC_STATE = 111
CC_SLOT_FIRST = 112
PROTOCOL = 1
EMPTY_SLOT = 7
SLOT_COUNT = 4
HEARTBEAT_SECONDS = 1.0
MIDI_PACKET_DATA_SIZE = 256

STATUS_CODES = {"idle": 0, "working": 1, "blocked": 2, "done": 3, "unknown": 4}
STATUS_PRIORITY = ["blocked", "working", "done", "unknown", "idle"]

CARD_RE = re.compile(r"\s*(\d+)\s*\[\s*(\S+)\s*\]\s*:\s*(\S+)\s+-\s+(.*)$")


def log(message):
    print(f"qmk-herdr: {message}", file=sys.stderr, flush=True)


class BridgeError(Exception):
    pass


def build_packet_list(messages):
    """Pack MIDI messages as a MIDIPacketList: UInt32 count, then per packet
    UInt64 timestamp, UInt16 length, and a fixed 256-byte data buffer."""
    packets = b"".join(
        struct.pack("<QH", 0, len(message)) + bytes(message).ljust(
            MIDI_PACKET_DATA_SIZE, b"\x00"
        )
        for message in messages
    )
    return struct.pack("<I", len(messages)) + packets


class MidiOut:
    """Minimal send-side MIDI interface shared by the platform backends."""

    def __init__(self, name):
        self.name = name

    def heartbeat(self):
        self.send(CC_HEARTBEAT, PROTOCOL)

    def send(self, control, value):
        raise NotImplementedError

    def close(self):
        pass


class AlsaMidiOut(MidiOut):
    """Write-side handle on the ALSA rawmidi node of a USB MIDI card."""

    def __init__(self, name):
        super().__init__(name)
        self.fd = None

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
        try:
            self.fd = os.open(nodes[0], os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            raise BridgeError(f"cannot open {nodes[0]}: {error}") from error

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def send(self, control, value):
        if self.fd is None:
            raise BridgeError("MIDI device is closed")
        try:
            os.write(self.fd, bytes((MIDI_CHANNEL, control, value)))
        except BlockingIOError:
            pass  # ponytail: drop the message; next frame or heartbeat resends


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
        port = ctypes.c_uint32()
        port_name = self._cf_string("qmk-herdr")
        status = cm.MIDIOutputPortCreate(client, port_name, ctypes.byref(port))
        cf.CFRelease(port_name)
        if status != 0:
            raise BridgeError(f"MIDIOutputPortCreate failed: {status}")
        self._client = client.value
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
        packet_list = build_packet_list([(MIDI_CHANNEL, control, value)])
        status = self._cm.MIDISend(self._port, self._endpoint, packet_list)
        if status != 0:
            raise BridgeError(f"MIDISend failed: {status}")


def make_midi_out(name):
    if sys.platform == "darwin":
        return CoreMidiOut(name)
    return AlsaMidiOut(name)


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


def watch_session(socket_path, midi, tracker):
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
        try:
            ack = json.loads(data.split(b"\n", 1)[0])
        except ValueError as error:
            raise BridgeError(f"malformed subscription ack: {error}") from error
        if ack.get("result", {}).get("type") != "subscription_started":
            raise BridgeError(f"Herdr rejected event subscription: {ack}")

        agents = snapshot(socket_path)
        changed = {a["pane_id"] for a in agents} != subscribed
        tracker.send_frame(midi, tracker.update(agents, notify=False))
        if changed:
            raise BridgeError("Herdr agent set changed; resubscribing")

        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                midi.heartbeat()
                continue
            if not chunk:
                raise BridgeError("Herdr event stream closed")
            data += chunk
            while b"\n" in data:
                line, data = data.split(b"\n", 1)
                if not line.strip():
                    continue
                agents = snapshot(socket_path)
                changed = {a["pane_id"] for a in agents} != subscribed
                tracker.send_frame(midi, tracker.update(agents, notify=True))
                if changed:
                    raise BridgeError("Herdr agent set changed; resubscribing")


def run(socket_path, port_name):
    midi = make_midi_out(port_name)
    tracker = Tracker()
    while True:
        try:
            midi.open()
            log(f"connected to MIDI matching {port_name!r}")
            watch_session(socket_path, midi, tracker)
        except Exception as error:  # any failure becomes a logged retry, never a dead daemon
            midi.close()
            log(f"{error}; reconnecting")
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

    packet_list = build_packet_list([(MIDI_CHANNEL, CC_HEARTBEAT, PROTOCOL), (MIDI_CHANNEL, CC_STATE, 3)])
    (num_packets,) = struct.unpack_from("<I", packet_list, 0)
    assert num_packets == 2
    offset = 4
    for expected in ([MIDI_CHANNEL, CC_HEARTBEAT, PROTOCOL], [MIDI_CHANNEL, CC_STATE, 3]):
        stamp, length = struct.unpack_from("<QH", packet_list, offset)
        assert stamp == 0
        assert length == 3
        data = packet_list[offset + 10 : offset + 10 + 3]
        assert list(data) == expected
        offset += 10 + MIDI_PACKET_DATA_SIZE
    assert offset == len(packet_list)

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
    print("self-test ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
        return
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        log("HERDR_SOCKET_PATH is missing; run qmk-herdr inside a Herdr pane")
        sys.exit(1)
    run(socket_path, sys.argv[1] if len(sys.argv) > 1 else "Planck EZ")


if __name__ == "__main__":
    main()
