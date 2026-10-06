#!/usr/bin/env python3
"""Build the single-file app with PyInstaller and prove it works.

    uv run --with pyinstaller python packaging/build.py [--name nextrunner-macos-arm64]

PyInstaller cannot cross-compile: the app is built for the OS and CPU this runs on.
CI runs it once per target (see .github/workflows/release.yml). The result is
dist/<name> and dist/<name>.sha256. The smoke test then runs the built file the way a
user would: version, add, dispatch (with the fake dev agent).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ".exe" if sys.platform == "win32" else ""


def build(name):
    subprocess.run([sys.executable, "-m", "PyInstaller", "--onefile", "--name", name,
                    "--collect-all", "textual", "--add-data", f"{ROOT / 'src' / 'nextrunner' / 'agents.example.json'}{os.pathsep}nextrunner", "--collect-all", "rich", "--paths", str(ROOT / "src"),
                    "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
                    "--specpath", str(ROOT / "build"), "--noconfirm", "--log-level", "WARN",
                    str(ROOT / "packaging" / "entry.py")], check=True)
    exe = ROOT / "dist" / (name + EXE)
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (ROOT / "dist" / f"{name}{EXE}.sha256").write_text(f"{digest}  {exe.name}\n")
    return exe


def run(exe, *args, env, timeout=60):
    result = subprocess.run([str(exe), *args], env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        sys.exit(f"FAILED: nextrunner {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def smoke(exe):
    fake = ROOT / "dev" / "fake_agent.py"
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "NEXTRUNNER_HOME": home}
        fake_cmd = [sys.executable, str(fake), "--name", "agent-a", "{prompt}"]
        (Path(home) / "agents.json").write_text(json.dumps(
            {"agent-a": {"cmd": fake_cmd, "reply": "stdout"}}))
        print("version:", run(exe, "--version", env=env).strip())
        task = run(exe, "add", "smoke test", "--to", "agent-a", env=env).strip()
        run(exe, "dispatch", env=env)
        shown = run(exe, "show", task, env=env)
        assert "agent-a handled: smoke test" in shown, shown
        print("dispatch: ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="nextrunner")
    ap.add_argument("--no-build", action="store_true", help="only run the smoke test on dist/<name>")
    args = ap.parse_args()
    exe = ROOT / "dist" / (args.name + EXE) if args.no_build else build(args.name)
    smoke(exe)
    print(f"built {exe} ({exe.stat().st_size / 1e6:.1f} MB)")
