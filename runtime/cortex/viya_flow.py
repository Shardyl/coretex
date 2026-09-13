"""
viya_flow.py - the Viya app's screens, driven for a golf tee booking (used by golf.Run.book).

Screen map (Viya 1.2.25, Play Store build, mapped live 13 Sep 2026; resource ids are stable anchors):
  Home          : tabs 'GOLF' ...                         -> tap 'GOLF'
  Golf          : course cards 'Earth Course' / 'Fire Course' / 'Majlis Course', each with 'Book Now'
  Book Tee Time : club_layout per course, holes '9'/'18' (count_text), month strip (hc_text_top) and day
                  strip (hc_text_middle, centred selection, swipe to scroll), 'Find Availability'
  Slots         : rows txt_time 'HH:MM' with player_one..four_layout; a layout is CLICKABLE only if that
                  many places are free; 'Earlier' / 'Later' page the rows; 'Select Time'
  Players       : per extra player edtxt_firstname_player / edtxt_lastname_player + member-type chips
                  (typeLayout: 'JGE Member' / 'JGE Weekday Members' / 'JGE Country Club Members');
                  'Confirm Players'
Weekends run a different (allocated) system: this flow is for weekday bookings only.

Every value that is behaviour (courses, order, players, member type, holes) comes from the skill PLAN.
"""
import re
import time

PKG = "com.wasl.viya.app"


def _rid(n) -> str:
    return n["id"].split("/")[-1]


def _by(ns, rid):
    return [n for n in ns if _rid(n) == rid]


