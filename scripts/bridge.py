#!/usr/bin/env python3
"""Mirror Herdr agent state on a QMK keyboard over USB MIDI (ALSA rawmidi).

Stdlib only: talks to Herdr's Unix socket (newline-delimited JSON-RPC) and
writes MIDI bytes straight to the ALSA rawmidi device node. No compiled
binary, no ALSA client libraries.
"""

import glob
import json
import os
import re
import socket
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

STATUS_CODES = {"idle": 0, "working": 1, "blocked": 2, "done": 3, "unknown": 4}
STATUS_PRIORITY = ["blocked", "working", "done", "unknown", "idle"]

CARD_RE = re.compile(r"\s*(\d+)\s*\[\s*(\S+)\s*\]\s*:\s*(\S+)\s+-\s*(.*)$")


def log(message):
    print(f"qmk-herdr: {message}", file=sys.stderr, flush=True)


class BridgeError(Exception):
    pass


class MidiOut:
    """Write-side handle on the ALSA rawmidi node of a USB MIDI card."""

    def __init__(self, name):
        self.name = name
        self.fd = None

    def open(self):
        needle = self.name.lower()
        cards = []
        for line in open("/proc/asound/cards"):
            match = CARD_RE.match(line.rstrip("\n"))
            if not match:
                continue
            index, card_id, _module, long_name = match.groups()
            cards.append((int(index), long_name.strip()))
        matches = [c for c in cards if needle in c[1].lower()]
        if not matches:
            available = ", ".join(f"{c[1]}" for c in cards) or "none"
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

    def heartbeat(self):
        self.send(CC_HEARTBEAT, PROTOCOL)


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
        value |= int(frame["any_working"]) << 3
        value |= int(frame["overflow"]) << 4
        value |= int(frame["chime_done"]) << 5
        value |= int(frame["chime_blocked"]) << 6
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
        response = json.loads(data.split(b"\n", 1)[0])
        return response["result"]["snapshot"]["agents"]


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
        ack = json.loads(data.split(b"\n", 1)[0])
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
    midi = MidiOut(port_name)
    tracker = Tracker()
    while True:
        try:
            midi.open()
            log(f"connected to MIDI card matching {port_name!r}")
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

    request = json.loads(subscription_request([{"pane_id": "w1:p1"}]))
    assert len(request["params"]["subscriptions"]) == 4
    assert request["params"]["subscriptions"][3]["pane_id"] == "w1:p1"

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
    expected = (CC_STATE, value)
    assert fake.sent[-1] == expected, fake.sent[-1]
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
