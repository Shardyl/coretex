"""
android.py - drive an Android phone over adb (wireless debugging, reached across Tailscale).

Screen elements are found by their visible text or content-desc from a uiautomator dump, never by
fixed coordinates, so a layout shift does not tap the wrong thing. Runs as the cortex user on the box;
the adb pairing key lives in that user's ~/.android.

    python -m cortex.integrations.android <serial> probe   # print every labelled element on screen
    python -m cortex.integrations.android <serial> shot out.png
"""
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


class Phone:
    def __init__(self, serial: str):
        self.serial = serial

    # ---- transport ----
    def adb(self, *args, timeout=15, binary=False):
        r = subprocess.run(["adb", "-s", self.serial, *args], capture_output=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)}: {r.stderr.decode(errors='replace').strip()[:200]}")
        return r.stdout if binary else r.stdout.decode(errors="replace")

    def shell(self, cmd, timeout=15):
        return self.adb("shell", cmd, timeout=timeout)

    def connect(self) -> bool:
        r = subprocess.run(["adb", "connect", self.serial], capture_output=True, text=True, timeout=20)
        if "connected" not in r.stdout:
            return False
        try:
            return self.shell("echo ok").strip() == "ok"
        except Exception:  # noqa: BLE001
            return False

    # ---- device state ----
    def wake(self):
        self.shell("input keyevent KEYCODE_WAKEUP")

    def locked(self) -> bool:
        out = self.shell("dumpsys window | grep -E 'mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen'")
        return bool(re.search(r"(mDreamingLockscreen|isKeyguardShowing|mShowingLockscreen)=true", out))

    def foreground(self) -> str:
        out = self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
        m = re.search(r"([a-zA-Z0-9_.]+)/[a-zA-Z0-9_.$]+", out)
        return m.group(1) if m else ""

    def launch(self, pkg: str):
        self.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")

    # ---- screen ----
    def nodes(self) -> list[dict]:
        xml = self.adb("exec-out", "sh", "-c", "uiautomator dump /sdcard/u.xml >/dev/null && cat /sdcard/u.xml",
                       timeout=20)
        xml = xml[xml.find("<?xml"):] if "<?xml" in xml else xml
        out = []
        for n in ET.fromstring(xml).iter("node"):
            label = (n.get("text") or "").strip() or (n.get("content-desc") or "").strip()
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds", ""))
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            out.append({"label": label, "id": n.get("resource-id", ""), "cls": n.get("class", ""),
                        "clickable": n.get("clickable") == "true", "enabled": n.get("enabled") != "false",
                        "x": (x1 + x2) // 2, "y": (y1 + y2) // 2, "box": (x1, y1, x2, y2)})
        return out

    def find(self, pattern, ns=None) -> list[dict]:
        rx = re.compile(pattern, re.I | re.S)
        return [n for n in (ns if ns is not None else self.nodes()) if n["label"] and rx.search(n["label"])]

    def wait_for(self, pattern, timeout=10.0, interval=0.25) -> list[dict]:
        end = time.monotonic() + timeout
        while True:
            hits = self.find(pattern)
            if hits or time.monotonic() >= end:
                return hits
            time.sleep(interval)

    def tap(self, node_or_xy):
        x, y = (node_or_xy["x"], node_or_xy["y"]) if isinstance(node_or_xy, dict) else node_or_xy
        self.shell(f"input tap {x} {y}")

    def tap_text(self, pattern, timeout=10.0) -> dict:
        hits = self.wait_for(pattern, timeout)
        if not hits:
            raise LookupError(f"not on screen: {pattern}")
        self.tap(hits[0])
        return hits[0]

    def type_text(self, s: str):
        self.shell("input text " + "'" + s.replace("'", "").replace(" ", "%s") + "'")

    def swipe(self, x1, y1, x2, y2, ms=250):
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {ms}")

    def back(self):
        self.shell("input keyevent KEYCODE_BACK")

    def screenshot(self, path: str):
        with open(path, "wb") as f:
            f.write(self.adb("exec-out", "screencap", "-p", binary=True, timeout=20))


if __name__ == "__main__":
    serial, cmd = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "probe")
    ph = Phone(serial)
    if not ph.connect():
        sys.exit(f"cannot reach phone at {serial}")
    if cmd == "probe":
        ph.wake()
        print("locked:", ph.locked(), "| foreground:", ph.foreground())
        for n in ph.nodes():
            if n["label"]:
                print(f"{'*' if n['clickable'] else ' '} ({n['x']},{n['y']}) {n['label']!r} {n['id']}")
    elif cmd == "shot":
        ph.screenshot(sys.argv[3] if len(sys.argv) > 3 else "phone.png")
