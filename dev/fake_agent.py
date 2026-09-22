#!/usr/bin/env python3
"""A stand-in agent for trying nextrunner without spending tokens or touching real agents.

    fake_agent.py [--edit] [--name NAME] PROMPT

It answers after a short pause. Put a word in the task's title or brief to make it misbehave:
    [fail]    exit with an error, so the task is rerouted
    [limit]   print a usage-limit message, so this agent rests
    [slow]    take 20 seconds (see a task stay running)
With --edit it also writes fake-agent-was-here.txt in the task's folder and ends with a COMMIT: line.
"""
import argparse
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--edit", action="store_true")
ap.add_argument("--name", default="fake")
ap.add_argument("prompt")
a = ap.parse_args()

time.sleep(20 if "[slow]" in a.prompt else 1.5)
if "[limit]" in a.prompt:
    sys.exit("You've hit your usage limit. Try again later.")
if "[fail]" in a.prompt:
    sys.exit(f"{a.name}: simulated failure")
title = next((line[6:] for line in a.prompt.splitlines() if line.startswith("Task: ")), "a task")
reply = f"{a.name} handled: {title}"
if a.edit:
    Path("fake-agent-was-here.txt").write_text(reply + "\n")
    reply += "\nCOMMIT: fake change from the dev agent"
print(reply)
