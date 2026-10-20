__version__ = "0.1.0"

# CORTEX NEVER RUNS AS ROOT (20 Oct 2026): hand-run scripts as root left 675 root-owned files in Cortex's own folders;
# the API (user cortex) then could not overwrite quotation-sensa-SEN-2026-0049.xlsx and every correction on card 1374
# crashed. Run as the app: sudo -u cortex /opt/coretex/.venv/bin/python ...  (CORTEX_AS_ROOT=1 overrides, deliberately)
import os as _os
if hasattr(_os, "geteuid") and _os.geteuid() == 0 and not _os.environ.get("CORTEX_AS_ROOT"):
    raise SystemExit("Cortex runs as the cortex user: sudo -u cortex /opt/coretex/.venv/bin/python ...")
