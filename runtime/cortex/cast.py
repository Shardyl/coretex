"""Who's who on an opportunity: the cast list (owner, 17 Oct 2026).

"It had all the information and still came out with a stupid response." Card 1297 (Heaven Properties) thanked
Balazs and asked him to pass our thanks on, although Balazs only made the introduction and left in the first minute,
Abraham ran the call and closed it asking for the proposal, and a 10 Oct note said Balazs asked to be copied, not
addressed. No model got that wrong: CODE chose the recipient (the person on the newest thread) and the sender (whoever
of us had sent the most on the deal), and nothing read the full meeting or the note. The same rules kept Gino as the
sender after the owner stepped in, and would ignore "I'm taking over this project" from a new person.

So every opportunity has a CAST: their side (each person's role: lead, decision maker, member, copy, support, left)
and ours (who leads for us). It is re-read on every event with the full material, not a summary:
    email in or out           a model reads the message and the current cast: does this change who's who?
    a meeting                 the best model reads the full notes and transcript
    his note / Talk           his words; anything he states is PINNED and never overridden by a later read
    one of us writes by hand  deterministic: that person now leads for us (unless another is pinned)
Code then obeys the cast: the recipient of every follow-up, who is copied, who sends. Every change is logged on the
opportunity with its reason. The models are data (setting `cast_models`), owner-approved 17 Oct 2026."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import db

ROLES = ("lead", "decision", "member", "cc", "support", "left")
ADDRESSABLE = ("lead", "decision", "member")           # may be written TO
COPIED = ("lead", "decision", "member", "cc", "support")  # stay on the copy line
_DEFAULT_MODELS = {"email": "claude-sonnet-5", "note": "claude-sonnet-5", "meeting": "claude-opus-5"}
_EMAIL_RX = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema() -> None:
    db.execute("alter table crm_projects add column if not exists cast_list jsonb")


def _models() -> dict:
    return {**_DEFAULT_MODELS, **(db.setting_get("cast_models") or {})}


def _team(company_label: str | None = None) -> dict:
    """Our own people: {email: name}, from every company's signature roster (one team runs the sister brands)."""
    out = {}
    for r in db.query("select data from company_profiles"):
        for em, s in ((r.get("data") or {}).get("signatures") or {}).items():
            if "@" in str(em):
                out[str(em).lower()] = (s or {}).get("name") or str(em).split("@")[0]
    return out


def get(deal_id) -> dict:
    """The cast, built from the deal's contacts the first time it is asked for."""
    ensure_schema()
    p = db.one("select id, contacts, contact_email, owner, cast_list from crm_projects where id=%s", (int(deal_id),))
    if not p:
        return {}
    c = p.get("cast_list") or {}
    if c.get("their") is not None:
        return c
    prim = (p.get("contact_email") or "").lower()
    their = []
    for x in p.get("contacts") or []:
        em = (x.get("email") or "").lower()
        if em and em not in {t["email"] for t in their}:
            their.append({"email": em, "name": x.get("name") or "", "role": "lead" if em == prim else "member",
                          "pinned": False, "why": "from the opportunity's contacts", "at": _now()})
    if prim and prim not in {t["email"] for t in their}:
        their.insert(0, {"email": prim, "name": "", "role": "lead", "pinned": False, "why": "primary contact", "at": _now()})
    c = {"their": their, "ours": {"email": (p.get("owner") or "").lower() or None, "pinned": False, "why": "", "at": _now()},
         "log": []}
    _save(int(deal_id), c)
    return c


def _save(deal_id: int, c: dict) -> None:
    c["log"] = (c.get("log") or [])[-40:]
    db.execute("update crm_projects set cast_list=%s::jsonb where id=%s", (json.dumps(c, default=str), int(deal_id)))
    lead = lead_of(c)
    if lead:     # the opportunity's primary contact IS the lead: every older path that reads contact_email follows
        p = db.one("select contacts, contact_email from crm_projects where id=%s", (int(deal_id),)) or {}
        lst = [dict(x) for x in (p.get("contacts") or [])]
        for t in c.get("their") or []:
            if t.get("role") == "left":
                continue
            x = next((x for x in lst if (x.get("email") or "").lower() == t["email"]), None)
            if not x:
                lst.append({"email": t["email"], "name": t.get("name") or "", "role": "", "primary": False})
            elif not x.get("name") and t.get("name"):
                x["name"] = t["name"]
        for x in lst:
            x["primary"] = (x.get("email") or "").lower() == lead
        db.execute("update crm_projects set contacts=%s::jsonb, contact_email=%s where id=%s",
                   (json.dumps(lst), lead, int(deal_id)))


