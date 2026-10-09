#!/usr/bin/env python3
"""Drive bash in a pseudo-terminal from a scene and save what it printed as an asciicast v2 file.

    record.py SCENE.json OUT.cast

A scene is {"cols": 150, "rows": 40, "env": {...}, "steps": [...]}. Steps:
    ["hide"] / ["show"]        output while hidden is kept (so the screen state is right)
                               but squeezed to one instant, so it never shows in the GIF
    ["type", "text"]           type like a person, one key at a time
    ["keys", "\\r", 0.6]       send raw keys, then pause (seconds, optional)
    ["sleep", 1.5]
    ["wait", "regex", 20]      wait until the output (ANSI stripped) matches, since the last ["mark"]
    ["mark"]
    ["grab", "name", "regex"]  remember the first group of a match in the output since the last mark
    ["type_var", "text with {name}"]
    ["snap", "name"]           note the time, so a frame can be pulled from the GIF later (OUT.snaps.json)
    ["fixpaths"]               rewrite the demo HOME to ~ in the board's event texts (log= paths)
"""
import codecs
import fcntl
import json
import os
import pty
import random
import re
import select
import struct
import sys
import termios
import threading
import time

ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[PX^_][^\x1b]*\x1b\\|[@-Z\\-_])")


def main(scene_path, out_path):
    scene = json.load(open(scene_path))
    cols, rows = scene.get("cols", 150), scene.get("rows", 40)
    env = {"PATH": os.environ["PATH"], "TERM": "xterm-256color", "COLORTERM": "truecolor",
           "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"} | scene.get("env", {})
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(sys.stdout.fileno(), termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.execvpe("bash", ["bash", "--noprofile", "--norc", "-i"], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    events, text = [], []          # (t, data) and the ANSI-stripped text seen so far
    lock = threading.Lock()
    state = {"hidden": False, "clock": 0.0, "last": time.monotonic(), "mark": 0, "done": False}
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def now():
        t = time.monotonic()
        if not state["hidden"]:
            state["clock"] += t - state["last"]
        state["last"] = t
        return state["clock"]

    def reader():
        while not state["done"]:
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            s = decoder.decode(data)
            with lock:
                events.append((now(), s))
                text.append(ANSI.sub("", s))

    threading.Thread(target=reader, daemon=True).start()
    if scene.get("livefix"):
        def fixer():
            while not state["done"]:
                try:
                    fix_paths(home)
                except Exception:
                    pass
                time.sleep(0.1)
        threading.Thread(target=fixer, daemon=True).start()
    seen = lambda: "".join(text)[state["mark"]:]
    vars_, snaps = {}, {}
    home = env.get("HOME", "")

    def wait(pattern, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with lock:
                m = re.search(pattern, seen(), re.S)
            if m:
                return m
            time.sleep(0.05)
        sys.exit(f"timed out waiting for {pattern!r}; last output:\n{seen()[-1500:]}")

    for step in scene["steps"]:
        kind, args = step[0], step[1:]
        if kind == "hide":
            with lock:
                now(); state["hidden"] = True
        elif kind == "show":
            with lock:
                now(); state["hidden"] = False
        elif kind in ("type", "type_var"):
            s = args[0].format(**vars_) if kind == "type_var" else args[0]
            for ch in s:
                os.write(fd, ch.encode())
                time.sleep(random.uniform(*scene.get("typing", [0.03, 0.075])) if not state["hidden"] else 0.002)
        elif kind == "keys":
            os.write(fd, args[0].encode())
            time.sleep(args[1] if len(args) > 1 else 0.4)
        elif kind == "sleep":
            time.sleep(args[0])
        elif kind == "mark":
            with lock:
                state["mark"] = len("".join(text))
        elif kind == "wait":
            wait(args[0], args[1] if len(args) > 1 else 20)
        elif kind == "snap":
            with lock:
                snaps[args[0]] = round(now(), 2)
        elif kind == "fixpaths":
            fix_paths(home)
        elif kind == "grab":
            vars_[args[0]] = wait(args[1]).group(1)
        else:
            sys.exit(f"unknown step {step}")

    time.sleep(0.3)
    state["done"] = True
    os.kill(pid, 9)
    with open(out_path, "w") as f:
        f.write(json.dumps({"version": 2, "width": cols, "height": rows,
                            "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"}}) + "\n")
        for t, s in events:
            f.write(json.dumps([round(t, 4), "o", s]) + "\n")
    if snaps:
        json.dump(snaps, open(out_path.replace(".cast", ".snaps.json"), "w"), indent=1)


def fix_paths(home):
    import sqlite3
    db = sqlite3.connect(os.path.join(home, "Library/Application Support/nextrunner/board.db"), timeout=5)
    for prefix in {home, os.path.realpath(home)}:
        db.execute("UPDATE events SET text = replace(text, ?, '~') WHERE text LIKE ?", (prefix, f"%{prefix}%"))
    db.commit()
    db.close()


if __name__ == "__main__":
    main(*sys.argv[1:3])