def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class Viya:
    def __init__(self, run):
        self.r, self.ph, self.p = run, run.ph, run.p

    def title(self, ns) -> str:
        t = _by(ns, "toolbar_title")
        return t[0]["label"] if t else ""

    # ---- navigation ----
    def home_to_booking(self, course: str):
        """Fresh start: force-stop Viya, open it, GOLF, then the course's Book Now."""
        self.ph.shell(f"am force-stop {PKG}")
        self.ph.launch(PKG)
        self.ph.tap_text(r"^GOLF$", timeout=20)
        ns = self.ph.wait_for(rf"^{course} Course$", timeout=15) and self.ph.nodes()
        name = [n for n in ns if n["label"] == f"{course} Course"]
        if not name:
            raise LookupError(f"{course} Course not on the golf screen")
        book = min(self.ph.find(r"^Book Now$", ns), key=lambda n: abs(n["y"] - name[0]["y"]))
        self.ph.tap(book)
        if not self.ph.wait_for(r"^Find Availability$", timeout=15):
            raise LookupError("Book Tee Time screen did not open")

    def pick_course(self, course: str, ns=None):
        ns = ns or self.ph.nodes()
        t = [n for n in _by(ns, "textTitle") if n["label"] == f"{course} Course"]
        if not t:
            raise LookupError(f"{course} Course not on Book Tee Time")
        lay = min(_by(ns, "club_layout"), key=lambda n: abs(n["x"] - t[0]["x"]) + abs(n["y"] - t[0]["y"]))
        self.ph.tap(lay)

    def pick_holes(self, holes: int, ns=None):
        ns = ns or self.ph.nodes()
        h = [n for n in _by(ns, "count_text") if n["label"] == str(holes)]
        if h:
            self.ph.tap(h[0])

    # ---- slots ----
    def slot_rows(self, ns, players: int):
        """[(minutes, 'HH:MM', button)] for rows whose N-player button is clickable (N places free)."""
        rid = {1: "player_one_layout", 2: "player_two_layout", 3: "player_three_layout",
               4: "player_four_layout"}[players]
        btns = [b for b in _by(ns, rid) if b["clickable"]]
        out = []
        for t in _by(ns, "txt_time"):
            b = [x for x in btns if abs(x["y"] - t["y"]) < 25]
            if b and re.match(r"^\d\d:\d\d$", t["label"]):
                out.append((_hhmm(t["label"]), t["label"], b[0]))
        return sorted(out)

    def choose(self, attempt: dict, rows):
        if attempt.get("exact"):
            hit = [r for r in rows if r[1] == attempt["exact"]]
        else:
            hit = [r for r in rows if r[0] <= _hhmm(attempt["latest"])]
        return hit[0] if hit else None

    # ---- form ----
    def prepare(self, course: str, day):
        """Fresh form on `course`, holes from PLAN, day strip swiped to `day` and tapped. Before release."""
        self.home_to_booking(course)
        self.pick_holes(int(self.p.get("holes", 18)))
        want = f"{day.day:02d}"
        seen_month_end = day.day > 20      # a low target day (e.g. 05) must come AFTER the month wrap
        for _ in range(12):
            ns = self.ph.nodes()
            days = _by(ns, "hc_text_middle")
            labels = [n["label"] for n in days]
            if any(int(x) < int(labels[0]) for x in labels if x.isdigit()) or "01" in labels:
                seen_month_end = True
            hit = [n for n in days if n["label"] == want]
            if hit and seen_month_end:
                self.ph.tap(hit[0]); time.sleep(0.8)
                self.r.log(f"form ready: {course}, {self.p.get('holes', 18)} holes, day {want} tapped")
                return
            y = days[0]["y"] - 24
            self.ph.swipe(972, y, 108, y, 200); time.sleep(0.7)
        raise LookupError(f"could not reach day {want} on the strip")

    def find_availability(self, timeout=15.0) -> str:
        """Tap Find Availability -> 'slots' (the list), 'closed' (Viya's 'Incomplete Data' popup, which is
        what an unopened day returns; dismissed here), 'assigned' (anything else, e.g. a tournament day
        where the app assigns the time)."""
        self.ph.tap_text(r"^Find Availability$", timeout=5)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ns = self.ph.nodes()
            if _by(ns, "btnConfirmTime") or _by(ns, "player_one_txt"):
                return "slots" if _by(ns, "player_one_txt") else "assigned"
            if [n for n in _by(ns, "tv_title") if n["label"] == "Incomplete Data"]:
                self.ph.tap_text(r"^Ok$", timeout=3)
                return "closed"
            if _by(ns, "btnConfirmPlayer") or (self.title(ns) and self.title(ns) != "Book Tee Time"):
                return "assigned"
            time.sleep(0.2)
        return "timeout"

    def rows_view(self, players: int, want_until: int | None = None):
        """Slot rows with N places free; pages 'Later' while the last visible row is before want_until."""
        ns = self.ph.nodes()
        rows = self.slot_rows(ns, players)
        for _ in range(3):
            times = sorted(_hhmm(t["label"]) for t in _by(ns, "txt_time") if re.match(r"^\d\d:\d\d$", t["label"]))
            if want_until is None or not times or times[-1] >= want_until or not _by(ns, "later_layout"):
                break
            self.ph.tap(_by(ns, "later_layout")[0]); time.sleep(0.8)
            ns = self.ph.nodes()
            rows = rows + [r for r in self.slot_rows(ns, players) if r[1] not in {x[1] for x in rows}]
        return ns, sorted(rows)

    # ---- players ----
    def fill_players(self, names: list[str], member_type: str):
        """One dump, then type straight through. Names are 'First Last'."""
        ns = self.ph.nodes()
        if self.title(ns) != "Players":
            raise LookupError(f"expected the Players screen, got '{self.title(ns)}'")
        firsts = sorted(_by(ns, "edtxt_firstname_player"), key=lambda n: n["y"])
        lasts = sorted(_by(ns, "edtxt_lastname_player"), key=lambda n: n["y"])
        chips = [n for n in _by(ns, "item_text") if n["label"] == member_type]
        chips.sort(key=lambda n: n["y"])
        if len(firsts) < len(names) or len(chips) < len(names):
            raise LookupError(f"Players screen has {len(firsts)} name rows / {len(chips)} '{member_type}' chips")
        # chips FIRST: once the keyboard opens it covers the lower player's chips (name fields stay put)
        for i in range(len(names)):
            self.ph.tap(chips[i])
        for i, full in enumerate(names):
            first, _, last = full.partition(" ")
            self.ph.tap(firsts[i]); self.ph.type_text(first or "x")
            self.ph.tap(lasts[i]); self.ph.type_text(last or "x")
