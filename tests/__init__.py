"""Every test runs against a throwaway NEXTRUNNER_HOME, so none can touch a real board or agents.json."""
import atexit
import os
import tempfile

for var in ("NEXTRUNNER_DB", "NEXTRUNNER_AGENTS", "NEXTRUNNER_LOG"):
    os.environ.pop(var, None)
_home = tempfile.TemporaryDirectory(prefix="nextrunner-test-home-")
atexit.register(_home.cleanup)
os.environ["NEXTRUNNER_HOME"] = _home.name
