#!/usr/bin/env python3
"""Mirror Herdr agent state on QMK keyboards over MIDI, one of them active.

Stdlib only: talks to Herdr's Unix socket (newline-delimited JSON-RPC) and
sends MIDI via the platform backend — the ALSA rawmidi device node on Linux,
CoreMIDI through ctypes on macOS. No compiled binary, no dependencies.
"""

import concurrent.futures
import ctypes
import fcntl
import glob
import json
import os
import queue
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
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
NOTE_AGENT_PREV = 110
NOTE_AGENT_URGENT = 111
NOTE_PANE_CLOSE = 112  # the firmware only sends closes after a double tap
NOTE_TAB_CLOSE = 113
NOTE_WORKSPACE_CLOSE = 114
NOTE_PANE_SPLIT = 115
TAP_VELOCITY = 127
HOLD_VELOCITY = 64  # a key held past the tapping term: its variant, if any
CC_HEARTBEAT = 110
CC_STATE = 111
CC_SLOT_FIRST = 112
CC_ECHO = 116  # returned by the keyboard after each protocol heartbeat
CC_RISK = 117  # approval risk for the focused blocked agent
CC_SORT = 118  # both ways: Herdr's panel mode to every board, toggle requests back
CC_ACTIVE = 119  # to the keyboard with each heartbeat: 1 active, 0 standby
CHIME_BITS = 0b1100000  # CC_STATE done/blocked chime flags: active board only
ECHO_STALE_SECONDS = 5.0
RECENT_BOARDS_KEPT = 8
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
NOTE_PROMPT = 126  # retired: it submitted the host's clipboard, never the iPad's
NOTE_CLEAR = 127
# Popup keys and what they open: Herdr TUI overlays.
POPUPS = {
    NOTE_AGENT_PICKER: "the Jump picker",
    NOTE_SCRATCHPAD: "the Floax scratch shell",
    NOTE_PALETTE: "the command palette",
    NOTE_SMART_ACTION: "the command palette",
}
# Popups Rootshell control mode is known to draw. Filled from the Rootshell
# spike: until an overlay is seen working there, its key does nothing while a
# Rootshell board is active instead of opening something invisible.
ROOTSHELL_SHOWN = frozenset()
PROTOCOL = 3
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
# Any real chance of a destructive action shows high risk, even when unsure.
TYPESAFE_RISK_HIGH_PROBABILITY = 0.3
BLOCKED_TAIL_LINES = 40
TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"
MIDI_PACKET_DATA_SIZE = 256
METADATA_SOURCE = "qmk-herdr"
# Herdr styles sidebar tokens per token, not per value, so the agent name goes
# in the token for its keyboard color, or the plain one for agents off the board.
COLOR_TOKENS = ["qmk_blue", "qmk_yellow", "qmk_teal", "qmk_mauve"]
PLAIN_TOKEN = "qmk_agent"
NAME_TOKENS = COLOR_TOKENS + [PLAIN_TOKEN]
# Names expire unless refreshed, so a stopped bridge leaves no stale colors.
NAME_TTL_MS = 30_000
NAME_REFRESH_SECONDS = 10.0
# The running bridge holds a POSIX record lock on this state-dir file, so one
# bridge runs per state dir whichever plugin root (Nix generation) started it.
LOCK_FILE = "qmk-herdr.lock"
LOG_FILE = "qmk-herdr.log"
LIFECYCLE_COMMANDS = ("start", "stop", "restart", "status")
STOP_SECONDS = 2.0
START_SECONDS = 3.0
# Bridges from before the lock, started from any plugin root or checkout.
LEGACY_BRIDGE_RE = re.compile(r"qmk-herdr[^/]*/scripts/bridge\.py$")

STATUS_CODES = {"idle": 0, "working": 1, "blocked": 2, "done": 3, "unknown": 4}
STATUS_PRIORITY = ["blocked", "working", "done", "unknown", "idle"]
STATUS_RANK = {
    status: len(STATUS_PRIORITY) - index for index, status in enumerate(STATUS_PRIORITY)
}
# CC_RISK values; zero keeps older firmware and non-TypeSafe bridges neutral.
RISK_NONE = 0
RISK_PENDING = 1
RISK_UNKNOWN = 2
RISK_LEVELS = [3, 4, 5]  # low, medium, high
BLOCKED_REASONS = {
    "permission": (
        "Waiting for the user to approve or deny a specific tool call, command, "
        "or file change"
    ),
    "question": (
        "Asked the user a question or offered choices that need a typed or "
        "selected answer, not a simple approval"
    ),
    "error": (
        "Stopped because of an error, crash, failed command, or exhausted limit "
        "and needs intervention"
    ),
    "other": "None of the above clearly fits",
}
# Herdr's built-in agent panel modes, as CC_SORT values (protocol 3).
SORT_PRIORITY = 0
SORT_GROUPED = 1
# Slot CC bits 3-4; zero means unknown and keeps the plain blocked blink.
REASON_CODES = {"permission": 1, "question": 2, "error": 3}
# Slot CC bits 5-6 carry the agent's color index, unique among the slots.
APPROVAL_RISKS = [
    "The pending action only reads or inspects: viewing files, searching, "
    "listing, or read-only commands",
    "The pending action changes files inside the project or runs local builds "
    "and tests",
    "The pending action is destructive or reaches outside the project: deleting "
    "data, force operations, git push, network requests, installing software, "
    "credentials or secrets, or system configuration",
]
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

    def maintain(self):
        """Periodic housekeeping between session events; most backends need none."""

    def take_refresh(self):
        """True once when every keyboard needs the current frame resent."""
        return False

    def rootshell_active(self):
        """Whether the active board is the one used through Rootshell."""
        return False


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


def alsa_midi_cards(cards_text=None, nodes=glob.glob):
    """(index, name) for every ALSA card that exposes a rawmidi node."""
    if cards_text is None:
        try:
            with open("/proc/asound/cards") as cards_file:
                cards_text = cards_file.read()
        except OSError as error:
            raise BridgeError(f"cannot list ALSA cards: {error}") from error
    cards = []
    for line in cards_text.splitlines():
        match = CARD_RE.match(line)
        if match and nodes(f"/dev/snd/midiC{match.group(1)}D*"):
            cards.append((int(match.group(1)), match.group(4).strip()))
    return cards


class AlsaMidiOut(MidiOut):
    """Duplex handle on the ALSA rawmidi node of one USB MIDI card."""

    def __init__(self, name, card_index):
        super().__init__(name)
        self.card_index = card_index
        self.fd = None
        self.parser = MidiParser()

    def open(self):
        nodes = sorted(glob.glob(f"/dev/snd/midiC{self.card_index}D*"))
        if not nodes:
            raise BridgeError(f"card {self.name!r} exposes no rawmidi device")
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
        # Per-port client names keep alsa_client_linked unambiguous when
        # several sequencer ports are open at once.
        self.out_client = f"{RTMIDI_OUT_CLIENT}: {name}"
        self.in_client = f"{RTMIDI_IN_CLIENT}: {name}"

    def open(self):
        if self.midi_out is None or self.midi_in is None:
            try:
                import rtmidi  # type: ignore[import-not-found]
            except ImportError as error:
                raise BridgeError("rtmidi backend requires python-rtmidi") from error
            self.midi_out = rtmidi.MidiOut(name=self.out_client)
            self.midi_in = rtmidi.MidiIn(name=self.in_client)

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
            (self.out_client, "Connecting To"),
            (self.in_client, "Connected From"),
        ):
            linked = alsa_client_linked(client, direction)
            if linked is not None and not linked:
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


class Port:
    """A MIDI port discovery found: identity while present, name kept across replugs."""

    def __init__(self, key, name, factory, named=False, output_only=False, rootshell=False):
        self.key = key
        self.name = name
        self.factory = factory
        self.named = named
        self.output_only = output_only
        self.rootshell = rootshell


class Board:
    """One open keyboard port and what the fleet knows about it."""

    def __init__(self, port, midi, now):
        self.key = port.key
        self.name = port.name
        self.named = port.named
        self.output_only = port.output_only
        self.rootshell = port.rootshell
        self.midi = midi
        self.opened_at = now
        self.last_echo = None
        self.echo_warned = False

    def confirmed(self, now):
        """Running the Herdr firmware: echoing heartbeats, or unable to echo at all."""
        if self.output_only:
            return True
        return self.last_echo is not None and now - self.last_echo <= ECHO_STALE_SECONDS


def scan_alsa_ports():
    return [
        Port(
            ("alsa", index, name),
            name,
            lambda index=index, name=name: AlsaMidiOut(name, index),
        )
        for index, name in alsa_midi_cards()
    ]


class RtMidiPortLister:
    """Duplex sequencer port names, through one long-lived pair of clients."""

    def __init__(self):
        import rtmidi  # type: ignore[import-not-found]

        self.midi_out = rtmidi.MidiOut(name=f"{RTMIDI_OUT_CLIENT}: scan")
        self.midi_in = rtmidi.MidiIn(name=f"{RTMIDI_IN_CLIENT}: scan")

    def __call__(self):
        inputs = set(self.midi_in.get_ports())
        return [port for port in self.midi_out.get_ports() if port in inputs]