def lead_of(c: dict) -> str | None:
    for role in ("lead", "decision", "member"):
        for t in c.get("their") or []:
            if t.get("role") == role:
                return t["email"]
    return None


def lead(deal_id) -> str | None:
    try:
        return lead_of(get(deal_id)) if deal_id else None
    except Exception:  # noqa: BLE001
        return None


def our_lead(deal_id) -> str | None:
    try:
        return ((get(deal_id) or {}).get("ours") or {}).get("email") or None if deal_id else None
    except Exception:  # noqa: BLE001
        return None


def copied(deal_id, to_email: str = "") -> list[str]:
    """Everyone on their side who stays copied, never the To, never anyone who has left."""
    c = get(deal_id) if deal_id else {}
    return [t["email"] for t in c.get("their") or [] if t.get("role") in COPIED and t["email"] != (to_email or "").lower()]


def left(deal_id) -> set:
    c = get(deal_id) if deal_id else {}
    return {t["email"] for t in c.get("their") or [] if t.get("role") == "left"}


def describe(deal_id) -> str:
    """One block for the drafter and the screens: who is who, and why."""
    c = get(deal_id) if deal_id else {}
    if not c:
        return ""
    team = _team()
    names = {"lead": "LEAD (write to them)", "decision": "decision maker", "member": "on the team",
             "cc": "copy only", "support": "support, copy only", "left": "LEFT, do not write to or copy"}
    lines = [f"- {t.get('name') or t['email']} <{t['email']}>: {names.get(t.get('role'), t.get('role'))}"
             + (f" ({t['why']})" if t.get("why") else "") for t in c.get("their") or []]
    o = (c.get("ours") or {}).get("email")
    if o:
        lines.append(f"- OUR LEAD: {team.get(o, o)} <{o}>" + (f" ({c['ours']['why']})" if c["ours"].get("why") else ""))
    return "\n".join(lines)


def _apply(deal_id: int, c: dict, out: dict, source: str, pin: bool, known: dict) -> list[str]:
    """Code applies a read: roles from the enum, people we can name, pinned entries untouched."""
    done = []
    their = {t["email"]: t for t in c.get("their") or []}
    for ch in (out.get("changes") or [])[:12]:
        em = str(ch.get("email") or "").strip().lower()
        role = str(ch.get("role") or "").strip().lower()
        why = str(ch.get("why") or "")[:200]
        if role not in ROLES or "@" not in em or em in _team():
            continue
        if em not in their and em not in known:
            continue                                   # a person the material never named is never invented
        cur = their.get(em)
        if cur and cur.get("pinned") and not pin:
            continue                                   # his word stands until he changes it
        if cur and cur.get("role") == role and not pin:
            continue
        if role == "lead":                             # one lead: the previous one stays on the team
            for t in their.values():
                if t.get("role") == "lead" and t["email"] != em and not (t.get("pinned") and not pin):
                    t["role"] = "member"
        entry = cur or {"email": em, "name": known.get(em, "")}
        if not entry.get("name") and str(ch.get("name") or "").strip():
            entry["name"] = str(ch["name"]).strip()[:80]
        entry.update({"role": role, "why": f"{why} ({source})" if why else source, "at": _now(),
                      "pinned": bool(pin)})
        their[em] = entry
        done.append(f"{entry.get('name') or em}: {role}")
    ol = out.get("our_lead") or {}
    oem = str(ol.get("email") or "").strip().lower() if isinstance(ol, dict) else ""
    if oem and oem in _team() and oem != (c.get("ours") or {}).get("email"):
        if pin or not (c.get("ours") or {}).get("pinned"):
            c["ours"] = {"email": oem, "pinned": bool(pin), "why": f"{str(ol.get('why') or '')[:200]} ({source})", "at": _now()}
            done.append(f"our lead: {_team().get(oem, oem)}")
    if done:
        c["their"] = list(their.values())
        c["log"] = (c.get("log") or []) + [{"at": _now(), "source": source, "changes": done}]
        _save(deal_id, c)
        try:
            from . import pipeline
            pipeline.log_deal(deal_id, "cast", f"Who's who updated from {source}: " + "; ".join(done))
        except Exception:  # noqa: BLE001
            pass
    return done


