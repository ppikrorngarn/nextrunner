"""First-run help: `nextrunner init` writes a starter agents.json, `nextrunner doctor` checks the one you have."""
import json
import os
import re
import shlex
import shutil
import string
from importlib import resources
from pathlib import Path

from . import paths
from .agents import HOOK_FIELDS, HOOKS, LISTS, expand, hooks_of

KNOWN_FIELDS = {"prompt", "cwd", "out", "board", "session"}
COMMAND_KEYS = ("cmd", "cmd_edit", "cmd_resume", "cmd_edit_resume")


def example_text():
    return resources.files("nextrunner").joinpath("agents.example.json").read_text()


def split_command(command):
    """Split a command line into words, honouring quotes. On Windows a backslash is part of a path, not an escape."""
    lex = shlex.shlex(command, posix=True)
    lex.whitespace_split, lex.commenters = True, ""
    if os.name == "nt":
        lex.escape = ""
    return list(lex)


def parse_agent(text):
    """'NAME=command words' -> (name, spec). {prompt} is added as the last word if the command has none."""
    name, sep, command = text.partition("=")
    name, words = name.strip(), split_command(command)
    if not sep or not name or not words:
        raise SystemExit(f"--agent wants NAME='command and arguments', not {text!r}")
    if not any("{prompt}" in word for word in words):
        words.append("{prompt}")
    return name, {"cmd": words, "reply": "stdout"}


def init(agent_args=(), force=False):
    """Write agents.json where nextrunner looks for it. Returns lines to print.

    With no --agent it writes the annotated-by-example template (placeholder commands to replace).
    With --agent NAME='command ...' (repeatable) it writes just those agents, read-only, each
    answering on stdout. Never overwrites unless force.
    """
    path = paths.agents_file()
    if path.exists() and not force:
        raise SystemExit(f"{path} already exists; edit it, or run again with --force to replace it")
    if agent_args:
        text = json.dumps(dict(parse_agent(a) for a in agent_args), indent=2) + "\n"
    else:
        text = example_text()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    kind = "your agents" if agent_args else "a template with placeholder agents"
    return [f"wrote {path} ({kind})",
            "next: edit it so each command starts your real agent, then run: nextrunner doctor",
            "      the README's agents.json section explains every field"]


def check_agents(agents):
    """[(level, text)] for each problem or confirmation. level is 'ok', 'warn' or 'error'."""
    results = []

    def add(level, text):
        results.append((level, text))

    if not isinstance(agents, dict) or not [n for n in agents if n != LISTS]:
        return [("error", "agents.json must be an object with at least one agent")]
    try:
        hooks = hooks_of(agents)
        agents = expand(agents)
    except ValueError as err:
        return [("error", str(err))]
    hook_problems = 0
    for kind, command in hooks.items():
        if not shutil.which(command[0]) and not (Path(command[0]).is_file() and Path(command[0]).stat().st_mode & 0o111):
            add("error", f"{HOOKS}.{kind}: cannot find the program {command[0]!r} on PATH")
            hook_problems += 1
        for word in command:
            try:
                fields = {f for _, f, _, _ in string.Formatter().parse(word) if f is not None}
            except ValueError as err:
                add("error", f"{HOOKS}.{kind}: {word!r}: {err} (write a literal brace as {{{{ or }}}})")
                hook_problems += 1
                continue
            for field in sorted(fields - HOOK_FIELDS):
                add("error", f"{HOOKS}.{kind}: unknown placeholder {{{field}}}; hooks know "
                             + ", ".join("{" + f + "}" for f in sorted(HOOK_FIELDS)))
                hook_problems += 1
    if hooks and not hook_problems:
        add("ok", f"hooks: {', '.join(hooks)}")
    for name, spec in agents.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("cmd"), list) or not spec["cmd"]:
            add("error", f"{name}: needs a \"cmd\" list")
            continue
        problems = 0
        for key in COMMAND_KEYS:
            command = spec.get(key)
            if command is None:
                continue
            if not isinstance(command, list) or not all(isinstance(w, str) for w in command) or not command:
                add("error", f"{name}.{key}: must be a list of strings")
                problems += 1
                continue
            program = command[0]
            if not (shutil.which(program) or (Path(program).is_file() and Path(program).stat().st_mode & 0o111)):
                add("error", f"{name}.{key}: cannot find the program {program!r} on PATH")
                problems += 1
            for word in command:
                try:
                    fields = {f for _, f, _, _ in string.Formatter().parse(word) if f is not None}
                except ValueError as err:
                    add("error", f"{name}.{key}: {word!r}: {err} (write a literal brace as {{{{ or }}}})")
                    problems += 1
                    continue
                for field in sorted(fields - KNOWN_FIELDS):
                    add("error", f"{name}.{key}: unknown placeholder {{{field}}}")
                    problems += 1
        reply = spec.get("reply", "stdout")
        if not (reply in ("stdout", "file") or (isinstance(reply, str) and reply.startswith("json:") and len(reply) > 5)):
            add("error", f"{name}: reply must be stdout, file or json:<key>, not {reply!r}")
            problems += 1
        if reply == "file" and not any("{out}" in w for w in spec["cmd"]):
            add("error", f"{name}: reply is \"file\" but cmd never uses {{out}}")
            problems += 1
        if spec.get("session"):
            try:
                if re.compile(spec["session"]).groups < 1:
                    add("error", f"{name}: the session pattern needs one (group) that captures the ID")
                    problems += 1
            except re.error as err:
                add("error", f"{name}: bad session pattern: {err}")
                problems += 1
            if not spec.get("cmd_resume"):
                add("warn", f"{name}: has a session pattern but no cmd_resume, so sessions are saved and never resumed")
        usage = spec.get("usage")
        if usage is not None:
            if not isinstance(usage, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in usage.items()):
                add("error", f"{name}: usage must be an object of label: pattern, not {usage!r}")
                problems += 1
            else:
                for label, pattern in usage.items():
                    try:
                        if re.compile(pattern).groups < 1:
                            add("error", f"{name}: usage.{label} needs one (group) that captures the figure")
                            problems += 1
                    except re.error as err:
                        add("error", f"{name}: usage.{label}: bad pattern: {err}")
                        problems += 1
        timeout = spec.get("timeout")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
            add("error", f"{name}: timeout must be a number of seconds above 0, not {timeout!r}")
            problems += 1
        if not problems:
            add("ok", f"{name}: {'read and edit' if spec.get('cmd_edit') else 'read only'}, "
                      f"parallel {spec.get('parallel', 1)}"
                      f"{f', timeout {timeout:g}s' if timeout is not None else ''}")
    if not shutil.which("git"):
        add("warn", "git is not on PATH, so --commit tasks will be skipped")
    return results


def doctor():
    """Check agents.json. Returns (lines to print, number of errors)."""
    path = paths.agents_file()
    if not path.exists():
        return [f"error: {path} does not exist; run: nextrunner init"], 1
    try:
        agents = json.loads(path.read_text())
    except ValueError as err:
        return [f"error: {path} is not valid JSON: {err}"], 1
    results = check_agents(agents)
    lines = [f"checking {path}"] + [f"{level:>5}  {text}" for level, text in results]
    errors = sum(level == "error" for level, _ in results)
    lines.append("all good" if not errors else f"{errors} problem{'s' * (errors != 1)} to fix")
    return lines, errors
