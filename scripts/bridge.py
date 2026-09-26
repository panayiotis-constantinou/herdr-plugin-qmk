#!/usr/bin/env python3
"""Mirror Herdr agent state on a QMK keyboard over USB MIDI.

Stdlib only: talks to Herdr's Unix socket (newline-delimited JSON-RPC) and
sends MIDI via the platform backend — the ALSA rawmidi device node on Linux,
CoreMIDI through ctypes on macOS. No compiled binary, no dependencies.
"""

import concurrent.futures
import ctypes
import glob
import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import types
from urllib.error import HTTPError
from urllib.request import Request, urlopen

MIDI_CHANNEL = 0xBE  # CC on MIDI channel 15 (status channels are zero-based)
NOTE_ON = 0x9E  # keyboard controls arrive as notes: CC 100/101 and 120-127 are reserved
NOTE_OFF = 0x8E
NOTE_WORKSPACE_PREV = 100
NOTE_WORKSPACE_NEXT = 101
NOTE_TAB_PREV = 102
NOTE_TAB_NEXT = 103
NOTE_PANE_LEFT = 104
NOTE_PANE_DOWN = 105
NOTE_PANE_UP = 106
NOTE_PANE_RIGHT = 107
NOTE_AGENT_PICKER = 108
NOTE_SCRATCHPAD = 109
CC_HEARTBEAT = 110
CC_STATE = 111
CC_SLOT_FIRST = 112
NOTE_WORKSPACE_NEW = 116
NOTE_TAB_NEW = 117
NOTE_LAZYGIT = 118
NOTE_PALETTE = 119
NOTE_PANE_ZOOM = 120
NOTE_HUNK = 121
NOTE_AGENT_NEXT = 122
NOTE_SMART_ACTION = 123
NOTE_ACCEPT = 124
NOTE_REJECT = 125
NOTE_PROMPT = 126
NOTE_CLEAR = 127
PROTOCOL = 2
EMPTY_SLOT = 7
SLOT_COUNT = 4
HEARTBEAT_SECONDS = 1.0
MIDI_POLL_SECONDS = 0.05
ALSA_SEQ_CLIENTS = "/proc/asound/seq/clients"
# Distinct names let the bridge find its own clients in ALSA_SEQ_CLIENTS.
RTMIDI_OUT_CLIENT = "QMK bridge out"
RTMIDI_IN_CLIENT = "QMK bridge in"
COMMAND_TIMEOUT_SECONDS = 2.0
TYPESAFE_TIMEOUT_SECONDS = 2.0
TYPESAFE_MIN_CONFIDENCE = 0.7
TYPESAFE_CHIME_THRESHOLD = 0.8
TYPESAFE_CHIME_DEBOUNCE_SECONDS = 0.25
TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"
MIDI_PACKET_DATA_SIZE = 256

STATUS_CODES = {"idle": 0, "working": 1, "blocked": 2, "done": 3, "unknown": 4}
STATUS_PRIORITY = ["blocked", "working", "done", "unknown", "idle"]
STATUS_RANK = {
    status: len(STATUS_PRIORITY) - index for index, status in enumerate(STATUS_PRIORITY)
}
AGENT_ROLES = {
    "implementation": "Writing, modifying, debugging, or testing code",
    "review": "Reviewing changes, risks, regressions, or correctness",
    "research": "Investigating code, documentation, evidence, or alternatives",
    "planning": "Designing, decomposing, or coordinating future work",
    "operations": "Operating, deploying, diagnosing, or maintaining systems",
    "documentation": "Writing or organizing documentation and explanations",
    "general": "No more specific role clearly fits",
}

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

    def send(self, control, value) -> None:
        raise BridgeError("MIDI backend does not implement send")

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
                if self.status & 0xF0 in (0x80, 0x90, 0xB0):
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
        return self._cf.CFStringCreateWithCString(None, text.encode("utf-8"), self.UTF8)

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
            self.close()
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


