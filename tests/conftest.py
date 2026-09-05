import os
import sys
import tempfile
from pathlib import Path

# import the project, not an installed copy
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Never touch the user's real key file or database from a test run.
os.environ.setdefault("AGENT_KEY", "test-agent-key")
os.environ.setdefault("SURSUMAI_HOME", tempfile.mkdtemp(prefix="sursumai-home-"))
os.environ.setdefault("SURSUMAI_DB_DIR", tempfile.mkdtemp(prefix="sursumai-db-"))
