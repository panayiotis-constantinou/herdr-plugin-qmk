#!/usr/bin/env python3
"""Run with python3 scripts/test_session.py; a fake Herdr socket, no MIDI."""
import json
import os
import socket
import tempfile
import threading
import time

import bridge


class FakeHerdr:
    """Answers requests from `agents`; records each event subscription."""

    def __init__(self, path):
        self.agents = [{"pane_id": "p1", "agent_status": "idle", "state_change_seq": 1}]
        self.subscriptions = []
        self.streams = []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(path)
        self.server.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        request = json.loads(conn.makefile().readline())
        if request["method"] == "events.subscribe":
            self.subscriptions.append(request["params"]["subscriptions"])
            self.streams.append(conn)
            ack = {"id": request["id"], "result": {"type": "subscription_started"}}
            conn.sendall(json.dumps(ack).encode() + b"\n")
            return  # The stream stays open until either side closes it.
        if request["method"] == "session.snapshot":
            result = {"snapshot": {"agents": self.agents}}
        else:
            result = {}
        conn.sendall(json.dumps({"id": request["id"], "result": result}).encode() + b"\n")
        conn.close()

    def push(self, event):
        self.streams[-1].sendall(json.dumps(event).encode() + b"\n")

    def stop(self):
        self.server.close()
        for stream in self.streams:
            stream.close()


class Midi(bridge.MidiOut):
    def __init__(self):
        super().__init__("test")
        self.sent = []

    def send(self, control, value):
        self.sent.append((control, value))


def wait_for(predicate, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


def check():
    with tempfile.TemporaryDirectory() as directory:
        config = os.path.join(directory, "config.toml")
        with open(config, "w") as handle:
            handle.write('[ui]\nagent_panel_sort = "priority"\n')
        os.environ["HERDR_CONFIG_PATH"] = config
        path = os.path.join(directory, "herdr.sock")
        herdr = FakeHerdr(path)
        midi = Midi()
        tracker = bridge.Tracker()
        automation = bridge.TypeSafeAutomation(client=bridge.TypeSafeClient(api_key=""))
        controller = bridge.HerdrController(tracker, command=lambda *args: {}, automation=automation)
        errors = []

        def session():
            try:
                bridge.watch_session(path, midi, tracker, controller, automation)
            except Exception as error:
                errors.append(error)

        bridge.log = lambda message: None
        thread = threading.Thread(target=session, daemon=True)
        thread.start()
        assert wait_for(lambda: len(herdr.subscriptions) == 1)
        status = [s for s in herdr.subscriptions[0] if s["type"] == "pane.agent_status_changed"]
        assert status == [{"type": "pane.agent_status_changed", "pane_id": "p1"}], status
        assert {"type": "pane.focused"} in herdr.subscriptions[0]
        assert wait_for(lambda: (bridge.CC_SLOT_FIRST, 0) in midi.sent), "p1 idle in slot 0"

        # A new agent renews the subscription in place, with no reconnect.
        herdr.agents = herdr.agents + [{"pane_id": "p2", "agent_status": "working", "state_change_seq": 2}]
        herdr.push({"event": "pane.agent_detected", "data": {"pane_id": "p2"}})
        assert wait_for(lambda: len(herdr.subscriptions) == 2), "resubscribed"
        panes = {s.get("pane_id") for s in herdr.subscriptions[1] if s["type"] == "pane.agent_status_changed"}
        assert panes == {"p1", "p2"}, panes
        assert thread.is_alive() and not errors, errors

        # A focus event alone brings a snapshot: the focused blocked agent's risk.
        herdr.agents = [dict(herdr.agents[0], agent_status="blocked", state_change_seq=3, focused=True), herdr.agents[1]]
        midi.sent.clear()
        herdr.push({"event": "pane.focused", "data": {"pane_id": "p1"}})
        assert wait_for(lambda: (bridge.CC_SLOT_FIRST, 2) in midi.sent), "blocked p1 shown"

        herdr.stop()
        thread.join(timeout=3)
        assert errors and "closed" in str(errors[0]), errors
    print("session test ok")


if __name__ == "__main__":
    check()