def alsa_client_linked(client_name, direction, text=None):
    """Whether the named ALSA sequencer client still has a subscription.

    Returns None when unknowable (no /proc on macOS, or client not listed).
    """
    if text is None:
        try:
            with open(ALSA_SEQ_CLIENTS, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return None
    in_client = False
    linked = False
    for line in text.splitlines():
        match = re.match(r'Client +\d+ : "(.*)" \[', line)
        if match:
            if in_client:
                break
            in_client = match.group(1) == client_name
        elif in_client and line.strip().startswith(f"{direction}:"):
            linked = True
    return linked if in_client else None


class RtMidiOut(MidiOut):
    """Use a named CoreMIDI/ALSA sequencer port via python-rtmidi."""

    def __init__(self, name):
        super().__init__(name)
        self.midi_out = None
        self.midi_in = None
        self.opened = False

    def open(self):
        if self.midi_out is None or self.midi_in is None:
            try:
                import rtmidi  # type: ignore[import-not-found]
            except ImportError as error:
                raise BridgeError("rtmidi backend requires python-rtmidi") from error
            self.midi_out = rtmidi.MidiOut(name=RTMIDI_OUT_CLIENT)
            self.midi_in = rtmidi.MidiIn(name=RTMIDI_IN_CLIENT)

        midi_out = self.midi_out
        midi_in = self.midi_in
        if midi_out is None or midi_in is None:
            raise BridgeError("rtmidi backend did not initialize")
        output_ports = midi_out.get_ports()
        input_ports = midi_in.get_ports()
        output_match = next(
            (
                index
                for index, port in enumerate(output_ports)
                if self.name.lower() in port.lower()
            ),
            None,
        )
        input_match = next(
            (
                index
                for index, port in enumerate(input_ports)
                if self.name.lower() in port.lower()
            ),
            None,
        )
        if output_match is None or input_match is None:
            raise BridgeError(
                f"no duplex sequencer MIDI port matching {self.name!r}; "
                f"outputs: {', '.join(output_ports) or 'none'}; "
                f"inputs: {', '.join(input_ports) or 'none'}"
            )
        try:
            midi_out.open_port(output_match)
            midi_in.open_port(input_match)
            midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        except Exception as error:
            self.close()
            raise BridgeError(
                f"cannot open sequencer port matching {self.name!r}: {error}"
            ) from error
        self.opened = True

    def close(self):
        if self.midi_out is not None:
            self.midi_out.close_port()
        if self.midi_in is not None:
            self.midi_in.close_port()
        # RtMidi keeps its ALSA client until destruction; reuse it on retries.
        self.opened = False

    def heartbeat(self):
        # ALSA drops the subscription silently when the port's owner exits
        # (e.g. rtpmidid restarts, often under the same client number), so
        # sends would vanish without error; force a reopen instead.
        for client, direction in (
            (RTMIDI_OUT_CLIENT, "Connecting To"),
            (RTMIDI_IN_CLIENT, "Connected From"),
        ):
            if alsa_client_linked(client, direction) is False:
                raise BridgeError(f"MIDI port matching {self.name!r} disappeared")
        super().heartbeat()

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
            if len(message) == 3 and message[0] & 0xF0 in (0x80, 0x90, 0xB0):
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

    def heartbeat(self):
        if self.active is None:
            raise BridgeError("all MIDI destinations are closed")
        self.active.heartbeat()

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
        targets = [
            make_midi_out(target.strip())
            for target in name.split("|")
            if target.strip()
        ]
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
            ["herdr", *args],
            capture_output=True,
            text=True,
            check=False,
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
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            continue
        if process.returncode == 0 and process.stdout.strip():
            return process.stdout.rstrip("\x00")
    raise BridgeError("prompt requires non-empty clipboard text")


class TypeSafeClient:
    """Small stdlib client for TypeSafe System One."""

    def __init__(self, api_key=None, model=None, opener=None):
        self.api_key = (
            api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY", "")
        ).strip()
        self.model = model or os.environ.get("TYPESAFE_MODEL", "jev-latest")
        self.opener = opener if opener is not None else urlopen

    @property
    def enabled(self):
        return self.api_key != ""

    def evaluate(self, state, questions):
        if not self.enabled:
            raise BridgeError("TypeSafe is not configured")
        payload = json.dumps(
            {"state": state, "model": self.model, "questions": questions}
        ).encode()
        request = Request(
            TYPESAFE_API_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=TYPESAFE_TIMEOUT_SECONDS) as response:
                data = response.read(1_000_001)
        except HTTPError as error:
            detail = error.read(512).decode("utf-8", "replace").strip()
            if not detail:
                detail = str(error.reason)
            raise BridgeError(f"TypeSafe HTTP {error.code}: {detail}") from error
        except OSError as error:
            raise BridgeError(f"TypeSafe request failed: {error}") from error
        if len(data) > 1_000_000:
            raise BridgeError("TypeSafe response exceeds 1 MB")
        try:
            response = json.loads(data)
        except ValueError as error:
            raise BridgeError(f"malformed TypeSafe response: {error}") from error
        if not isinstance(response, dict) or not isinstance(
            response.get("answers"), dict
        ):
            raise BridgeError("malformed TypeSafe response: answers is not an object")
        return response["answers"]


class TypeSafeAutomation:
    """Optional semantic decisions; code still owns all side effects."""

    AGENT_FIELDS = (
        "pane_id",
        "workspace_id",
        "cwd",
        "title",
        "terminal_title_stripped",
        "agent_status",
        "focused",
        "state_change_seq",
    )

    def __init__(self, client=None, executor=None, clock=time.monotonic):
        self.client = client or TypeSafeClient()
        self.executor = executor
        self.clock = clock
        if self.client.enabled and self.executor is None:
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.results = queue.SimpleQueue()
        self.agents = []
        self.signature = ()
        self.slot_scores = {}
        self.rank_key = None
        self.score_key = None
        self.agent_roles = {}
        self.role_keys = {}
        self.pending_done = []
        self.chime_due = None
        self.last_error = None

    @staticmethod
    def _agent(agent):
        return {
            field: agent.get(field)
            for field in TypeSafeAutomation.AGENT_FIELDS
            if agent.get(field) is not None
        }

    def _agent_state(self, agent):
        state = self._agent(agent)
        pane_id = agent["pane_id"]
        role = self.agent_roles.get(pane_id)
        if role is not None and self.role_keys.get(pane_id) == self._role_key(agent):
            state["role"] = role
        return state

    @staticmethod
    def _role_key(agent):
        return (
            agent.get("workspace_id"),
            agent.get("cwd"),
            agent.get("title"),
            agent.get("terminal_title_stripped"),
            agent.get("kind"),
            agent.get("source"),
            agent.get("command"),
        )

    @staticmethod
    def _number(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _signature(agents):
        return tuple(
            sorted(
                (
                    agent["pane_id"],
                    agent.get("agent_status"),
                    agent.get("state_change_seq"),
                )
                for agent in agents
            )
        )

    @staticmethod
    def _ranking_key(agents):
        return tuple(
            sorted(
                (
                    agent["pane_id"],
                    agent.get("workspace_id"),
                    agent.get("cwd"),
                    agent.get("title"),
                    agent.get("terminal_title_stripped"),
                    agent.get("agent_status"),
                    agent.get("focused"),
                    agent.get("state_change_seq"),
                )
                for agent in agents
            )
        )

    def _scores_for(self, agents):
        if self.score_key != self._ranking_key(agents) or any(
            agent["pane_id"] not in self.slot_scores for agent in agents
        ):
            return {}
        return self.slot_scores

    def attention_order(self, agents):
        scores = self._scores_for(agents)
        if not agents or not scores:
            return None
        return [
            agent["pane_id"]
            for agent in sorted(
                agents,
                key=lambda agent: (
                    -STATUS_RANK.get(agent.get("agent_status"), 0),
                    -scores[agent["pane_id"]],
                    agent.get("state_change_seq", 0),
                    agent["pane_id"],
                ),
            )
        ]

    def _submit(self, kind, context, state, questions):
        if not self.client.enabled or self.executor is None:
            return False
        future = self.executor.submit(self.client.evaluate, state, questions)

        def done(completed):
            try:
                result = (kind, context, completed.result(), None)
            except Exception as error:
                result = (kind, context, None, error)
            self.results.put(result)

        future.add_done_callback(done)
        return True

    def _schedule_ranking(self, agents):
        key = self._ranking_key(agents)
        if not agents or key == self.rank_key:
            return
        self.rank_key = key
        if not self.client.enabled:
            return
        summaries = [self._agent_state(agent) for agent in agents]
        questions = {}
        question_to_pane = {}
        role_question_to_pane = {}
        role_question_keys = {}
        for index, agent in enumerate(agents):
            if len(agents) > 1:
                question = f"attention_{index}"
                question_to_pane[question] = agent["pane_id"]
                questions[question] = {
                    "type": "score",
                    "instructions": {
                        "question": (
                            "How useful would it be for the user to inspect this agent next?"
                        ),
                        "pane_id": agent["pane_id"],
                        "guidance": (
                            "Compare its project, task, role, focus, and recent state with "
                            "the other agents. Deterministic code owns status priority."
                        ),
                    },
                    "criteria": [
                        "No current semantic reason to inspect it",
                        "Low inspection priority",
                        "Useful to inspect soon",
                        "Strongest semantic reason to inspect next",
                    ],
                }
            role_key = self._role_key(agent)
            if self.role_keys.get(agent["pane_id"]) != role_key:
                role_question = f"role_{index}"
                role_question_to_pane[role_question] = agent["pane_id"]
                role_question_keys[role_question] = role_key
                questions[role_question] = {
                    "type": "choice",
                    "instructions": {
                        "question": "What is this agent's primary current role?",
                        "pane_id": agent["pane_id"],
                        "guidance": (
                            "Classify the task shown by this agent's title, command, "
                            "project, and other metadata."
                        ),
                    },
                    "criteria": AGENT_ROLES,
                }
        if not questions:
            return
        self._submit(
            "rank",
            {
                "key": key,
                "question_to_pane": question_to_pane,
                "role_question_to_pane": role_question_to_pane,
                "role_question_keys": role_question_keys,
            },
            {"agents": summaries},
            questions,
        )

    def publish(self, midi, tracker, agents, notify):
        self.agents = [dict(agent) for agent in agents]
        self.signature = self._signature(self.agents)
        frame = tracker.update(
            self.agents, notify=notify, scores=self._scores_for(self.agents)
        )
        self._schedule_ranking(self.agents)
        if frame["chime_blocked"]:
            self.pending_done.clear()
            self.chime_due = None
            frame["chime_done"] = False
            tracker.send_frame(midi, frame)
            return
        if not self.client.enabled or not frame["chime_done"]:
            tracker.send_frame(midi, frame)
            return

        tracker.send_frame(midi, dict(frame, chime_done=False))
        self.pending_done.extend(
            transition
            for transition in frame["transitions"]
            if transition["to"] == "done"
        )
        self.chime_due = self.clock() + TYPESAFE_CHIME_DEBOUNCE_SECONDS

    def _flush_chime(self):
        if self.chime_due is None or self.clock() < self.chime_due:
            return
        transitions = self.pending_done
        self.pending_done = []
        self.chime_due = None
        if not transitions:
            return
        self._submit(
            "chime",
            {"signature": self.signature},
            {
                "transitions": transitions,
                "agents": [self._agent_state(agent) for agent in self.agents],
            },
            {
                "done": {
                    "type": "noul",
                    "instructions": (
                        "Would one audible cue help the user notice these completed "
                        "background agent transitions now?"
                    ),
                    "criteria": {
                        "true": "At least one completion is useful to notice away from its pane",
                        "false": (
                            "They are focused, routine, duplicate, or not worth interrupting"
                        ),
                    },
                }
            },
        )

    def _apply_chime(self, midi, tracker, context, answers, error):
        if context["signature"] != self.signature:
            return
        frame = tracker.update(
            self.agents, notify=False, scores=self._scores_for(self.agents)
        )

        def allowed(question, requested):
            if not requested:
                return False
            if error is not None:
                return True
            answer = answers.get(question, {})
            if answer.get("type") != "noul":
                return True
            return self._number(answer.get("noul"), 1.0) >= TYPESAFE_CHIME_THRESHOLD

        frame["chime_done"] = allowed("done", True)
        frame["chime_blocked"] = False
        if frame["chime_done"]:
            tracker.send_frame(midi, frame)

    def _apply_rank(self, midi, tracker, context, answers, error):
        if context["key"] != self.rank_key:
            return
        if error is not None:
            self.rank_key = None
            return
        for question, pane_id in context["role_question_to_pane"].items():
            answer = answers.get(question, {})
            role = answer.get("choice")
            if (
                answer.get("type") == "choice"
                and role in AGENT_ROLES
                and self._number(answer.get("confidence")) >= TYPESAFE_MIN_CONFIDENCE
            ):
                self.agent_roles[pane_id] = role
                self.role_keys[pane_id] = context["role_question_keys"][question]
        if not context["question_to_pane"]:
            return
        scores = {}
        for question, pane_id in context["question_to_pane"].items():
            answer = answers.get(question, {})
            if (
                answer.get("type") == "score"
                and self._number(answer.get("confidence")) >= TYPESAFE_MIN_CONFIDENCE
            ):
                scores[pane_id] = self._number(answer.get("score"))
        self.slot_scores = scores
        self.score_key = context["key"]
        frame = tracker.update(
            self.agents, notify=False, scores=self._scores_for(self.agents)
        )
        tracker.send_frame(midi, frame)

    def poll(self, midi, tracker):
        self._flush_chime()
        while True:
            try:
                kind, context, answers, error = self.results.get_nowait()
            except queue.Empty:
                return
            if error is not None:
                message = str(error)
                if message != self.last_error:
                    log(f"TypeSafe: {message}; using deterministic fallback")
                    self.last_error = message
            else:
                self.last_error = None
            if kind == "chime":
                self._apply_chime(midi, tracker, context, answers or {}, error)
            elif kind == "rank":
                self._apply_rank(midi, tracker, context, answers or {}, error)


class HerdrController:
    def __init__(
        self, tracker, command=run_herdr, clipboard=read_clipboard, automation=None
    ):
        self.tracker = tracker
        self.command = command
        self.clipboard = clipboard
        self.automation = automation

    def _focused_workspace(self):
        workspaces = self.command("workspace", "list")["workspaces"]
        return next((item for item in workspaces if item.get("focused")), None)

    def _focused_pane(self):
        workspace = self._focused_workspace()
        if workspace is None:
            raise BridgeError("Herdr has no focused workspace")
        panes = self.command("pane", "list", "--workspace", workspace["workspace_id"])[
            "panes"
        ]
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
        current = next(
            (index for index, item in enumerate(items) if item.get("focused")), None
        )
        if current is None:
            raise BridgeError(f"Herdr has no focused {kind}")
        target = items[(current + delta) % len(items)][key]
        self.command(kind, "focus", target)

    def handle(self, control, value):
        if value != 127:
            return False
        if control in (NOTE_WORKSPACE_PREV, NOTE_WORKSPACE_NEXT):
            self._cycle(
                "workspace",
                "workspace_id",
                -1 if control == NOTE_WORKSPACE_PREV else 1,
            )
        elif control in (NOTE_TAB_PREV, NOTE_TAB_NEXT):
            workspace = self._focused_workspace()
            if workspace is None:
                raise BridgeError("Herdr has no focused workspace")
            self._cycle(
                "tab",
                "tab_id",
                -1 if control == NOTE_TAB_PREV else 1,
                workspace["workspace_id"],
            )
        elif NOTE_PANE_LEFT <= control <= NOTE_PANE_RIGHT:
            directions = {
                NOTE_PANE_LEFT: "left",
                NOTE_PANE_DOWN: "down",
                NOTE_PANE_UP: "up",
                NOTE_PANE_RIGHT: "right",
            }
            pane = self._focused_pane()
            self.command(
                "pane",
                "focus",
                "--direction",
                directions[control],
                "--pane",
                pane["pane_id"],
            )
        elif control == NOTE_AGENT_PICKER:
            self.command(
                "plugin", "action", "invoke", "open", "--plugin", "lancodev.jump"
            )
        elif control == NOTE_SCRATCHPAD:
            self.command(
                "plugin", "action", "invoke", "toggle", "--plugin", "herdr-floax"
            )
        elif control == NOTE_WORKSPACE_NEW:
            pane = self._focused_pane()
            self.command("workspace", "create", "--cwd", pane["cwd"], "--focus")
        elif control == NOTE_TAB_NEW:
            pane = self._focused_pane()
            self.command(
                "tab",
                "create",
                "--workspace",
                pane["workspace_id"],
                "--cwd",
                pane["cwd"],
                "--focus",
            )
        elif control == NOTE_LAZYGIT:
            self.command(
                "plugin", "action", "invoke", "open", "--plugin", "herdr-lazygit"
            )
        elif control in (NOTE_PALETTE, NOTE_SMART_ACTION):
            self.command(
                "plugin", "action", "invoke", "open", "--plugin", "jt.command-palette"
            )
        elif control == NOTE_PANE_ZOOM:
            self.command("pane", "zoom", self._focused_pane()["pane_id"], "--toggle")
        elif control == NOTE_HUNK:
            self.command(
                "plugin", "action", "invoke", "worktree-tab", "--plugin", "hunk.diff"
            )
        elif control == NOTE_AGENT_NEXT:
            agents = self.command("agent", "list")["agents"]
            if not agents:
                raise BridgeError("Herdr has no live agents")
            panes = (
                self.automation.attention_order(agents)
                if self.automation is not None
                else None
            ) or [agent["pane_id"] for agent in agents]
            focused = self._focused_pane()["pane_id"]
            current = panes.index(focused) if focused in panes else -1
            self.command("agent", "focus", panes[(current + 1) % len(panes)])
        elif control in (NOTE_ACCEPT, NOTE_REJECT, NOTE_CLEAR):
            keys = {NOTE_ACCEPT: "enter", NOTE_REJECT: "esc", NOTE_CLEAR: "ctrl+c"}
            self.command(
                "agent", "send-keys", self._focused_pane()["pane_id"], keys[control]
            )
        elif control == NOTE_PROMPT:
            pane_id = self._focused_pane()["pane_id"]
            prompt = self.clipboard()
            self.command("agent", "prompt", pane_id, prompt)
        else:
            return False
        return True

    def poll(self, midi):
        for status, control, value in midi.receive():
            if status == NOTE_OFF:
                value = 0
            elif status != NOTE_ON:
                continue
            try:
                if self.handle(control, value):
                    log(f"control note {control}")
            except Exception as error:
                log(f"control note {control} failed: {error}")


class Tracker:
    def __init__(self):
        self.slots = [None] * SLOT_COUNT
        self.previous = {}

    def update(self, agents, notify, scores=None):
        scores = scores or {}
        agents = sorted(
            agents,
            key=lambda agent: (
                -STATUS_RANK.get(agent["agent_status"], 0),
                -scores.get(agent["pane_id"], 0),
                agent.get("state_change_seq", 0),
                agent["pane_id"],
            ),
        )
        wanted = [agent["pane_id"] for agent in agents[:SLOT_COUNT]]
        self.slots = [slot if slot in wanted else None for slot in self.slots]
        for pane_id in wanted:
            if pane_id not in self.slots:
                self.slots[self.slots.index(None)] = pane_id

        current = {a["pane_id"]: a["agent_status"] for a in agents}
        transitions = [
            {"pane_id": pane_id, "from": self.previous[pane_id], "to": status}
            for pane_id, status in current.items()
            if notify and pane_id in self.previous and self.previous[pane_id] != status
        ]

        def changed_to(status):
            return any(transition["to"] == status for transition in transitions)

        def any_is(status):
            return any(a["agent_status"] == status for a in agents)

        aggregate = next((s for s in STATUS_PRIORITY if any_is(s)), "idle")
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
            "transitions": transitions,
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
    ] + [{"type": "pane.agent_status_changed", "pane_id": a["pane_id"]} for a in agents]
    return json.dumps(
        {
            "id": "qmk-herdr-subscribe",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
    ).encode()


def watch_session(socket_path, midi, tracker, controller, automation):
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
        automation.publish(midi, tracker, agents, notify=False)
        last_heartbeat = time.monotonic()
        if changed:
            raise BridgeError("Herdr agent set changed; resubscribing")

        sock.settimeout(MIDI_POLL_SECONDS)
        while True:
            controller.poll(midi)
            automation.poll(midi, tracker)
            while b"\n" in data:
                line, data = data.split(b"\n", 1)
                if not line.strip():
                    continue
                agents = snapshot(socket_path)
                changed = {a["pane_id"] for a in agents} != subscribed
                automation.publish(midi, tracker, agents, notify=True)
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
    automation = TypeSafeAutomation()
    controller = HerdrController(tracker, automation=automation)
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
            watch_session(socket_path, midi, tracker, controller, automation)
        except (
            Exception
        ) as error:  # any failure becomes a logged retry, never a dead daemon
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
    assert first["slots"] == [1, 0, EMPTY_SLOT, EMPTY_SLOT], first
    assert not first["chime_done"]

    second = tracker.update(
        [
            {"pane_id": "p2", "agent_status": "done", "state_change_seq": 3},
            {"pane_id": "p1", "agent_status": "blocked", "state_change_seq": 4},
        ],
        notify=True,
    )
    assert second["slots"] == [3, 2, EMPTY_SLOT, EMPTY_SLOT], second
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
    parsed = parser.feed((NOTE_ON, NOTE_WORKSPACE_PREV))
    assert parsed == []
    parsed = parser.feed((0xF8, 127, NOTE_WORKSPACE_NEXT, 127))
    expected_messages = [
        (NOTE_ON, NOTE_WORKSPACE_PREV, 127),
        (NOTE_ON, NOTE_WORKSPACE_NEXT, 127),
    ]
    assert parsed == expected_messages
    parsed = parser.feed((0xF0, 1, 2, 0xF7, NOTE_ON, NOTE_ACCEPT, 127))
    expected_messages = [(NOTE_ON, NOTE_ACCEPT, 127)]
    assert parsed == expected_messages
    parsed = parser.feed((0xF1, 1, NOTE_ON, NOTE_REJECT, 127))
    expected_messages = [(NOTE_ON, NOTE_REJECT, 127)]
    assert parsed == expected_messages
    parsed = parser.feed((NOTE_OFF, NOTE_REJECT, 0, MIDI_CHANNEL, CC_STATE, 1))
    expected_messages = [(NOTE_OFF, NOTE_REJECT, 0), (MIDI_CHANNEL, CC_STATE, 1)]
    assert parsed == expected_messages

    expected = [MIDI_CHANNEL, CC_HEARTBEAT, PROTOCOL]
    packet_list = build_packet_list(expected)
    num_packets, stamp, length = struct.unpack_from("<IQH", packet_list, 0)
    assert num_packets == 1
    assert stamp == 0
    assert length == 3
    assert list(packet_list[14:17]) == expected
    assert len(packet_list) == 4 + 8 + 2 + MIDI_PACKET_DATA_SIZE + 2

    frame = dict(overflow)
    frame.update(
        aggregate="done",
        any_working=False,
        overflow=False,
        chime_done=True,
        chime_blocked=False,
    )

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

    seq_clients = """Client 129 : "QMK bridge out" [User Legacy]
  Port   0 : "RtMidi output" (R-e-) [Out]
    Connecting To: 128:0
Client 130 : "QMK bridge in" [User Legacy]
  Port   0 : "RtMidi input" (-We-) [In]
Client 131 : "Other" [User Legacy]
  Port   0 : "x" (RWe-) [In/Out]
    Connected From: 128:0
"""
    assert alsa_client_linked("QMK bridge out", "Connecting To", seq_clients)
    assert alsa_client_linked("QMK bridge in", "Connected From", seq_clients) is False
    assert alsa_client_linked("missing", "Connecting To", seq_clients) is None

    ports_available = [True]

    class FakeRtMidiOut:
        instances = []

        def __init__(self, name=None):
            self.opened = None
            self.sent = []
            self.closed = False
            self.instances.append(self)

        def get_ports(self):
            return ["unrelated"] + (["qmk-herdr-ipad"] if ports_available[0] else [])

        def open_port(self, index):
            self.opened = index

        def send_message(self, message):
            self.sent.append(message)

        def close_port(self):
            self.closed = True

    class FakeRtMidiIn:
        instances = []

        def __init__(self, name=None):
            self.opened = None
            self.events = [([NOTE_ON, NOTE_ACCEPT, 127], 0.0)]
            self.closed = False
            self.instances.append(self)

        def get_ports(self):
            return ["unrelated"] + (["qmk-herdr-ipad"] if ports_available[0] else [])

        def open_port(self, index):
            self.opened = index

        def ignore_types(self, **kwargs):
            pass

        def get_message(self):
            return self.events.pop(0) if self.events else None

        def close_port(self):
            self.closed = True

    previous_rtmidi = sys.modules.get("rtmidi")
    fake_rtmidi = types.ModuleType("rtmidi")
    setattr(fake_rtmidi, "MidiOut", FakeRtMidiOut)
    setattr(fake_rtmidi, "MidiIn", FakeRtMidiIn)
    sys.modules["rtmidi"] = fake_rtmidi
    try:
        output = make_midi_out("rtmidi:HERDR-IPAD")
        output.open()
        output.send(CC_HEARTBEAT, PROTOCOL)
        received = output.receive()
        expected_messages = [(NOTE_ON, NOTE_ACCEPT, 127)]
        assert received == expected_messages
        output.close()
        output_instance = FakeRtMidiOut.instances[-1]
        input_instance = FakeRtMidiIn.instances[-1]
        assert output_instance.opened == 1 and input_instance.opened == 1
        assert output_instance.sent == [[MIDI_CHANNEL, CC_HEARTBEAT, PROTOCOL]]
        assert output_instance.closed and input_instance.closed

        ports_available[0] = False
        missing = make_midi_out("rtmidi:qmk-herdr-ipad")
        before = len(FakeRtMidiOut.instances)
        for _ in range(3):
            try:
                missing.open()
            except BridgeError:
                missing.close()
            else:
                raise AssertionError("missing RtMidi port opened")
        assert len(FakeRtMidiOut.instances) == before + 1
        ports_available[0] = True
        missing.open()
        assert missing.midi_out is FakeRtMidiOut.instances[-1]
        missing.close()
    finally:
        if previous_rtmidi is None:
            del sys.modules["rtmidi"]
        else:
            sys.modules["rtmidi"] = previous_rtmidi

    commands = []

    def fake_herdr(*args):
        if args == ("workspace", "list"):
            return {
                "workspaces": [
                    {"workspace_id": "w1", "number": 1, "focused": True},
                    {"workspace_id": "w2", "number": 2, "focused": False},
                ]
            }
        if args == ("tab", "list", "--workspace", "w1"):
            return {
                "tabs": [
                    {"tab_id": "w1:t1", "number": 1, "focused": True},
                    {"tab_id": "w1:t2", "number": 2, "focused": False},
                ]
            }
        if args == ("pane", "list", "--workspace", "w1"):
            return {
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "workspace_id": "w1",
                        "cwd": "/tmp/project",
                        "focused": True,
                    }
                ]
            }
        if args == ("agent", "list"):
            return {"agents": [{"pane_id": "w1:p1"}, {"pane_id": "w2:p3"}]}
        commands.append(args)
        return {}

    tracker.slots = ["w1:p2", None, None, None]
    controller = HerdrController(
        tracker, command=fake_herdr, clipboard=lambda: "test prompt"
    )
    assert not controller.handle(NOTE_ACCEPT, 0)
    for control in (
        NOTE_WORKSPACE_PREV,
        NOTE_WORKSPACE_NEXT,
        NOTE_TAB_PREV,
        NOTE_TAB_NEXT,
        NOTE_PANE_LEFT,
        NOTE_PANE_DOWN,
        NOTE_PANE_UP,
        NOTE_PANE_RIGHT,
        NOTE_AGENT_PICKER,
        NOTE_SCRATCHPAD,
        NOTE_WORKSPACE_NEW,
        NOTE_TAB_NEW,
        NOTE_LAZYGIT,
        NOTE_PALETTE,
        NOTE_PANE_ZOOM,
        NOTE_HUNK,
        NOTE_AGENT_NEXT,
        NOTE_SMART_ACTION,
        NOTE_ACCEPT,
        NOTE_REJECT,
        NOTE_PROMPT,
        NOTE_CLEAR,
    ):
        assert controller.handle(control, 127)
    expected_commands = [
        ("workspace", "focus", "w2"),
        ("tab", "focus", "w1:t2"),
        ("plugin", "action", "invoke", "open", "--plugin", "lancodev.jump"),
        ("plugin", "action", "invoke", "toggle", "--plugin", "herdr-floax"),
        ("workspace", "create", "--cwd", "/tmp/project", "--focus"),
        ("tab", "create", "--workspace", "w1", "--cwd", "/tmp/project", "--focus"),
        ("plugin", "action", "invoke", "open", "--plugin", "herdr-lazygit"),
        ("plugin", "action", "invoke", "open", "--plugin", "jt.command-palette"),
        ("pane", "zoom", "w1:p1", "--toggle"),
        ("plugin", "action", "invoke", "worktree-tab", "--plugin", "hunk.diff"),
        ("agent", "focus", "w2:p3"),
        ("agent", "prompt", "w1:p1", "test prompt"),
        ("agent", "send-keys", "w1:p1", "enter"),
        ("agent", "send-keys", "w1:p1", "esc"),
        ("agent", "send-keys", "w1:p1", "ctrl+c"),
    ]
    assert all(command in commands for command in expected_commands)

    class FakeControlMidi:
        def receive(self):
            return [
                (MIDI_CHANNEL, NOTE_ACCEPT, 127),
                (NOTE_OFF, NOTE_ACCEPT, 127),
                (NOTE_ON, NOTE_ACCEPT, 127),
            ]

    commands.clear()
    controller.poll(FakeControlMidi())
    assert commands == [("agent", "send-keys", "w1:p1", "enter")], commands

    class ImmediateFuture:
        def __init__(self, function, arguments):
            try:
                self.value = function(*arguments)
                self.error = None
            except Exception as error:
                self.value = None
                self.error = error

        def result(self):
            if self.error is not None:
                raise self.error
            return self.value

        def add_done_callback(self, callback):
            callback(self)

    class ImmediateExecutor:
        def submit(self, function, *arguments):
            return ImmediateFuture(function, arguments)

    class FakeHTTPResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, _limit):
            return b'{"answers":{"target":{"type":"choice","choice":"agent_0"}}}'

    http_call = {}

    def fake_open(request, timeout):
        http_call.update(request=request, timeout=timeout)
        return FakeHTTPResponse()

    http_client = TypeSafeClient("secret", "jev-test", opener=fake_open)
    http_answers = http_client.evaluate(
        {"agents": [{"pane_id": "w1:p1"}]},
        {
            "target": {
                "type": "choice",
                "instructions": "Choose",
                "criteria": {"agent_0": "Test agent"},
            }
        },
    )
    assert http_answers["target"]["choice"] == "agent_0"
    assert http_call["request"].get_header("Authorization") == "Bearer secret"
    try:
        request_payload = json.loads(http_call["request"].data)
    except ValueError as error:
        raise AssertionError("TypeSafe request is not JSON") from error
    assert request_payload["model"] == "jev-test"
    assert http_call["timeout"] == TYPESAFE_TIMEOUT_SECONDS

    class FakeTypeSafeClient:
        enabled = True

        def __init__(
            self,
            noul=1.0,
            role="implementation",
            fail=False,
        ):
            self.noul = noul
            self.role = role
            self.fail = fail
            self.calls = []

        def evaluate(self, state, questions):
            self.calls.append((state, questions))
            if self.fail:
                raise BridgeError("test TypeSafe failure")
            answers = {}
            for question_id, question in questions.items():
                if question["type"] == "choice":
                    answers[question_id] = {
                        "type": "choice",
                        "choice": self.role,
                        "confidence": 0.99,
                    }
                elif question["type"] == "noul":
                    answers[question_id] = {"type": "noul", "noul": self.noul}
                else:
                    pane_id = question["instructions"]["pane_id"]
                    answers[question_id] = {
                        "type": "score",
                        "score": 3 if pane_id == "p5" else 0,
                        "confidence": 0.99,
                    }
            return answers

    client = FakeTypeSafeClient()
    smart = TypeSafeAutomation(client, executor=ImmediateExecutor())
    smart_controller = HerdrController(
        tracker, command=fake_herdr, clipboard=lambda: "test prompt", automation=smart
    )
    commands.clear()
    assert smart_controller.handle(NOTE_SMART_ACTION, 127)
    assert smart_controller.handle(NOTE_PROMPT, 127)
    expected = [
        ("plugin", "action", "invoke", "open", "--plugin", "jt.command-palette"),
        ("agent", "prompt", "w1:p1", "test prompt"),
    ]
    assert commands == expected
    assert client.calls == []

    now = [0.0]

    def test_clock():
        return now[0]

    expired = TypeSafeAutomation(
        FakeTypeSafeClient(fail=True),
        executor=ImmediateExecutor(),
        clock=test_clock,
    )
    expired._schedule_ranking([{"pane_id": "w1:p1", "agent_status": "idle"}])
    expired.poll(fake, tracker)
    assert expired.rank_key is None

    quiet = TypeSafeAutomation(
        FakeTypeSafeClient(noul=0.0),
        executor=ImmediateExecutor(),
        clock=test_clock,
    )
    quiet_tracker = Tracker()
    quiet_midi = FakeMidi()
    working = [
        {
            "pane_id": "p1",
            "agent_status": "working",
            "state_change_seq": 1,
            "title": "test",
            "focused": False,
        }
    ]
    quiet.publish(quiet_midi, quiet_tracker, working, notify=False)
    done = [dict(working[0], agent_status="done", state_change_seq=2)]
    quiet.publish(quiet_midi, quiet_tracker, done, notify=True)
    state_count = sum(item[0] == CC_STATE for item in quiet_midi.sent if item != "hb")
    now[0] += TYPESAFE_CHIME_DEBOUNCE_SECONDS
    quiet.poll(quiet_midi, quiet_tracker)
    assert (
        sum(item[0] == CC_STATE for item in quiet_midi.sent if item != "hb")
        == state_count
    )

    batch_client = FakeTypeSafeClient(noul=0.0)
    batch = TypeSafeAutomation(
        batch_client,
        executor=ImmediateExecutor(),
        clock=test_clock,
    )
    batch_tracker = Tracker()
    batch_midi = FakeMidi()
    both_working = working + [dict(working[0], pane_id="p2")]
    batch.publish(batch_midi, batch_tracker, both_working, notify=False)
    first_done = [done[0], both_working[1]]
    batch.publish(batch_midi, batch_tracker, first_done, notify=True)
    both_done = [done[0], dict(done[0], pane_id="p2")]
    batch.publish(batch_midi, batch_tracker, both_done, notify=True)
    assert not [questions for _, questions in batch_client.calls if "done" in questions]
    now[0] += TYPESAFE_CHIME_DEBOUNCE_SECONDS
    batch.poll(batch_midi, batch_tracker)
    chime_calls = [call for call in batch_client.calls if "done" in call[1]]
    assert len(chime_calls) == 1
    assert len(chime_calls[0][0]["transitions"]) == 2

    failed_chime = TypeSafeAutomation(
        FakeTypeSafeClient(fail=True),
        executor=ImmediateExecutor(),
        clock=test_clock,
    )
    failed_chime_tracker = Tracker()
    failed_chime_midi = FakeMidi()
    failed_chime.publish(failed_chime_midi, failed_chime_tracker, working, notify=False)
    failed_chime.publish(failed_chime_midi, failed_chime_tracker, done, notify=True)
    now[0] += TYPESAFE_CHIME_DEBOUNCE_SECONDS
    failed_chime.poll(failed_chime_midi, failed_chime_tracker)
    state_values = [
        item[1]
        for item in failed_chime_midi.sent
        if item != "hb" and item[0] == CC_STATE
    ]
    assert state_values[-1] & (1 << 5)

    blocked_tracker = Tracker()
    blocked_midi = FakeMidi()
    quiet.publish(blocked_midi, blocked_tracker, working, notify=False)
    blocked = [dict(working[0], agent_status="blocked", state_change_seq=2)]
    quiet.publish(blocked_midi, blocked_tracker, blocked, notify=True)
    blocked_values = [
        item[1] for item in blocked_midi.sent if item != "hb" and item[0] == CC_STATE
    ]
    assert blocked_values[-1] & (1 << 6)

    ranked_client = FakeTypeSafeClient()
    ranked = TypeSafeAutomation(ranked_client, executor=ImmediateExecutor())
    ranked_tracker = Tracker()
    ranked_midi = FakeMidi()
    many = [
        {
            "pane_id": f"p{index}",
            "agent_status": "idle",
            "state_change_seq": index,
            "title": f"task {index}",
            "cwd": f"/{index}",
        }
        for index in range(1, 6)
    ]
    ranked.publish(ranked_midi, ranked_tracker, many, notify=False)
    assert "p5" not in ranked_tracker.slots
    _, rank_questions = ranked_client.calls[-1]
    assert rank_questions["role_0"]["type"] == "choice"
    assert rank_questions["attention_0"]["type"] == "score"
    ranked.poll(ranked_midi, ranked_tracker)
    assert "p5" in ranked_tracker.slots and "p4" not in ranked_tracker.slots
    assert ranked.agent_roles["p1"] == "implementation"
    assert "role" not in ranked._agent_state(dict(many[0], title="changed task"))
    role_call_count = sum(
        question.startswith("role_")
        for _, questions in ranked_client.calls
        for question in questions
    )
    ranked.publish(ranked_midi, ranked_tracker, many, notify=False)
    assert role_call_count == sum(
        question.startswith("role_")
        for _, questions in ranked_client.calls
        for question in questions
    )
    attention = ranked.attention_order(many)
    assert attention is not None and attention[0] == "p5"

    def attention_herdr(*args):
        if args == ("agent", "list"):
            return {
                "agents": [
                    {"pane_id": "w1:p1", "agent_status": "idle"},
                    {"pane_id": "w1:p2", "agent_status": "idle"},
                    {"pane_id": "w2:p3", "agent_status": "idle"},
                ]
            }
        return fake_herdr(*args)

    attention_agents = attention_herdr("agent", "list")["agents"]
    ranked.slot_scores = {"w1:p1": 3, "w1:p2": 0, "w2:p3": 2}
    ranked.score_key = ranked._ranking_key(attention_agents)
    attention_controller = HerdrController(
        tracker, command=attention_herdr, automation=ranked
    )
    assert attention_controller.handle(NOTE_AGENT_NEXT, 127)
    expected_attention_focus = ("agent", "focus", "w2:p3")
    assert commands[-1] == expected_attention_focus

    fallback = make_midi_out("Planck EZ|rtmidi:qmk-herdr-ipad")
    assert isinstance(fallback, FallbackMidiOut)
    assert isinstance(
        fallback.targets[0], CoreMidiOut if sys.platform == "darwin" else AlsaMidiOut
    )
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