def rtmidi_available():
    try:
        import rtmidi  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def make_scanner(rtmidi_ports=(), coremidi_names=()):
    """Every port worth probing now: local keyboards plus configured rtmidi ports.

    Linux finds USB keyboards as ALSA rawmidi cards. macOS lists duplex
    CoreMIDI ports through python-rtmidi; without it, CoreMIDI names from the
    config are driven output-only and can never claim.
    """
    lister = None
    if sys.platform == "darwin" and rtmidi_available():
        lister = RtMidiPortLister()

    def scan():
        ports = [
            Port(
                ("rtmidi", name),
                f"rtmidi:{name}",
                lambda name=name: RtMidiOut(name),
                named=True,
                rootshell=rootshell,
            )
            for name, rootshell in rtmidi_ports
        ]
        if lister is not None:
            ports += [
                Port(("rtmidi", name), name, lambda name=name: RtMidiOut(name))
                for name in lister()
                if not any(wanted.lower() in name.lower() for wanted, _ in rtmidi_ports)
            ]
        elif sys.platform == "darwin":
            ports += [
                Port(("coremidi", name), name, lambda name=name: CoreMidiOut(name), output_only=True)
                for name in coremidi_names
            ]
        else:
            ports += scan_alsa_ports()
        return ports

    return scan


def parse_port_list(port_list):
    """(rtmidi (name, rootshell) pairs, plain entries) from midi-port.

    Entries are separated by `|`. `rtmidi:NAME` names a port discovery cannot
    find; a trailing `@rootshell` marks it as used through Rootshell.
    """
    rtmidi, plain = [], []
    for entry in (entry.strip() for entry in port_list.split("|")):
        if not entry:
            continue
        if not entry.startswith("rtmidi:"):
            plain.append(entry)
            continue
        name = entry.removeprefix("rtmidi:").strip()
        rootshell = name.endswith("@rootshell")
        name = name.removesuffix("@rootshell").strip()
        if name:
            rtmidi.append((name, rootshell))
    return rtmidi, plain


