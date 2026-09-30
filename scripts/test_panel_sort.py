#!/usr/bin/env python3
"""Run with python3 scripts/test_panel_sort.py; no live Herdr or MIDI required."""
import os
import tempfile
import tomllib
from pathlib import Path

from bridge import SORT_GROUPED, SORT_PRIORITY, BridgeError, PanelSort, Tracker


def check():
    agents = [
        {"pane_id": "idle", "agent_status": "idle", "state_change_seq": 99},
        {"pane_id": "work", "agent_status": "working", "state_change_seq": 10},
        {"pane_id": "done", "agent_status": "done", "state_change_seq": 1},
        {"pane_id": "block", "agent_status": "blocked", "state_change_seq": 2},
        {"pane_id": "tie", "agent_status": "working", "state_change_seq": 10},
    ]
    tracker = Tracker()
    assert tracker.order(agents, {"idle": 100}) == ["block", "done", "work", "tie", "idle"]
    tracker.sort = SORT_GROUPED
    assert tracker.order(agents) == [agent["pane_id"] for agent in agents]

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        original = '[ui]\nagent_panel_sort = "priority" # retained\nsound = true\n\n[keys]\nnext = "n"\n'
        path.write_text(original)
        reloads = []
        panel = PanelSort(tracker, str(path), reload=lambda: reloads.append(True))
        assert panel.read() and tracker.sort == SORT_PRIORITY
        tracker.sort = SORT_GROUPED
        panel.write()
        assert reloads == [True]
        assert tomllib.loads(path.read_text())["ui"]["agent_panel_sort"] == "spaces"
        assert '# retained' in path.read_text() and 'sound = true' in path.read_text()
        assert not panel.read()
        path.write_text(original)
        assert panel.read() and tracker.sort == SORT_PRIORITY

        # A failed reload restores the file and host mode, not a false LED ack.
        def fail():
            raise BridgeError("reload failed")
        panel.reload = fail
        tracker.sort = SORT_GROUPED
        try:
            panel.write()
            assert False, "reload failure was swallowed"
        except BridgeError:
            pass
        assert path.read_text() == original
        panel.read()
        assert tracker.sort == SORT_PRIORITY

        # Refuse managed symlinks rather than silently replacing them.
        link = Path(directory) / "link.toml"
        link.symlink_to(path)
        panel = PanelSort(tracker, str(link), reload=lambda: None)
        panel.read()
        tracker.sort = SORT_GROUPED
        try:
            panel.write()
            assert False, "symlink was replaced"
        except BridgeError:
            pass
        assert link.is_symlink() and path.read_text() == original
        assert panel.read() and tracker.sort == SORT_PRIORITY

        # Missing setting defaults to Grouped; insertion cannot touch [keys].
        path.write_text('[keys]\nnext = "n"\n')
        panel = PanelSort(tracker, str(path), reload=lambda: None)
        panel.read()
        assert tracker.sort == SORT_GROUPED
        tracker.sort = SORT_PRIORITY
        panel.write()
        assert tomllib.loads(path.read_text()) == {"keys": {"next": "n"}, "ui": {"agent_panel_sort": "priority"}}
        os.chmod(path, 0o640)
        tracker.sort = SORT_GROUPED
        panel.write()
        assert path.stat().st_mode & 0o777 == 0o640

        # A keyboard request drops any agent view hiding the panel, even when
        # the config already matches, but not when the write fails.
        cleared = []
        panel = PanelSort(tracker, str(path), reload=lambda: None, clear_view=lambda: cleared.append(True))
        panel.read()
        tracker.sort = SORT_PRIORITY
        panel.request()
        assert cleared == [True] and tomllib.loads(path.read_text())["ui"]["agent_panel_sort"] == "priority"
        panel.request()
        assert cleared == [True, True]
        panel.reload = fail
        tracker.sort = SORT_GROUPED
        try:
            panel.request()
            assert False, "reload failure was swallowed"
        except BridgeError:
            pass
        assert cleared == [True, True]
    print("panel-sort checks ok")


if __name__ == "__main__":
    check()
