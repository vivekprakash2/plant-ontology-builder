"""Shared paths for the ontology builder."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "Data"

AM_DIR = DATA_DIR / "am"
APM_DIR = DATA_DIR / "apm"
DCS_DIR = DATA_DIR / "dcs"
HIST_DIR = DATA_DIR / "hist"
CMMS_DIR = DATA_DIR / "cmms"
ERP_DIR = DATA_DIR / "erp"

OUTPUT_DIR = REPO_ROOT / "output"

# Load a local, gitignored .env file (KEY=value lines) into os.environ, if
# present, so every entry point (run_pipeline.py, run_graph.py, the test
# suite, ad-hoc scripts) picks up credentials (DATABRICKS_TOKEN,
# EMBEDDING_MODEL, etc.) automatically -- not just server.py, which
# previously had its own separate copy of this same loading logic. This
# module is imported by ingest.py/resolution.py/graph.py/etc., so this
# runs once, early, before any provider factory reads os.environ.
#
# SECURITY_NOTE: values are read straight into os.environ and never
# logged. `os.environ.setdefault` never overwrites a real environment
# variable that's already set.
_env_file = REPO_ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _value = _line.partition("=")
        os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))

# Prose source for autonomous process-topology extraction
# (`topology_extraction.extract_topology_with_llm`, wired into
# `graph.build_graph`) -- the plant's real physical flow line (what feeds/
# cools/supplies what) isn't structurally present in any of the six
# systems' data, only in unstructured engineering text. Defaults to
# SCENARIO.md's narrative, but is deliberately overridable via env var
# (checked after .env is loaded above, so it can be set there too) so a
# different deployment (a different plant, a real P&ID narrative, a
# maintenance-log excerpt, etc.) can point this at its own prose file
# without any code change -- this is what keeps topology extraction
# extensible instead of being tied to this one hackathon's dataset.
PROCESS_DESCRIPTION_PATH = Path(
    os.environ.get("PROCESS_DESCRIPTION_PATH", str(REPO_ROOT / "docs" / "SCENARIO.md"))
)