_READ = """You keep the cast list of one sales opportunity: who is who on the CLIENT's side, and which of OUR people
leads it. You are given the current cast and ONE new piece of material (an email, a meeting's full notes and
transcript, or the owner's own note). Decide whether the material CHANGES who is who. Most material changes nothing:
then return no changes.
Roles for the client's people: lead (the person we write TO: who runs it on their side, the one who asked for the next
step, who said they are taking over), decision (decides, involved), member (on their team, involved), cc (keep informed
only), support (helps or introduced us, not running it, copy only), left (no longer involved: left the company, handed
over, dropped out). Read behaviour, not titles: a person who only made the introduction and left the call is support;
a person who ran the meeting and asked for the proposal is the lead; "I'm taking over this project" makes the writer
the lead; "please deal with X from now on" makes X the lead. A note from OUR owner saying who to address or copy is
an instruction: follow it exactly.
Our lead: the one of OUR people who is now running it, when the material shows a change (they stepped into the thread,
ran the meeting, or the owner says so). Otherwise leave it out.
Only name people whose email address appears in the material or the current cast. Never invent an address.
Return JSON: {"changes": [{"email": "...", "name": "<their full name as the material gives it>", "role":
"lead|decision|member|cc|support|left", "why": "<one line, in the material's own terms>"}], "our_lead": {"email": "...",
"why": "..."} or null}"""


def read(deal_id, material: str, *, kind: str = "email", source: str = "", people: dict | None = None,
         pin: bool = False, apply: bool = True, ours: bool = True) -> dict:
    """Re-read who's who from one piece of FULL material. kind: email | meeting | note. `people` = {email: name}
    named in the material (headers, attendees). Returns {"changes": [...], "applied": [...]}"""
    from . import provider
    if not deal_id or not (material or "").strip():
        return {}
    c = get(deal_id)
    if not c:
        return {}
    known = {**{k.lower(): v for k, v in (people or {}).items()},
             **{t["email"]: t.get("name") or "" for t in c.get("their") or []}}
    for em in _EMAIL_RX.findall(material or ""):
        known.setdefault(em.lower().strip("."), "")
    team = _team()
    cur = describe(deal_id) or "(nobody yet)"
    d = db.one("select title, stage from crm_projects where id=%s", (int(deal_id),)) or {}
    model = _models().get(kind) or _DEFAULT_MODELS["email"]
    budget = 60000 if kind == "meeting" else 12000
    user = (f"OPPORTUNITY #{deal_id}: {d.get('title')} (stage {d.get('stage')})\n\nOUR PEOPLE (never the client): "
            + ", ".join(f"{n} <{e}>" for e, n in team.items()) + f"\n\nCURRENT CAST:\n{cur}\n\nPEOPLE NAMED IN THIS "
            f"MATERIAL: " + ", ".join(f"{n or ''} <{e}>".strip() for e, n in known.items() if e not in team)
            + f"\n\nTHE MATERIAL ({kind}):\n{material[:budget]}")
    try:
        out = provider.think_json(_READ, user, model=model, max_tokens=1500, purpose=f"cast-{kind}") or {}
    except Exception as e:  # noqa: BLE001
        print(f"[cast] read deal {deal_id}: {type(e).__name__}: {e}", flush=True)
        return {}
    if not ours:      # an email Cortex sent from a mailbox CODE chose says nothing about who leads for us
        out["our_lead"] = None
    res = {"changes": out.get("changes") or [], "our_lead": out.get("our_lead")}
    if apply:
        res["applied"] = _apply(int(deal_id), c, out, source or kind, pin or kind == "note", known)
    return res


def we_spoke(deal_id, our_email: str, source: str = "") -> list[str]:
    """One of OUR people wrote on the opportunity by hand: they lead for us from now on (owner, 17 Oct 2026: "if I
    come into the thread and start talking, it looks like I'm taking the lead"). Deterministic; a pin stands."""
    em = (our_email or "").lower()
    if not deal_id or em not in _team():
        return []
    c = get(deal_id)
    if (c.get("ours") or {}).get("email") == em or (c.get("ours") or {}).get("pinned"):
        return []
    return _apply(int(deal_id), c, {"our_lead": {"email": em, "why": "wrote on the thread by hand"}},
                  source or "their own email", False, {})


def read_async(*a, **k) -> None:
    import threading
    threading.Thread(target=lambda: read(*a, **k), daemon=True, name="cast-read").start()