class KeyboardFleet(MidiOut):
    """Every keyboard running the Herdr firmware, one of them active.

    All boards mirror the same frames, including Herdr's panel sort mode.
    Only the active board chimes and can change the sort. A control note or
    sort request from a standby board claims it and is swallowed, so the
    first press on a board you just picked up never acts.
    """

    def __init__(self, scan, state_dir=None, clock=time.monotonic):
        super().__init__("keyboards")
        self.scan = scan
        self.state_dir = state_dir
        self.clock = clock
        self.boards = {}
        self.ignored = set()  # present ports that never echoed; forgotten once gone
        self.failures = {}
        self.active = None
        self.provisional = True  # picked without a claim: a better-remembered board may replace it
        self.pending = []
        self.refresh_wanted = False
        self.last_scan = None
        self.status_text = None
        self.recent = self._load_recent()

    @classmethod
    def from_config(cls, port_list, state_dir=None):
        rtmidi_ports, plain = parse_port_list(port_list)
        coremidi_names = []
        if plain and sys.platform == "darwin" and not rtmidi_available():
            coremidi_names = plain
        elif plain:
            log(
                f"ignoring {', '.join(plain)} in midi-port: "
                "keyboards are found automatically; list only rtmidi: ports"
            )
        return cls(make_scanner(rtmidi_ports, coremidi_names), state_dir)

    # Selection -------------------------------------------------------------

    def _recent_path(self):
        return os.path.join(self.state_dir, "recent-boards") if self.state_dir else None

    def _load_recent(self):
        path = self._recent_path()
        try:
            with open(path, encoding="utf-8") as handle:
                return [line.strip() for line in handle if line.strip()]
        except (OSError, TypeError):
            return []

    def _remember(self, name):
        self.recent = [name] + [other for other in self.recent if other != name]
        self.recent = self.recent[:RECENT_BOARDS_KEPT]
        path = self._recent_path()
        if path is None:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("".join(f"{line}\n" for line in self.recent))
        except OSError as error:
            log(f"cannot remember the active keyboard: {error}")

    def _best(self, exclude=None):
        now = self.clock()
        candidates = [
            board
            for key, board in self.boards.items()
            if key != exclude and board.confirmed(now)
        ]
        if not candidates:
            return None

        def rank(board):
            return self.recent.index(board.name) if board.name in self.recent else len(self.recent)

        return min(candidates, key=rank).key

    def _activate(self, key, reason, remember):
        board = self.boards[key]
        changed = key != self.active
        self.active = key
        self.provisional = not remember
        if remember:
            self._remember(board.name)
        if changed:
            log(f"active keyboard: {board.name} ({reason})")
            self.refresh_wanted = True
            self._mark_all()
        self._write_status()

    def _settle(self):
        """Pick an active board when there is none, or improve a provisional pick."""
        if self.active is not None and not self.provisional:
            return
        best = self._best()
        if best is None or best == self.active:
            return
        name = self.boards[best].name
        self._activate(best, "last used" if name in self.recent else "first found", remember=False)

    def _lose_active(self, name, why):
        lost = self.active
        self.active = None
        self.provisional = True
        successor = self._best(exclude=lost)
        if successor is None:
            log(f"no active keyboard: {name} {why}")
            self._write_status()
            return
        # The successor stays active when the lost board returns: remember it.
        self._activate(successor, f"{name} {why}", remember=True)

    def _drop(self, key, why):
        board = self.boards.pop(key)
        try:
            board.midi.close()
        except Exception:
            pass
        log(f"{board.name} {why}")
        if key == self.active:
            self._lose_active(board.name, why)
        self._write_status()

    # Discovery -------------------------------------------------------------

    def open(self):
        self.maintain(force=True)

    def close(self):
        for key in list(self.boards):
            board = self.boards.pop(key)
            try:
                board.midi.close()
            except Exception:
                pass
        self.active = None
        self.provisional = True

    def maintain(self, force=False):
        now = self.clock()
        if not force and self.last_scan is not None and now - self.last_scan < HEARTBEAT_SECONDS:
            return
        self.last_scan = now
        try:
            ports = self.scan()
        except Exception as error:
            message = f"cannot list MIDI ports: {error}"
            if self.failures.get("scan") != message:
                log(message)
                self.failures["scan"] = message
            return
        self.failures.pop("scan", None)
        present = {port.key for port in ports}
        for key in [key for key in self.boards if key not in present]:
            self._drop(key, "unplugged")
        self.ignored &= present
        for port in ports:
            if port.key in self.boards or port.key in self.ignored:
                continue
            self._probe(port, now)
        for key, board in list(self.boards.items()):
            self._check_echo(key, board, now)
        self._settle()
        self._write_status()

    def _probe(self, port, now):
        midi = None
        try:
            midi = port.factory()
            midi.open()
        except Exception as error:
            if midi is not None:
                try:
                    midi.close()
                except Exception:
                    pass
            message = str(error)
            if self.failures.get(port.key) != message:
                log(f"cannot open {port.name}: {message}")
                self.failures[port.key] = message
            return
        self.failures.pop(port.key, None)
        board = Board(port, midi, now)
        self.boards[port.key] = board
        if board.output_only:
            log(f"found {board.name} (output only: it cannot echo or claim)")
            self.refresh_wanted = True
        self._beat(port.key, board)

    def _check_echo(self, key, board, now):
        if board.confirmed(now):
            return
        waited = now - (board.last_echo if board.last_echo is not None else board.opened_at)
        if waited <= ECHO_STALE_SECONDS:
            return
        if board.last_echo is None and not board.named:
            # Plain MIDI gear never answers; stop sending it heartbeats.
            self.ignored.add(key)
            self._drop(key, "does not echo the Herdr protocol; ignoring it")
            return
        if not board.echo_warned:
            log(f"no heartbeat echo from {board.name}; check its MIDI route and firmware")
            board.echo_warned = True
        if key == self.active:
            self._lose_active(board.name, "stopped echoing")

    # MidiOut ---------------------------------------------------------------

    def _beat(self, key, board):
        try:
            board.midi.heartbeat()
        except Exception as error:
            self._drop(key, f"failed: {error}")
            return
        if board.confirmed(self.clock()):
            self._mark(key, board)

    def _mark(self, key, board):
        try:
            board.midi.send(CC_ACTIVE, int(key == self.active))
        except Exception as error:
            self._drop(key, f"failed: {error}")

    def _mark_all(self):
        now = self.clock()
        for key, board in list(self.boards.items()):
            if key in self.boards and board.confirmed(now):
                self._mark(key, board)

    def heartbeat(self):
        for key, board in list(self.boards.items()):
            if key in self.boards:
                self._beat(key, board)

    def send(self, control, value):
        now = self.clock()
        for key, board in list(self.boards.items()):
            if key not in self.boards or not board.confirmed(now):
                continue
            sent = value & ~CHIME_BITS if control == CC_STATE and key != self.active else value
            try:
                board.midi.send(control, sent)
            except Exception as error:
                self._drop(key, f"failed: {error}")

    def receive(self):
        messages = []
        for key, board in list(self.boards.items()):
            if key not in self.boards:
                continue
            try:
                received = board.midi.receive()
            except Exception as error:
                self._drop(key, f"failed: {error}")
                continue
            for message in received:
                if key not in self.boards:
                    break
                if self._consume(key, board, message):
                    messages.append(message)
        messages += self.pending
        self.pending = []
        return messages

    def _consume(self, key, board, message):
        """Book-keep one message; True when it belongs to the controller."""
        status, control, value = message
        now = self.clock()
        if status == MIDI_CHANNEL and control == CC_ECHO:
            if value != PROTOCOL:
                return False
            newly = not board.confirmed(now)
            board.last_echo = now
            if board.echo_warned:
                log(f"heartbeat echo from {board.name} restored")
                board.echo_warned = False
            if newly:
                log(f"found {board.name}")
                self.refresh_wanted = True
                self._settle()
                if key in self.boards:
                    self._mark(key, board)
                self._write_status()
            return False
        if key == self.active:
            return True
        if (status == NOTE_ON and value > 0) or (status == MIDI_CHANNEL and control == CC_SORT):
            # A Herdr note proves the firmware even before its first echo.
            if board.last_echo is None:
                board.last_echo = now
            self._activate(key, "claimed", remember=True)
        return False

    def rootshell_active(self):
        board = self.boards.get(self.active)
        return bool(board and board.rootshell)

    def take_refresh(self):
        wanted = self.refresh_wanted
        self.refresh_wanted = False
        return wanted

    def _write_status(self):
        if not self.state_dir:
            return
        now = self.clock()
        lines = []
        for key, board in self.boards.items():
            if key == self.active:
                role = "active"
            elif board.confirmed(now):
                role = "standby"
            else:
                role = "no echo"
            lines.append(f"{role:8} {board.name}\n")
        text = "".join(lines) or "no keyboards found\n"
        if text == self.status_text:
            return
        self.status_text = text
        try:
            with open(os.path.join(self.state_dir, "boards"), "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass


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


def read_agent_tail(pane_id):
    try:
        process = subprocess.run(
            [
                "herdr",
                "agent",
                "read",
                pane_id,
                "--source",
                "recent",
                "--lines",
                str(BLOCKED_TAIL_LINES),
                "--format",
                "text",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise BridgeError(f"herdr agent read {pane_id} timed out") from error
    if process.returncode:
        message = process.stderr.strip() or "unknown error"
        raise BridgeError(f"herdr agent read {pane_id} failed: {message}")
    return process.stdout


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

    def __init__(
        self, client=None, executor=None, clock=time.monotonic, reader=read_agent_tail
    ):
        self.client = client or TypeSafeClient()
        self.reader = reader
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
        # pane_id -> {"seq", "reason", "risk"} for each blocked episode
        self.blocked = {}

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

    def scores_for(self, agents):
        if self.score_key != self._ranking_key(agents) or any(
            agent["pane_id"] not in self.slot_scores for agent in agents
        ):
            return {}
        return self.slot_scores

    def attention_order(self, agents):
        scores = self.scores_for(agents)
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
        return self._submit_call(
            kind, context, lambda: self.client.evaluate(state, questions)
        )

    def _submit_call(self, kind, context, call):
        if not self.client.enabled or self.executor is None:
            return False
        future = self.executor.submit(call)

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

    def tracks_blocked(self):
        return bool(self.blocked)

    def _judge_blocked(self, pane_id):
        tail = self.reader(pane_id)
        return self.client.evaluate(
            {"terminal_tail": tail},
            {
                "reason": {
                    "type": "choice",
                    "instructions": (
                        "Why is the coding agent in `terminal_tail` waiting for the user?"
                    ),
                    "criteria": BLOCKED_REASONS,
                },
                "risk": {
                    "type": "score",
                    "instructions": (
                        "If the agent in `terminal_tail` is asking to approve an action, "
                        "how risky is approving it?"
                    ),
                    "criteria": APPROVAL_RISKS,
                },
            },
        )

    def _schedule_blocked(self, agents):
        blocked = {
            agent["pane_id"]: agent.get("state_change_seq")
            for agent in agents
            if agent.get("agent_status") == "blocked"
        }
        self.blocked = {
            pane_id: info
            for pane_id, info in self.blocked.items()
            if pane_id in blocked and blocked[pane_id] == info["seq"]
        }
        if not self.client.enabled:
            return
        for pane_id, seq in blocked.items():
            if pane_id in self.blocked:
                continue
            # Only a blocked agent's recent output leaves the host, once per episode.
            self.blocked[pane_id] = {"seq": seq, "reason": 0, "risk": RISK_PENDING}
            self._submit_call(
                "blocked",
                {"pane_id": pane_id, "seq": seq},
                lambda pane_id=pane_id: self._judge_blocked(pane_id),
            )

    def publish(self, midi, tracker, agents, notify):
        self.agents = [dict(agent) for agent in agents]
        self.signature = self._signature(self.agents)
        self._schedule_blocked(self.agents)
        frame = tracker.update(
            self.agents,
            notify=notify,
            scores=self.scores_for(self.agents),
            blocked=self.blocked,
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

    def refresh(self, midi, tracker):
        """Resend the last agents' frame after a new ranking, judgement, or sort."""
        frame = tracker.update(
            self.agents,
            notify=False,
            scores=self.scores_for(self.agents),
            blocked=self.blocked,
        )
        tracker.send_frame(midi, frame)

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
            self.agents,
            notify=False,
            scores=self.scores_for(self.agents),
            blocked=self.blocked,
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
        self.refresh(midi, tracker)

    def _apply_blocked(self, midi, tracker, context, answers, error):
        info = self.blocked.get(context["pane_id"])
        if info is None or info["seq"] != context["seq"]:
            return
        reason = answers.get("reason", {})
        if (
            error is None
            and reason.get("type") == "choice"
            and self._number(reason.get("confidence")) >= TYPESAFE_MIN_CONFIDENCE
        ):
            info["reason"] = REASON_CODES.get(reason.get("choice"), 0)
        info["risk"] = RISK_UNKNOWN
        risk = answers.get("risk", {})
        if (
            error is None
            and info["reason"] == REASON_CODES["permission"]
            and risk.get("type") == "score"
        ):
            probabilities = risk.get("probabilities") or {}
            high = str(len(APPROVAL_RISKS) - 1)
            if self._number(probabilities.get(high)) >= TYPESAFE_RISK_HIGH_PROBABILITY:
                info["risk"] = RISK_LEVELS[-1]
            elif self._number(risk.get("confidence")) >= TYPESAFE_MIN_CONFIDENCE:
                level = round(self._number(risk.get("score")))
                info["risk"] = RISK_LEVELS[max(0, min(level, len(RISK_LEVELS) - 1))]
        self.refresh(midi, tracker)

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
            elif kind == "blocked":
                self._apply_blocked(midi, tracker, context, answers or {}, error)


class HerdrController:
    def __init__(self, tracker, command=run_herdr, automation=None):
        self.tracker = tracker
        self.command = command
        self.automation = automation
        self.retired_logged = False

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

    def handle(self, control, value, rootshell=False):
        """True when the note ran, a reason string when skipped, else False."""
        if value not in (TAP_VELOCITY, HOLD_VELOCITY):
            return False
        held = value == HOLD_VELOCITY
        if rootshell and control in POPUPS and control not in ROOTSHELL_SHOWN:
            return f"{POPUPS[control]} is not shown in Rootshell; ignoring note {control}"
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
                "swap" if held else "focus",
                "--direction",
                directions[control],
                "--pane",
                pane["pane_id"],
            )
        elif control == NOTE_PANE_SPLIT:
            pane = self._focused_pane()
            self.command(
                "pane",
                "split",
                pane["pane_id"],
                "--direction",
                "down" if held else "right",
                "--focus",
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
        elif control == NOTE_TAB_NEW and held:
            pane = self._focused_pane()
            self.command("pane", "move", pane["pane_id"], "--new-tab", "--focus")
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
                "plugin",
                "action",
                "invoke",
                "open-tab" if held else "open",
                "--plugin",
                "herdr-lazygit",
            )
        elif control in (NOTE_PALETTE, NOTE_SMART_ACTION):
            self.command(
                "plugin", "action", "invoke", "open", "--plugin", "jt.command-palette"
            )
        elif control == NOTE_PANE_ZOOM:
            self.command("pane", "zoom", self._focused_pane()["pane_id"], "--toggle")
        elif control == NOTE_PANE_CLOSE:
            self.command("pane", "close", self._focused_pane()["pane_id"])
        elif control == NOTE_TAB_CLOSE:
            self.command("tab", "close", self._focused_pane()["tab_id"])
        elif control == NOTE_WORKSPACE_CLOSE:
            workspace = self._focused_workspace()
            if workspace is None:
                raise BridgeError("Herdr has no focused workspace")
            self.command("workspace", "close", workspace["workspace_id"])
        elif control == NOTE_HUNK:
            self.command(
                "plugin", "action", "invoke", "worktree-tab", "--plugin", "hunk.diff"
            )
        elif control in (NOTE_AGENT_NEXT, NOTE_AGENT_PREV, NOTE_AGENT_URGENT):
            agents = self.command("agent", "list")["agents"]
            if not agents:
                raise BridgeError("Herdr has no live agents")
            panes = (
                self.automation.attention_order(agents)
                if self.automation is not None
                else None
            )
            if control == NOTE_AGENT_URGENT:
                # Without a confident ranking, blocked agents still come first.
                panes = panes or [
                    agent["pane_id"]
                    for agent in sorted(
                        agents,
                        key=lambda agent: (
                            -STATUS_RANK.get(agent.get("agent_status"), 0),
                            agent.get("state_change_seq", 0),
                        ),
                    )
                ]
                self.command("agent", "focus", panes[0])
                return True
            # Next and Previous walk the keyboard's slot order.
            scores = (
                self.automation.scores_for(agents)
                if self.automation is not None
                else {}
            )
            panes = self.tracker.order(agents, scores)
            focused = self._focused_pane()["pane_id"]
            if focused in panes:
                delta = 1 if control == NOTE_AGENT_NEXT else -1
                target = panes[(panes.index(focused) + delta) % len(panes)]
            else:
                target = panes[0] if control == NOTE_AGENT_NEXT else panes[-1]
            self.command("agent", "focus", target)
        elif control in (NOTE_ACCEPT, NOTE_REJECT, NOTE_CLEAR):
            keys = {NOTE_ACCEPT: "enter", NOTE_REJECT: "esc", NOTE_CLEAR: "ctrl+c"}
            self.command(
                "agent", "send-keys", self._focused_pane()["pane_id"], keys[control]
            )
        elif control == NOTE_PROMPT:
            if self.retired_logged:
                return False
            self.retired_logged = True
            return "note 126 is retired: clipboard prompts were removed; remap the key"
        else:
            return False
        return True

    def poll(self, midi):
        """Run keyboard controls; True when the keyboard changed its sort mode."""
        sorted_changed = False
        rootshell = getattr(midi, "rootshell_active", lambda: False)
        for status, control, value in midi.receive():
            if status == MIDI_CHANNEL and control == CC_SORT:
                sort = SORT_GROUPED if value == SORT_GROUPED else SORT_PRIORITY
                if sort != self.tracker.sort:
                    self.tracker.sort = sort
                    sorted_changed = True
                    log(f"panel sorted by {'grouped' if sort else 'priority'}")
                continue
            if status == NOTE_OFF:
                value = 0
            elif status != NOTE_ON:
                continue
            try:
                handled = self.handle(control, value, rootshell())
                if handled is True:
                    log(f"control note {control}")
                elif handled:
                    log(handled)
            except Exception as error:
                log(f"control note {control} failed: {error}")
        return sorted_changed


class PanelSort:
    """Sync Herdr's persisted built-in sort without owning an agent-view override."""

    def __init__(self, tracker, path=None, reload=None, clear_view=None):
        self.tracker = tracker
        self.path = path or os.environ.get("HERDR_CONFIG_PATH") or os.path.expanduser("~/.config/herdr/config.toml")
        self.reload = reload or (lambda: run_herdr("server", "reload-config"))
        self.clear_view = clear_view or (lambda: None)
        self.stamp = None
        self.mode = SORT_PRIORITY

    def request(self):
        """Apply the keyboard's requested mode and show it."""
        self.write()
        # Any agent view (say a herdr-projects focus) hides the panel sort,
        # so a keyboard request drops it; a failed write leaves it alone.
        self.clear_view()

    def read(self):
        stamp = os.stat(self.path)
        if stamp != self.stamp:
            with open(self.path, "rb") as handle:
                mode = tomllib.load(handle).get("ui", {}).get("agent_panel_sort", "spaces")
            if mode not in ("priority", "spaces", "workspaces"):
                raise BridgeError(f"unsupported Herdr panel sort: {mode}")
            self.mode = SORT_PRIORITY if mode == "priority" else SORT_GROUPED
            self.stamp = stamp
        changed = self.mode != self.tracker.sort
        self.tracker.sort = self.mode
        return changed

    def write(self):
        if os.path.islink(self.path):
            raise BridgeError("Herdr config is a symlink; activate Panix's writable config first")
        stamp = os.stat(self.path)
        with open(self.path, encoding="utf-8") as handle:
            content = handle.read()
        mode = "spaces" if self.tracker.sort == SORT_GROUPED else "priority"
        # Only edit the [ui] section, preserving unrelated settings and comments.
        section = re.search(r"(?ms)^\[ui\][^\n]*\n.*?(?=^\s*\[|\Z)", content)
        setting = f'agent_panel_sort = "{mode}"'
        if section:
            body = section.group()
            if re.search(r"(?m)^\s*agent_panel_sort\s*=", body):
                body = re.sub(r"(?m)^(\s*agent_panel_sort\s*=\s*)[^\n#]*(.*)$", lambda m: m[1] + f'"{mode}" ' + m[2], body)
            else:
                body = body.rstrip() + "\n" + setting + "\n\n"
            updated = content[:section.start()] + body + content[section.end():]
        else:
            updated = content.rstrip() + "\n\n[ui]\n" + setting + "\n"
        tomllib.loads(updated)  # Never replace a valid config with invalid TOML.
        if updated == content:
            return
        fd, temp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, stamp.st_mode & 0o777)
            if os.stat(self.path) != stamp:
                raise BridgeError("Herdr config changed concurrently; retry the toggle")
            os.replace(temp, self.path)
            written = os.stat(self.path)
            try:
                result = self.reload()
                if isinstance(result, dict) and result.get("status") == "failed":
                    raise BridgeError(f"Herdr config reload failed: {result}")
            except Exception:
                # Restore only our write, never clobber a concurrent edit.
                if os.stat(self.path) == written:
                    fd, temp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)))
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(content)
                    os.chmod(temp, stamp.st_mode & 0o777)
                    os.replace(temp, self.path)
                raise
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


