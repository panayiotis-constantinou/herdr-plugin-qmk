#!/usr/bin/env python3
"""Run with python3 scripts/test_lifecycle.py; no live Herdr or MIDI required."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time

import bridge

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
# A stand-in daemon: takes the lock like `bridge.py run`, then idles.
HOLDER = (
    "import sys, time; sys.path.insert(0, sys.argv[1]); import bridge; "
    "sys.exit(1) if not bridge.hold_lock(sys.argv[2]) else None; "
    "print('held', flush=True); time.sleep(60)"
)


def holder(state_dir):
    return [sys.executable, "-c", HOLDER, SCRIPTS, state_dir]


def wait_for(predicate, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def quietly(function, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return function(*args, **kwargs)


def check():
    listing = """\
  101  1000 /nix/store/py/bin/python3 /nix/store/abc-herdr-plugin-panayiotis.qmk-herdr-fe617555c550/scripts/bridge.py rtmidi:qmk-herdr-ipad
  102  1000 python3 /home/me/Projects/qmk-herdr/scripts/bridge.py
  103  1000 python3 /home/me/Projects/qmk-herdr/scripts/bridge.py run rtmidi:x
  104  1000 python3 /home/me/Projects/qmk-herdr/scripts/bridge.py status
  105  1001 python3 /home/other/qmk-herdr/scripts/bridge.py
  106  1000 python3 /home/me/Projects/other/scripts/bridge.py
  107  1000 python3 /home/me/Projects/qmk-herdr/scripts/bridge.py --self-test
  108  1000 /home/me/Projects/qmk-herdr/scripts/bridge.py
  109  1000 vim /home/me/Projects/qmk-herdr/scripts/notes.txt /home/me/Projects/qmk-herdr/scripts/bridge.py
"""
    assert bridge.stray_bridges(listing, 1000) == [101, 102, 108], bridge.stray_bridges(listing, 1000)
    assert bridge.stray_bridges(listing, 1000, {101, 108}) == [102]

    # The real stray scan would see this machine's bridges; never stop those.
    bridge.find_strays = lambda holder: []

    with tempfile.TemporaryDirectory() as state_dir, tempfile.TemporaryDirectory() as config_dir:
        assert bridge.running_pid(state_dir) is None, "no lock file: stopped"

        first = subprocess.Popen(holder(state_dir), stdout=subprocess.PIPE, text=True)
        try:
            assert first.stdout.readline().strip() == "held"
            assert bridge.running_pid(state_dir) == first.pid
            # Asking twice does not take or drop the lock.
            assert bridge.running_pid(state_dir) == first.pid
            second = subprocess.run(holder(state_dir), capture_output=True, text=True, timeout=10)
            assert second.returncode == 1, "a second bridge must not take the lock"
        finally:
            first.kill()
            first.wait()
        assert wait_for(lambda: bridge.running_pid(state_dir) is None), "the lock dies with its owner"

        # start launches the daemon once; a second start finds it.
        assert quietly(bridge.start_bridge, state_dir, config_dir, command=holder(state_dir)) == 0
        daemon = bridge.running_pid(state_dir)
        assert daemon is not None
        assert quietly(bridge.start_bridge, state_dir, config_dir, command=holder(state_dir)) == 0
        assert bridge.running_pid(state_dir) == daemon
        assert quietly(bridge.bridge_status, state_dir) == 0

        # stop ends it, whichever root started it, and status says so.
        assert quietly(bridge.stop_bridges, state_dir)
        assert wait_for(lambda: bridge.running_pid(state_dir) is None)
        assert quietly(bridge.bridge_status, state_dir) == 1

        # A daemon that exits before taking the lock is a failed start.
        failing = [sys.executable, "-c", "print('boom'); raise SystemExit(1)"]
        assert quietly(bridge.start_bridge, state_dir, config_dir, command=failing) == 1
        with open(os.path.join(state_dir, bridge.LOG_FILE)) as handle:
            assert handle.read() == "boom\n"
    print("lifecycle test ok")


if __name__ == "__main__":
    check()
