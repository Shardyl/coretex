#!/usr/bin/env bash
# Cortex deploy (13 Oct 2026): push, pull on the box, run the case library, restart ONLY if every case passes.
# A failing case rolls the box back to the commit it was running, and the services are not restarted.
#   bin/deploy.sh          full library (a few Haiku calls, under a cent)
#   bin/deploy.sh --fast   code-only cases
set -e
cd "$(dirname "$0")/.."
# a file that does not even compile never leaves this machine (13 Oct 2026: a broken string reached GitHub; the box
# gate caught it and rolled back, but nothing broken should be pushed at all)
for f in runtime/cortex/*.py; do python -W error -m py_compile "$f" || { echo "SYNTAX CHECK FAILED: $f, nothing was pushed"; exit 1; }; done
# a name used but never defined is caught HERE, before anything is pushed (13 Oct 2026: 'subj' in the sent sweep)
if python -m pyflakes runtime/cortex/*.py 2>&1 | grep -i "undefined name\|referenced before assignment"; then
  echo "STATIC CHECK FAILED: fix the names above, nothing was pushed"; exit 1
fi
git pull -q --rebase --autostash
git push -q
ssh cortex "set -e
cd /opt/coretex
PREV=\$(sudo -u cortex git rev-parse HEAD)
sudo -u cortex git pull -q --ff-only
cd runtime
if sudo -u cortex /opt/coretex/.venv/bin/python -m cortex.regress $1 2>&1 | grep -v '^\[provider\]\|^\[qualify\]\|^\[corrections\]'; [ \${PIPESTATUS[0]} -eq 0 ]; then
  sudo systemctl restart cortex-api cortex-engine
  echo \"deployed \$(sudo -u cortex git -C /opt/coretex rev-parse --short HEAD): \$(systemctl is-active cortex-api cortex-engine | tr '\n' ' ')\"
else
  sudo -u cortex git -C /opt/coretex reset -q --hard \$PREV
  echo \"CASE LIBRARY FAILED: box rolled back to \${PREV:0:7}, services NOT restarted\"
  exit 1
fi"
