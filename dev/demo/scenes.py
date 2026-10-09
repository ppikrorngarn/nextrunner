#!/usr/bin/env python3
"""Write the scene files that record.py plays. Usage: scenes.py OUT_DIR"""
import json
import os
import sys
from pathlib import Path

ENV = {
    "HOME": os.environ.get("DEMO_HOME", "/private/tmp/nr-demo"),
    "PATH": os.environ["PATH"],  # nextrunner must be on it
    "PS1": "\\[\\e[38;2;122;162;247m\\]$\\[\\e[0m\\] ",
    "NEXTRUNNER_AGENT": "you",
    "DEMO_DELAY": "2",
}
START = [["hide"], ["type", "cd ~/projects/app; clear\r"], ["sleep", 0.6], ["show"], ["sleep", 0.8]]
ESC, ENTER = "\x1b", "\r"
ID = r"(t_[0-9a-z]{10})"


def cmd(line, pause=0.3):
    return [["mark"], ["type", line], ["sleep", 0.35], ["keys", ENTER, pause]]


SCENES = {
    "cli": {"cols": 128, "rows": 38, "livefix": True, "typing": [0.018, 0.042], "steps": START + [
        *cmd('nextrunner add "Summarise the open issues" --body "Group them by area and say where to start."'),
        ["grab", "issues", ID], ["sleep", 1.0],
        *cmd('nextrunner add "Find why the nightly build is slow" --to agent-c'), ["wait", ID], ["sleep", 1.0],
        *cmd("nextrunner dispatch --jobs 2"), ["wait", r"done by.*done by", 40], ["sleep", 2.0],
        ["mark"], ["type_var", "nextrunner show {issues}"], ["sleep", 0.35], ["keys", ENTER, 0.3],
        ["wait", r"agent-b\s+done"], ["sleep", 5.0],
    ]},
    "fanout": {"cols": 112, "rows": 30, "typing": [0.018, 0.042], "steps": START + [
        *cmd('nextrunner add "Is the cache key safe across users?" --body "Look at the cache middleware." --to agent-a --to agent-b'),
        ["grab", "first", ID], ["wait", ID + r".*" + ID], ["sleep", 1.0],
        *cmd("nextrunner dispatch --jobs 2"), ["wait", r"done by.*done by", 40], ["sleep", 1.5],
        ["mark"], ["type_var", "nextrunner compare {first}"], ["sleep", 0.35], ["keys", ENTER, 0.3],
        ["wait", r"agent-b\s+done.*export"], ["sleep", 5.0],
    ]},
    "ui": {"cols": 128, "rows": 38, "livefix": True, "env": {"DEMO_DELAY": "3"}, "steps": START + [
        *cmd("nextrunner ui", 0.1), ["wait", "Quit", 20], ["sleep", 3.2], ["snap", "start"],
        ["keys", "j", 1.8], ["snap", "held"],
        ["keys", "y", 2.0], ["snap", "approved"],
        ["keys", "d", 1.3], ["snap", "dispatchform"], ["keys", ENTER, 1.6], ["snap", "running"],
        ["sleep", 4.6], ["snap", "finished"],
        ["keys", "/", 0.6], ["type", "issues"], ["sleep", 0.5], ["keys", ENTER, 2.6], ["snap", "table"],
        ["keys", ENTER, 3.4], ["snap", "fullscreen"], ["keys", ESC, 1.0], ["keys", ESC, 1.0],
        ["keys", "/", 0.6], ["type", "cache"], ["sleep", 0.5], ["keys", ENTER, 2.6], ["snap", "fanout-a"],
        ["keys", "j", 2.8], ["snap", "fanout-b"], ["keys", ESC, 1.2],
        ["keys", "a", 1.2], ["snap", "addform"], ["type", "Write a migration guide"], ["sleep", 0.8],
        ["keys", "\x13", 3.0], ["snap", "added"],
    ]},
}

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
for name, scene in SCENES.items():
    scene["env"] = ENV | scene.get("env", {})
    (out / f"{name}.json").write_text(json.dumps(scene, indent=1))
    print(out / f"{name}.json")