class Tracker:
    def __init__(self):
        self.slots = [None] * SLOT_COUNT
        self.colors = {}
        self.sort = SORT_PRIORITY
        self.previous = {}

    def order(self, agents, scores=None):
        """Pane ids in Herdr's built-in panel order, first slot first."""
        # Herdr's list already has workspace/tab/layout order. Stable sorting
        # preserves that order for priority ties. AI scores never reorder slots.
        if self.sort == SORT_GROUPED:
            return [agent["pane_id"] for agent in agents]
        priority = {"blocked": 4, "done": 3, "working": 2, "idle": 1, "unknown": 0}
        return [agent["pane_id"] for agent in sorted(agents, key=lambda agent: (
            -priority.get(agent.get("agent_status"), 0),
            -agent.get("state_change_seq", 0),
        ))]

    def update(self, agents, notify, scores=None, blocked=None):
        blocked = blocked or {}
        wanted = self.order(agents, scores)[:SLOT_COUNT]
        self.slots = wanted + [None] * (SLOT_COUNT - len(wanted))
        # Each agent on the board keeps its own color until it leaves.
        self.colors = {s: c for s, c in self.colors.items() if s in wanted}
        for pane_id in wanted:
            if pane_id not in self.colors:
                free = set(range(SLOT_COUNT)) - set(self.colors.values())
                self.colors[pane_id] = min(free)

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
                (
                    STATUS_CODES.get(current[s], EMPTY_SLOT)
                    | (blocked.get(s, {}).get("reason", 0) << 3)
                    | (self.colors[s] << 5)
                    if s is not None
                    else EMPTY_SLOT
                )
                for s in self.slots
            ],
            "risk": next(
                (
                    blocked.get(a["pane_id"], {}).get("risk", RISK_NONE)
                    for a in agents
                    if a.get("focused") and a["agent_status"] == "blocked"
                ),
                RISK_NONE,
            ),
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
        midi.send(CC_SORT, self.sort)
        for index, status in enumerate(frame["slots"]):
            midi.send(CC_SLOT_FIRST + index, status)
        midi.send(CC_RISK, frame["risk"])
        value = STATUS_CODES.get(frame["aggregate"], 4)
        value |= frame["any_working"] << 3
        value |= frame["overflow"] << 4
        value |= frame["chime_done"] << 5
        value |= frame["chime_blocked"] << 6
        midi.send(CC_STATE, value)


