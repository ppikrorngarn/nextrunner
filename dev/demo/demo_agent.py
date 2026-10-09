#!/usr/bin/env python3
"""A stand-in agent for the README recordings: canned Markdown answers, no tokens.

    demo_agent.py --name NAME PROMPT

A file named limit-NAME in $HOME (the demo home) makes NAME answer with a usage-limit
message, so the dispatcher rests it and hands the task to the next agent.
A title containing "flaky" fails, so the task ends up blocked.
"""
import argparse
import os
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--name", default="agent")
ap.add_argument("prompt")
a = ap.parse_args()

title = next((line[6:] for line in a.prompt.splitlines() if line.startswith("Task: ")), "")
time.sleep(float(os.environ.get("DEMO_DELAY", "2")))

if (Path.home() / f"limit-{a.name}").exists():
    sys.exit("Error: You've hit your usage limit. Try again in 4 hours.")
if "flaky" in title.lower():
    sys.exit(f"{a.name}: could not reach the test server (connection refused)")

ANSWERS = {
    "open issues": """Twelve open issues, in three areas.

| Area | Open | Oldest | Most urgent |
|---|---|---|---|
| Sync | 5 | 19 days | Offline edits lost after a crash |
| Search | 4 | 11 days | Results ignore accents |
| Settings | 3 | 6 days | Dark mode resets on restart |

Start with **Sync**: two of its issues have the same cause (the save queue is not flushed on exit).""",
    "login form": """Looks good overall. Three things before merging:

1. **The error message leaks which part was wrong.** "Unknown email" tells an attacker the address is not registered; say "Email or password is wrong".
2. The submit button stays enabled while the request runs, so a double click sends two requests.
3. No test covers the expired-session path.

Nothing blocking in the styling or the copy.""",
    "release notes": """## v0.2

- **Faster sync**: changes upload in batches, about 4x fewer requests.
- **Search** now matches accented and unaccented letters alike.
- **Fixed**: dark mode no longer resets after a restart.
- **Fixed**: offline edits survive a crash.""",
    "nightly build": """The nightly build takes 41 minutes; 29 of them are two steps.

| Step | Minutes | Note |
|---|---|---|
| Install dependencies | 17 | cache key changes every night |
| Unit tests | 6 | |
| Integration tests | 12 | runs on one worker |
| Package | 6 | |

Fix the cache key (it includes the date) and split the integration tests over 3 workers: about **15 minutes** saved.""",
    "weekly update": """Draft for the team channel:

> This week: faster sync is merged, search handles accents, and the dark mode bug is fixed. Next week: the save queue on exit and the integration test split. No blockers.""",
    "cache key": {
        "agent-a": """**Yes, it is safe.** The key is built from the user id and the request path, so two users never share an entry. The only shared entries are public pages, which carry no user data.""",
        "agent-b": """**Mostly.** The key includes the user id, but `/export` is cached by path only (line 88 of the cache middleware). Two users exporting at the same second could get each other's file. Add the user id there, or skip the cache for `/export`.""",
    },
}

reply = f"Done: {title}"
for key, answer in ANSWERS.items():
    if key in title.lower():
        reply = answer.get(a.name, next(iter(answer.values()))) if isinstance(answer, dict) else answer
        break
print(reply)
