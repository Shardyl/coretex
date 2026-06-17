"""Hourly: sync Mailgun unsubscribes + complaints -> newsletter_opt_out in crm_master.

Run from cron as the cortex user:
  /opt/coretex/.venv/bin/python /opt/coretex/runtime/mailgun_sync.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cortex import newsletter  # noqa: E402

if __name__ == "__main__":
    print(newsletter.sync_unsubscribes())