def herdr_request(socket_path, method, params):
    """Send one socket API request on its own connection and return its result."""
    request = json.dumps({"id": "qmk-herdr", "method": method, "params": params})
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(HEARTBEAT_SECONDS)
        sock.connect(socket_path)
        sock.sendall(request.encode() + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise BridgeError(f"{method} connection closed")
            data += chunk
    try:
        response = json.loads(data.split(b"\n", 1)[0])
    except ValueError as error:
        raise BridgeError(f"malformed {method} response: {error}") from error
    if "error" in response:
        error = response["error"]
        raise BridgeError(f"{method} failed: {error.get('code')}: {error.get('message')}")
    return response.get("result")


def snapshot(socket_path):
    result = herdr_request(socket_path, "session.snapshot", {})
    try:
        return result["snapshot"]["agents"]
    except (KeyError, TypeError) as error:
        raise BridgeError(f"malformed snapshot response: {error}") from error


class AgentNames:
    """Publish each agent's name to Herdr in its keyboard color."""

    def __init__(self, socket_path, request=herdr_request, clock=time.monotonic):
        self.socket_path = socket_path
        self.request = request
        self.clock = clock
        self.shown = {}
        self.refreshed = None

    @staticmethod
    def wanted(agents, colors):
        """pane_id -> (token, name) with the same label as Herdr's agent field."""
        names = {}
        for agent in agents:
            name = agent.get("display_agent") or agent.get("agent")
            if name:
                color = colors.get(agent["pane_id"])
                token = PLAIN_TOKEN if color is None else COLOR_TOKENS[color]
                names[agent["pane_id"]] = (token, name)
        return names

    def sync(self, agents, colors):
        wanted = self.wanted(agents, colors)
        now = self.clock()
        due = self.refreshed is None or now - self.refreshed >= NAME_REFRESH_SECONDS
        if not due and wanted == self.shown:
            return
        for pane_id in sorted(self.shown.keys() - wanted.keys()):
            self._report(pane_id, None)
        for pane_id, shown in wanted.items():
            if due or self.shown.get(pane_id) != shown:
                self._report(pane_id, shown)
        self.shown = wanted
        if due:
            self.refreshed = now

    def _report(self, pane_id, shown):
        token, name = shown or (None, None)
        params = {
            "pane_id": pane_id,
            "source": METADATA_SOURCE,
            "tokens": {t: name if t == token else None for t in NAME_TOKENS},
        }
        if shown is not None:
            params["ttl_ms"] = NAME_TTL_MS
        try:
            self.request(self.socket_path, "pane.report_metadata", params)
        except (BridgeError, OSError) as error:
            # A pane that just closed has no name left to show.
            if "pane_not_found" not in str(error):
                log(f"agent name for {pane_id} failed: {error}")


# Events that can change what the keyboards show. Agent status needs a
# subscription per pane, so the set is renewed whenever the agents change;
# focus and layout events let LEDs follow the session without waiting for
# the heartbeat snapshot.
EVENT_TYPES = [
    "pane.agent_detected",
    "pane.closed",
    "pane.exited",
    "pane.focused",
    "pane.moved",
    "tab.focused",
    "tab.moved",
    "tab.closed",
    "workspace.focused",
    "workspace.moved",
    "workspace.reordered",
    "workspace.closed",
    "layout.updated",
]


def subscription_request(agents):
    subscriptions = [{"type": kind} for kind in EVENT_TYPES] + [
        {"type": "pane.agent_status_changed", "pane_id": a["pane_id"]} for a in agents
    ]
    return json.dumps(
        {
            "id": "qmk-herdr-subscribe",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions},
        }
    ).encode()


def open_events(socket_path, agents):
    """Subscribe on a new connection; the socket and any bytes past the ack."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
        sock.settimeout(HEARTBEAT_SECONDS)
        sock.sendall(subscription_request(agents) + b"\n")
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
    except BaseException:
        sock.close()
        raise
    sock.settimeout(MIDI_POLL_SECONDS)
    return sock, data


def watch_session(socket_path, midi, tracker, controller, automation, names=None):
    names = names or AgentNames(socket_path)
    panel_sort = PanelSort(
        tracker, clear_view=lambda: herdr_request(socket_path, "agent.view.clear", {})
    )
    panel_sort.read()
    initial = snapshot(socket_path)
    sock, data = open_events(socket_path, initial)
    stream = {"sock": sock, "data": data, "subscribed": {a["pane_id"] for a in initial}}

    def publish(agents, notify):
        """Show the agents; renew the subscription when the set changed."""
        automation.publish(midi, tracker, agents, notify=notify)
        panes = {a["pane_id"] for a in agents}
        if panes == stream["subscribed"]:
            return
        sock, data = open_events(socket_path, agents)
        stream["sock"].close()
        stream.update(sock=sock, data=data, subscribed=panes)
        # Catch whatever changed while the new stream was starting.
        automation.publish(midi, tracker, snapshot(socket_path), notify=True)

    try:
        publish(snapshot(socket_path), notify=False)
        last_heartbeat = time.monotonic()
        while True:
            midi.maintain()
            sorted_changed = controller.poll(midi)
            if sorted_changed:
                try:
                    panel_sort.request()
                except Exception as error:
                    log(f"cannot change Herdr panel sorting: {error}")
                    panel_sort.read()
            panel_changed = panel_sort.read()
            if midi.take_refresh() or sorted_changed or panel_changed:
                automation.refresh(midi, tracker)
            automation.poll(midi, tracker)
            names.sync(automation.agents, tracker.colors)
            lines = stream["data"].split(b"\n")
            if any(line.strip() for line in lines[:-1]):
                # A burst of events costs one snapshot.
                stream["data"] = lines[-1]
                publish(snapshot(socket_path), notify=True)
                last_heartbeat = time.monotonic()
            try:
                chunk = stream["sock"].recv(4096)
            except TimeoutError:
                if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                    publish(snapshot(socket_path), notify=True)
                    last_heartbeat = time.monotonic()
                continue
            if not chunk:
                raise BridgeError("Herdr event stream closed")
            stream["data"] += chunk
    finally:
        stream["sock"].close()


def run(socket_path, port_list, state_dir=None):
    # The fleet outlives Herdr reconnects: boards and the active choice stay put.
    midi = KeyboardFleet.from_config(port_list, state_dir)
    midi.open()
    tracker = Tracker()
    automation = TypeSafeAutomation()
    controller = HerdrController(tracker, automation=automation)
    last_error = None
    # Names outlive resubscribes so agents that left get cleared; the periodic
    # refresh republishes them all after a Herdr restart.
    names = AgentNames(socket_path)
    while True:
        try:
            watch_session(socket_path, midi, tracker, controller, automation, names)
        except (
            Exception
        ) as error:  # any failure becomes a logged retry, never a dead daemon
            message = f"{error}; reconnecting"
            if message != last_error:
                log(message)
                last_error = message
            time.sleep(1)


# Lifecycle -------------------------------------------------------------------


def _flock_struct(lock_type):
    """A struct flock covering the whole file, in this platform's layout."""
    if sys.platform == "darwin":
        return struct.pack("qqihh", 0, 0, 0, lock_type, os.SEEK_SET)
    return struct.pack("hhqqi4x", lock_type, os.SEEK_SET, 0, 0, 0)


def _flock_holder(data):
    if sys.platform == "darwin":
        _start, _length, pid, lock_type, _whence = struct.unpack("qqihh", data)
    else:
        lock_type, _whence, _start, _length, pid = struct.unpack("hhqqi4x", data)
    return None if lock_type == fcntl.F_UNLCK else pid


def running_pid(state_dir):
    """Pid of the bridge holding the state dir's lock, or None.

    F_GETLK only asks, so a status check never races a bridge starting up.
    """
    try:
        fd = os.open(os.path.join(state_dir, LOCK_FILE), os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        return _flock_holder(fcntl.fcntl(fd, fcntl.F_GETLK, _flock_struct(fcntl.F_WRLCK)))
    finally:
        os.close(fd)


_held_lock = None


def hold_lock(state_dir):
    """Take the state dir's lock for the rest of this process; False when taken.

    A POSIX lock drops when its owner closes any descriptor of the file, so
    only this function ever opens it in the bridge.
    """
    global _held_lock
    fd = os.open(os.path.join(state_dir, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _held_lock = fd
    return True


def stray_bridges(listing, uid, exclude=()):
    """Pids in `ps -Ao pid=,uid=,command=` output running a pre-lock bridge."""
    pids = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) < 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        pid = int(fields[0])
        if int(fields[1]) != uid or pid in exclude:
            continue
        command = fields[2:]
        script = next(
            (index for index, arg in enumerate(command[:2]) if LEGACY_BRIDGE_RE.search(arg)),
            None,
        )
        if script is None:
            continue
        rest = command[script + 1 :]
        # Lock holders run `run`; lifecycle calls and tests are not daemons.
        if rest and rest[0] in LIFECYCLE_COMMANDS + ("run", "--self-test"):
            continue
        pids.append(pid)
    return pids


def find_strays(holder):
    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,uid=,command="],
            capture_output=True,
            text=True,
            check=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return stray_bridges(listing, os.getuid(), {os.getpid(), holder})


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + STOP_SECONDS
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stop_bridges(state_dir):
    holder = running_pid(state_dir)
    strays = find_strays(holder)
    if strays:
        print(f"qmk-herdr: stopping pre-lock bridges: {', '.join(map(str, strays))}")
    terminate(([holder] if holder else []) + strays)
    # The pid file from before the lock only ever named one of them.
    try:
        os.unlink(os.path.join(state_dir, "qmk-herdr.pid"))
    except FileNotFoundError:
        pass
    return holder is not None or bool(strays)


def start_bridge(state_dir, config_dir, command=None):
    holder = running_pid(state_dir)
    strays = find_strays(holder)
    if strays:
        print(f"qmk-herdr: stopping pre-lock bridges: {', '.join(map(str, strays))}")
        terminate(strays)
    if holder is not None:
        print(f"qmk-herdr is running (pid {holder})")
        return 0
    if command is None:
        command = [sys.executable, os.path.abspath(__file__), "run"]
        try:
            with open(os.path.join(config_dir, "midi-port"), encoding="utf-8") as handle:
                port_list = handle.read().strip()
        except FileNotFoundError:
            port_list = ""
        if port_list:
            command.append(port_list)
    log_path = os.path.join(state_dir, LOG_FILE)
    with open(log_path, "w", encoding="utf-8") as log_file:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + START_SECONDS
    while time.monotonic() < deadline:
        if running_pid(state_dir) == child.pid:
            print(f"qmk-herdr started (pid {child.pid})")
            return 0
        if child.poll() is not None:
            break
        time.sleep(0.05)
    if child.poll() is None:
        terminate([child.pid])
    with open(log_path, encoding="utf-8") as handle:
        sys.stderr.write(handle.read())
    print("qmk-herdr failed to start", file=sys.stderr)
    return 1


def bridge_status(state_dir):
    holder = running_pid(state_dir)
    strays = find_strays(holder)
    if strays:
        print(f"pre-lock bridges also running: {', '.join(map(str, strays))}; restart stops them")
    if holder is None:
        print("qmk-herdr is stopped")
        try:
            with open(os.path.join(state_dir, LOG_FILE), encoding="utf-8") as handle:
                sys.stdout.write("".join(handle.readlines()[-20:]))
        except FileNotFoundError:
            pass
        return 1
    print(f"qmk-herdr is running (pid {holder})")
    try:
        with open(os.path.join(state_dir, "boards"), encoding="utf-8") as handle:
            sys.stdout.write(handle.read())
    except FileNotFoundError:
        pass
    return 0


def lifecycle(action, state_dir, config_dir):
    os.makedirs(state_dir, exist_ok=True)
    if action == "status":
        return bridge_status(state_dir)
    if action in ("stop", "restart"):
        stop_bridges(state_dir)
        if action == "stop":
            print("qmk-herdr stopped")
            return 0
    return start_bridge(state_dir, config_dir)


def self_test():
    tracker = Tracker()
    first = tracker.update(
        [
            {"pane_id": "p2", "agent_status": "working", "state_change_seq": 2},
            {"pane_id": "p1", "agent_status": "idle", "state_change_seq": 1},
        ],
        notify=False,
    )
    assert first["slots"] == [1, 0 | 1 << 5, EMPTY_SLOT, EMPTY_SLOT], first
    assert not first["chime_done"]

    second = tracker.update(
        [
            {"pane_id": "p2", "agent_status": "done", "state_change_seq": 3},
            {"pane_id": "p1", "agent_status": "blocked", "state_change_seq": 4},
        ],
        notify=True,
    )
    # The blocked agent moves first and both keep their colors.
    assert second["slots"] == [2 | 1 << 5, 3, EMPTY_SLOT, EMPTY_SLOT], second
    assert second["aggregate"] == "blocked"
    assert second["chime_done"] and second["chime_blocked"], second

    overflow = tracker.update(
        [
            {"pane_id": f"p{i}", "agent_status": "idle", "state_change_seq": i}
            for i in range(1, 6)
        ],
        notify=True,
    )
    assert overflow["overflow"], overflow
    assert overflow["slots"] == [1 << 5, 2 << 5, 3 << 5, 0], overflow
    assert tracker.slots == ["p5", "p4", "p3", "p2"]

    tracker.sort = SORT_GROUPED
    recent = tracker.update(
        [
            {"pane_id": f"p{i}", "agent_status": "idle", "state_change_seq": i}
            for i in range(1, 6)
        ],
        notify=False,
    )
    assert tracker.slots == ["p1", "p2", "p3", "p4"]
    assert recent["slots"] == [1 << 5, 0, 3 << 5, 2 << 5], recent
    tracker.sort = SORT_PRIORITY

    # Metadata tests use a fixed color fixture, independent of ordering.
    tracker.colors = {"p2": 0, "p3": 2, "p4": 3, "p5": 1}

    # Agent names follow the keyboard colors: each agent carries its name in
    # exactly one token, agents off the board get the plain one, departed
    # agents are cleared, and every name is refreshed on time.
    reports = []
    names_now = [0.0]

    def fake_report(socket_path, method, params):
        assert method == "pane.report_metadata" and params["source"] == METADATA_SOURCE
        assert set(params["tokens"]) == set(NAME_TOKENS)
        reports.append(params)
        if params["pane_id"] == "gone":
            raise BridgeError("pane.report_metadata failed: pane_not_found: gone")

    def shown():
        return {
            r["pane_id"]: {t: v for t, v in r["tokens"].items() if v is not None}
            for r in reports
        }

    named = [
        {"pane_id": f"p{i}", "agent": "claude" if i != 1 else "pi"}
        for i in range(1, 6)
    ]
    named[2]["display_agent"] = "reviewer"
    names = AgentNames("sock", request=fake_report, clock=lambda: names_now[0])
    names.sync(named, dict(tracker.colors))
    assert shown() == {
        "p1": {"qmk_agent": "pi"},
        "p2": {"qmk_blue": "claude"},
        "p3": {"qmk_teal": "reviewer"},
        "p4": {"qmk_mauve": "claude"},
        "p5": {"qmk_yellow": "claude"},
    }, reports
    assert all(r["ttl_ms"] == NAME_TTL_MS for r in reports)
    reports.clear()
    names.sync(named, dict(tracker.colors))
    assert reports == []
    moved = named[:1] + named[3:] + [{"pane_id": "gone", "agent": "claude"}]
    names.sync(moved, {"p5": 1, "p4": 0, "gone": 2})
    assert shown() == {
        "p2": {},
        "p3": {},
        "p4": {"qmk_blue": "claude"},
        "gone": {"qmk_teal": "claude"},
    }, reports
    assert all("ttl_ms" not in r for r in reports if r["pane_id"] in ("p2", "p3"))
    reports.clear()
    names_now[0] += NAME_REFRESH_SECONDS
    names.sync(moved[:-1], {"p5": 1, "p4": 0})
    assert sorted(r["pane_id"] for r in reports) == ["gone", "p1", "p4", "p5"], reports

    request = subscription_request([{"pane_id": "w1:p1"}])
    assert request.count(b'"type"') == len(EVENT_TYPES) + 1
    assert b'"type": "pane.focused"' in request
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
    assert not alsa_client_linked("QMK bridge in", "Connected From", seq_clients)
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
            self.events = [
                ([MIDI_CHANNEL, CC_ECHO, PROTOCOL], 0.0),
                ([NOTE_ON, NOTE_ACCEPT, 127], 0.0),
            ]
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
    fake_rtmidi.__dict__.update(MidiOut=FakeRtMidiOut, MidiIn=FakeRtMidiIn)
    sys.modules["rtmidi"] = fake_rtmidi
    try:
        output = RtMidiOut("HERDR-IPAD")
        output.open()
        output.send(CC_HEARTBEAT, PROTOCOL)
        received = output.receive()
        # Echoes pass through: the fleet uses them to confirm the firmware.
        expected_messages = [(MIDI_CHANNEL, CC_ECHO, PROTOCOL), (NOTE_ON, NOTE_ACCEPT, 127)]
        assert received == expected_messages
        output.close()
        output_instance = FakeRtMidiOut.instances[-1]
        input_instance = FakeRtMidiIn.instances[-1]
        assert output_instance.opened == 1 and input_instance.opened == 1
        assert output_instance.sent == [[MIDI_CHANNEL, CC_HEARTBEAT, PROTOCOL]]
        assert output_instance.closed and input_instance.closed

        ports_available[0] = False
        missing = RtMidiOut("qmk-herdr-ipad")
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
                        "tab_id": "w1:t1",
                        "cwd": "/project",
                        "focused": True,
                    }
                ]
            }
        if args == ("agent", "list"):
            return {"agents": [{"pane_id": "w1:p1"}, {"pane_id": "w2:p3"}]}
        commands.append(args)
        return {}

    tracker.slots = ["w1:p2", None, None, None]
    controller = HerdrController(tracker, command=fake_herdr)
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
        NOTE_PANE_CLOSE,
        NOTE_TAB_CLOSE,
        NOTE_WORKSPACE_CLOSE,
        NOTE_HUNK,
        NOTE_AGENT_NEXT,
        NOTE_AGENT_PREV,
        NOTE_AGENT_URGENT,
        NOTE_SMART_ACTION,
        NOTE_ACCEPT,
        NOTE_REJECT,
        NOTE_CLEAR,
    ):
        assert controller.handle(control, 127) is True, control
    expected_commands = [
        ("workspace", "focus", "w2"),
        ("tab", "focus", "w1:t2"),
        ("plugin", "action", "invoke", "open", "--plugin", "lancodev.jump"),
        ("plugin", "action", "invoke", "toggle", "--plugin", "herdr-floax"),
        ("workspace", "create", "--cwd", "/project", "--focus"),
        ("tab", "create", "--workspace", "w1", "--cwd", "/project", "--focus"),
        ("plugin", "action", "invoke", "open", "--plugin", "herdr-lazygit"),
        ("plugin", "action", "invoke", "open", "--plugin", "jt.command-palette"),
        ("pane", "zoom", "w1:p1", "--toggle"),
        ("pane", "close", "w1:p1"),
        ("tab", "close", "w1:t1"),
        ("workspace", "close", "w1"),
        ("plugin", "action", "invoke", "worktree-tab", "--plugin", "hunk.diff"),
        ("agent", "focus", "w2:p3"),
        ("agent", "send-keys", "w1:p1", "enter"),
        ("agent", "send-keys", "w1:p1", "esc"),
        ("agent", "send-keys", "w1:p1", "ctrl+c"),
    ]
    assert all(command in commands for command in expected_commands)

    # Holds pick each key's variant; keys without one treat a hold as a tap.
    commands.clear()
    for control, velocity in (
        (NOTE_PANE_SPLIT, TAP_VELOCITY),
        (NOTE_PANE_LEFT, HOLD_VELOCITY),
        (NOTE_TAB_NEW, HOLD_VELOCITY),
        (NOTE_LAZYGIT, HOLD_VELOCITY),
        (NOTE_PANE_SPLIT, HOLD_VELOCITY),
        (NOTE_ACCEPT, HOLD_VELOCITY),
    ):
        assert controller.handle(control, velocity)
    assert commands == [
        ("pane", "split", "w1:p1", "--direction", "right", "--focus"),
        ("pane", "swap", "--direction", "left", "--pane", "w1:p1"),
        ("pane", "move", "w1:p1", "--new-tab", "--focus"),
        ("plugin", "action", "invoke", "open-tab", "--plugin", "herdr-lazygit"),
        ("pane", "split", "w1:p1", "--direction", "down", "--focus"),
        ("agent", "send-keys", "w1:p1", "enter"),
    ], commands
    assert not controller.handle(NOTE_PANE_SPLIT, 1), "unknown velocities are ignored"

    class FakeControlMidi:
        def receive(self):
            return [
                (MIDI_CHANNEL, NOTE_ACCEPT, 127),
                (NOTE_OFF, NOTE_ACCEPT, 127),
                (NOTE_ON, NOTE_ACCEPT, 127),
            ]

    commands.clear()
    assert not controller.poll(FakeControlMidi())
    expected = [("agent", "send-keys", "w1:p1", "enter")]
    assert commands == expected, commands

    class FakeSortMidi:
        def receive(self):
            return [(MIDI_CHANNEL, CC_ECHO, PROTOCOL), (MIDI_CHANNEL, CC_SORT, 1)]

    def recency_herdr(*args):
        if args == ("agent", "list"):
            return {
                "agents": [
                    {
                        "pane_id": "w1:p1",
                        "agent_status": "blocked",
                        "state_change_seq": 3,
                    },
                    {"pane_id": "w1:p2", "agent_status": "idle", "state_change_seq": 1},
                    {"pane_id": "w2:p3", "agent_status": "idle", "state_change_seq": 2},
                ]
            }
        return fake_herdr(*args)

    sort_tracker = Tracker()
    sort_controller = HerdrController(sort_tracker, command=recency_herdr)
    assert sort_controller.handle(NOTE_AGENT_NEXT, 127)
    assert commands[-1] == ("agent", "focus", "w2:p3"), "priority: p1, p3, p2"
    assert sort_controller.poll(FakeSortMidi()) and sort_tracker.sort == SORT_GROUPED
    assert not sort_controller.poll(FakeSortMidi()), "unchanged mode needs no frame"
    assert sort_controller.handle(NOTE_AGENT_NEXT, 127)
    assert commands[-1] == ("agent", "focus", "w1:p2"), "grouped: p1, p2, p3"
    assert sort_controller.handle(NOTE_AGENT_URGENT, 127)
    assert commands[-1] == ("agent", "focus", "w1:p1"), "urgent ignores grouped"

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
    smart_controller = HerdrController(tracker, command=fake_herdr, automation=smart)
    commands.clear()
    assert smart_controller.handle(NOTE_SMART_ACTION, 127)
    expected = [("plugin", "action", "invoke", "open", "--plugin", "jt.command-palette")]
    assert commands == expected
    assert client.calls == []

    # The retired clipboard note says so once, then stays quiet.
    assert "retired" in smart_controller.handle(NOTE_PROMPT, 127)
    assert smart_controller.handle(NOTE_PROMPT, 127) is False
    assert commands == expected

    # On a Rootshell board, popups it may not draw do nothing; tabs still work.
    for control in POPUPS:
        assert "Rootshell" in smart_controller.handle(control, 127, rootshell=True)
    assert commands == expected
    assert smart_controller.handle(NOTE_HUNK, 127, rootshell=True) is True
    assert parse_port_list(" rtmidi:qmk-herdr-ipad@rootshell | Moonlander|rtmidi: other |rtmidi:") == (
        [("qmk-herdr-ipad", True), ("other", False)],
        ["Moonlander"],
    )

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
    assert ranked_tracker.slots == ["p5", "p4", "p3", "p2"]
    _, rank_questions = ranked_client.calls[-1]
    assert rank_questions["role_0"]["type"] == "choice"
    assert rank_questions["attention_0"]["type"] == "score"
    ranked.poll(ranked_midi, ranked_tracker)
    assert ranked_tracker.slots == ["p5", "p4", "p3", "p2"], "AI must not reorder the panel slots"
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
    expected_attention_focus = ("agent", "focus", "w1:p2")
    assert commands[-1] == expected_attention_focus
    assert attention_controller.handle(NOTE_AGENT_PREV, 127)
    assert commands[-1] == ("agent", "focus", "w2:p3")
    assert attention_controller.handle(NOTE_AGENT_URGENT, 127)
    assert commands[-1] == ("agent", "focus", "w1:p1")

    def urgent_herdr(*args):
        if args == ("agent", "list"):
            return {
                "agents": [
                    {"pane_id": "w1:p1", "agent_status": "idle"},
                    {"pane_id": "w1:p2", "agent_status": "blocked"},
                ]
            }
        return fake_herdr(*args)

    urgent_controller = HerdrController(tracker, command=urgent_herdr)
    assert urgent_controller.handle(NOTE_AGENT_URGENT, 127)
    assert commands[-1] == ("agent", "focus", "w1:p2")

    class BlockedClient:
        enabled = True

        def __init__(self):
            self.calls = []
            self.reason = "permission"
            self.probabilities = {"0": 0.8, "1": 0.2, "2": 0.0}

        def evaluate(self, state, questions):
            self.calls.append((state, questions))
            if "reason" not in questions:
                return {}
            return {
                "reason": {"type": "choice", "choice": self.reason, "confidence": 0.95},
                "risk": {
                    "type": "score",
                    "score": sum(
                        int(level) * probability
                        for level, probability in self.probabilities.items()
                    ),
                    "confidence": 0.9,
                    "probabilities": self.probabilities,
                },
            }

    blocked_client = BlockedClient()
    reads = []
    judge = TypeSafeAutomation(
        blocked_client,
        executor=ImmediateExecutor(),
        reader=lambda pane_id: reads.append(pane_id) or "Allow `ls`? (y/n)",
    )
    judge_tracker = Tracker()
    judge_midi = FakeMidi()

    def risk_sent():
        return [
            item[1] for item in judge_midi.sent if item != "hb" and item[0] == CC_RISK
        ]

    def slot_sent(index):
        return [
            item[1]
            for item in judge_midi.sent
            if item != "hb" and item[0] == CC_SLOT_FIRST + index
        ]

    waiting = [
        {
            "pane_id": "b1",
            "agent_status": "blocked",
            "state_change_seq": 3,
            "focused": True,
        },
        {"pane_id": "b2", "agent_status": "idle", "state_change_seq": 1},
    ]
    judge.publish(judge_midi, judge_tracker, waiting, notify=False)
    assert risk_sent()[-1] == RISK_PENDING and reads == ["b1"]
    judged = [
        state for state, questions in blocked_client.calls if "reason" in questions
    ]
    assert judged == [{"terminal_tail": "Allow `ls`? (y/n)"}]
    assert judge.tracks_blocked()
    judge.poll(judge_midi, judge_tracker)
    assert risk_sent()[-1] == RISK_LEVELS[0]
    assert (
        slot_sent(judge_tracker.slots.index("b1"))[-1]
        == STATUS_CODES["blocked"] | 1 << 3
    )
    judge.publish(judge_midi, judge_tracker, waiting, notify=True)
    assert reads == ["b1"], "one request per blocked episode"

    blocked_client.probabilities = {"0": 0.3, "1": 0.3, "2": 0.4}
    waiting[0]["state_change_seq"] = 5
    judge.publish(judge_midi, judge_tracker, waiting, notify=True)
    judge.poll(judge_midi, judge_tracker)
    assert risk_sent()[-1] == RISK_LEVELS[-1]

    blocked_client.reason = "question"
    waiting[0]["state_change_seq"] = 7
    judge.publish(judge_midi, judge_tracker, waiting, notify=True)
    judge.poll(judge_midi, judge_tracker)
    assert risk_sent()[-1] == RISK_UNKNOWN
    assert (
        slot_sent(judge_tracker.slots.index("b1"))[-1]
        == STATUS_CODES["blocked"] | 2 << 3
    )

    waiting[0]["focused"] = False
    judge.publish(judge_midi, judge_tracker, waiting, notify=True)
    assert risk_sent()[-1] == RISK_NONE
    waiting[0]["agent_status"] = "done"
    judge.publish(judge_midi, judge_tracker, waiting, notify=True)
    assert not judge.tracks_blocked()

    failing = TypeSafeAutomation(
        FakeTypeSafeClient(fail=True),
        executor=ImmediateExecutor(),
        reader=lambda pane_id: "",
    )
    failing_midi = FakeMidi()
    waiting[0].update(agent_status="blocked", focused=True, state_change_seq=9)
    failing_tracker = Tracker()
    failing.publish(failing_midi, failing_tracker, waiting, notify=False)
    failing.poll(failing_midi, failing_tracker)
    failed_risk = [
        item[1] for item in failing_midi.sent if item != "hb" and item[0] == CC_RISK
    ]
    assert failed_risk[-1] == RISK_UNKNOWN

    cards_text = """ 0 [SoloCast       ]: USB-Audio - HyperX SoloCast
                      HP, Inc HyperX SoloCast at usb-0000:0a:00.3-2, full speed
 4 [Glow           ]: USB-Audio - Planck EZ Glow
                      ZSA Technology Labs Planck EZ Glow at usb-0000:0a:00.3-1.3.1.2, full speed
 5 [I              ]: USB-Audio - Moonlander Mark I
                      ZSA Technology Labs Moonlander Mark I at usb-0000:0a:00.3-1.3.3, full speed
"""
    with_nodes = lambda pattern: [pattern] if pattern.startswith(("/dev/snd/midiC4", "/dev/snd/midiC5")) else []
    assert alsa_midi_cards(cards_text, with_nodes) == [(4, "Planck EZ Glow"), (5, "Moonlander Mark I")]

    class FakeBoard(MidiOut):
        """A keyboard port: Herdr firmware echoes heartbeats, other gear stays silent."""

        def __init__(self, name, herdr=True, sort=None):
            super().__init__(name)
            self.herdr = herdr
            self.sort = sort
            self.sent = []
            self.inbox = []
            self.closed = False

        def open(self):
            self.closed = False

        def close(self):
            self.closed = True

        def send(self, control, value):
            if self.closed:
                raise BridgeError("gone")
            self.sent.append((control, value))
            if control == CC_HEARTBEAT and self.herdr:
                self.inbox.append((MIDI_CHANNEL, CC_ECHO, PROTOCOL))
            if control == CC_SORT:
                self.sort = value  # Host updates never echo.

        def receive(self):
            messages, self.inbox = self.inbox, []
            return messages

        def marks(self):
            return [value for control, value in self.sent if control == CC_ACTIVE]

    now = [100.0]
    boards = {
        "Moonlander Mark I": FakeBoard("Moonlander Mark I"),
        "Planck EZ Glow": FakeBoard("Planck EZ Glow", sort=SORT_GROUPED),
        "Synth": FakeBoard("Synth", herdr=False),
    }
    plugged = list(boards)
    opened = []

    def fake_scan():
        def factory(name):
            opened.append(name)
            return boards[name]

        return [Port(("alsa", name), name, lambda name=name: factory(name)) for name in plugged]

    import tempfile

    with tempfile.TemporaryDirectory() as state_dir:
        with open(os.path.join(state_dir, "recent-boards"), "w") as handle:
            handle.write("Planck EZ Glow\n")
        fleet = KeyboardFleet(fake_scan, state_dir, clock=lambda: now[0])
        fleet.open()
        assert all(board.sent[0] == (CC_HEARTBEAT, PROTOCOL) for board in boards.values())
        assert fleet.active is None
        # Echoes confirm Herdr boards; the remembered Planck wins over the first found.
        assert fleet.receive() == []
        assert fleet.active == ("alsa", "Planck EZ Glow") and fleet.provisional
        assert fleet.take_refresh() and not fleet.take_refresh()
        assert boards["Planck EZ Glow"].marks()[-1] == 1
        assert boards["Moonlander Mark I"].marks()[-1] == 0
        assert boards["Synth"].marks() == []

        # Frames reach confirmed boards only; chimes reach the active board only.
        fleet.send(CC_STATE, STATUS_CODES["done"] | CHIME_BITS)
        assert boards["Planck EZ Glow"].sent[-1] == (CC_STATE, STATUS_CODES["done"] | CHIME_BITS)
        assert boards["Moonlander Mark I"].sent[-1] == (CC_STATE, STATUS_CODES["done"])
        assert (CC_STATE, STATUS_CODES["done"]) not in boards["Synth"].sent

        # Silent gear is dropped after the echo window and not probed again.
        for _ in range(int(ECHO_STALE_SECONDS) + 1):
            now[0] += HEARTBEAT_SECONDS
            fleet.heartbeat()
            fleet.receive()
            fleet.maintain()
        assert fleet.active == ("alsa", "Planck EZ Glow")
        assert ("alsa", "Synth") not in fleet.boards and boards["Synth"].closed
        now[0] += HEARTBEAT_SECONDS
        fleet.maintain()
        assert opened.count("Synth") == 1

        # Every board mirrors Herdr's sort mode; a standby board's note claims it and is swallowed.
        fleet.send(CC_SORT, SORT_GROUPED)
        assert boards["Moonlander Mark I"].sort == boards["Planck EZ Glow"].sort == SORT_GROUPED
        fleet.heartbeat()
        boards["Moonlander Mark I"].inbox.append((NOTE_ON, NOTE_ACCEPT, 127))
        claimed = fleet.receive()
        assert claimed == [], "claiming a board must not change panel sorting"
        assert fleet.active == ("alsa", "Moonlander Mark I") and not fleet.provisional
        with open(os.path.join(state_dir, "recent-boards")) as handle:
            assert handle.read().splitlines() == ["Moonlander Mark I", "Planck EZ Glow"]
        assert boards["Moonlander Mark I"].marks()[-1] == 1
        assert boards["Planck EZ Glow"].marks()[-1] == 0
        with open(os.path.join(state_dir, "boards")) as handle:
            assert handle.read() == "active   Moonlander Mark I\nstandby  Planck EZ Glow\n"

        # The active board's notes pass through; the standby board's are dropped.
        boards["Moonlander Mark I"].inbox.append((NOTE_ON, NOTE_TAB_NEXT, 127))
        boards["Planck EZ Glow"].inbox.append((NOTE_OFF, NOTE_ACCEPT, 0))
        assert fleet.receive() == [(NOTE_ON, NOTE_TAB_NEXT, 127)]

        # A standby board's sort request claims it without reaching Herdr; the active board's passes.
        boards["Planck EZ Glow"].inbox.append((MIDI_CHANNEL, CC_SORT, SORT_PRIORITY))
        assert fleet.receive() == [], "claiming a board must not change panel sorting"
        assert fleet.active == ("alsa", "Planck EZ Glow")
        boards["Planck EZ Glow"].inbox.append((MIDI_CHANNEL, CC_SORT, SORT_PRIORITY))
        assert fleet.receive() == [(MIDI_CHANNEL, CC_SORT, SORT_PRIORITY)]
        boards["Moonlander Mark I"].inbox.append((NOTE_ON, NOTE_TAB_NEXT, 127))
        assert fleet.receive() == [] and fleet.active == ("alsa", "Moonlander Mark I")

        # Unplugging the active board promotes the other; it stays active when the first returns.
        plugged.remove("Moonlander Mark I")
        now[0] += HEARTBEAT_SECONDS
        fleet.maintain()
        assert fleet.active == ("alsa", "Planck EZ Glow") and not fleet.provisional
        plugged.append("Moonlander Mark I")
        now[0] += HEARTBEAT_SECONDS
        fleet.maintain()
        fleet.receive()
        assert fleet.active == ("alsa", "Planck EZ Glow")
        assert boards["Moonlander Mark I"].marks()[-1] == 0

        # An active board that stops echoing hands over to one that still does.
        boards["Planck EZ Glow"].herdr = False
        now[0] += ECHO_STALE_SECONDS + 1
        fleet.heartbeat()
        fleet.receive()
        fleet.maintain()
        assert fleet.active == ("alsa", "Moonlander Mark I")

        # A board that fails mid-send is dropped instead of breaking the session.
        boards["Moonlander Mark I"].closed = True
        fleet.send(CC_RISK, RISK_NONE)
        assert ("alsa", "Moonlander Mark I") not in fleet.boards
        assert fleet.active is None
        fleet.close()
    print("self-test ok")


def main():
    args = sys.argv[1:]
    if args and args[0] == "--self-test":
        self_test()
        return
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if args and args[0] in LIFECYCLE_COMMANDS:
        config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        if not state_dir or not config_dir:
            print("qmk-herdr: run lifecycle commands through the plugin's actions", file=sys.stderr)
            sys.exit(2)
        sys.exit(lifecycle(args[0], state_dir, config_dir))
    # `run [ports]` is the daemon; bare `[ports]` still runs it in the foreground.
    if args and args[0] == "run":
        args = args[1:]
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        log("HERDR_SOCKET_PATH is missing; run qmk-herdr inside a Herdr pane")
        sys.exit(1)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)
        if not hold_lock(state_dir):
            log(f"another bridge is running (pid {running_pid(state_dir)}); exiting")
            sys.exit(1)
    run(socket_path, args[0] if args else "", state_dir)


if __name__ == "__main__":
    main()
