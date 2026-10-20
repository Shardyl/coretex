"""One finder for everything Talk looks up by name (owner, 16 Oct 2026: "I'm talking to it, so the spelling isn't
always right, but it doesn't infer or try and intelligently find out the correct option").

The 16 Oct audit found every lookup was a plain substring match: "Kabila" never found Qabilah, "Wealth" never found
WELF, "Boo Boo Land" never found Boo Boo Laand, "Rhonda Alhamad" never found Randa Al Hammad. Here a query is
matched word by word against the candidate's words in three ways, best one wins per word:
  exact or prefix           1.0 / 0.9
  sound-alike               0.85 (Postgres fuzzystrmatch dmetaphone, primary or alternate code equal), 0.6 when the
                            codes are one letter apart ("Sharma" XRM / "Shama" XM)
  spelling                  trigram similarity, the pg_trgm measure, computed here
then ranked up for what is live: an open opportunity, recent activity, a client. Heard-wrong words the owner has
taught (setting `talk_aliases`, {heard: means}) are swapped in first; the team's own names are seeded.
Code only, no model: a model call per lookup would be slow and is not needed to rank a few hundred candidates."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache

from . import db

_SEED_ALIASES = {"richard": "rashad", "irish": "ayresh", "aries": "ayresh", "gina": "gino", "wealth": "welf",
                 "sharma": "shama"}
_STOP = {"the", "and", "for", "from", "with", "llc", "fzco", "fze", "ltd", "group", "company", "at", "of", "a",
         "an", "in", "on", "to", "my", "our", "deal", "opportunity", "project", "card", "quote", "quotation"}
_OPEN_STAGES = ("Opportunity", "Quote", "Booked", "Production", "Final Payment", "Recurring", "Close & review")


def aliases() -> dict:
    a = dict(_SEED_ALIASES)
    try:
        a.update({str(k).lower(): str(v).lower() for k, v in (db.setting_get("talk_aliases") or {}).items()})
    except Exception:  # noqa: BLE001
        pass
    return a


def remember_alias(heard: str, means: str) -> dict:
    a = db.setting_get("talk_aliases") or {}
    a[str(heard).strip().lower()] = str(means).strip().lower()
    db.setting_set("talk_aliases", a)
    return a


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 2]


def query_words(q: str) -> list[str]:
    al = aliases()
    out = []
    for w in words(q):
        w = al.get(w, w)
        if w not in _STOP:
            out.extend(words(w))
    return out


def _tri(w: str) -> set:
    s = f"  {w} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def trigram(a: str, b: str) -> float:
    ta, tb = _tri(a), _tri(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


_PHON: dict = {}


def _phon(ws: list[str]) -> None:
    need = sorted({w for w in ws if w not in _PHON and not w.isdigit()})
    for i in range(0, len(need), 2000):
        chunk = need[i:i + 2000]
        try:
            for r in db.query("select w, dmetaphone(w) p, dmetaphone_alt(w) a from unnest(%s::text[]) w", (chunk,)):
                _PHON[r["w"]] = {c for c in (r["p"], r["a"]) if c}
        except Exception:  # noqa: BLE001 - no extension: spelling and exact matching still work
            for w in chunk:
                _PHON[w] = set()


@lru_cache(maxsize=4096)
def _lev1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    if len(a) > len(b):
        a, b = b, a
    return any(b[:i] + b[i + 1:] == a for i in range(len(b)))


def word_score(q: str, w: str) -> float:
    if q == w:
        return 1.0
    if len(q) >= 3 and (w.startswith(q) or q.startswith(w) and len(w) >= 3):
        return 0.9
    best = trigram(q, w)
    pq, pw = _PHON.get(q) or set(), _PHON.get(w) or set()
    if pq & pw and min(len(q), len(w)) >= 3:
        # SOUNDING ALIKE IS NOT A TIE (20 Oct 2026): "ticky" sounds like "Tikkie" AND "DAWAK" (both TK), and a new
        # opportunity's stage boost put DAWAK first. Spelling closeness and the first letter break the tie.
        best = max(best, min(0.89, 0.8 + 0.3 * best + (0.03 if q[:1] == w[:1] else 0)))
    elif (min(len(q), len(w)) >= 4 and best >= 0.2 and min(len(q), len(w)) / max(len(q), len(w)) >= 0.6
          # one letter apart in sound AND some shared spelling: "Sharma" / "Shama", never "form" / "farheen"
          and any(len(x) >= 3 and len(y) >= 3 and _lev1(x, y) for x in pq for y in pw)):
        best = max(best, 0.6)
    return best


def score(qws: list[str], text: str) -> float:
    """Mean over the query's words of the best match among the candidate's words."""
    cws = words(text)
    if not qws or not cws:
        return 0.0
    cws = cws + [a + b for a, b in zip(cws, cws[1:]) if len(a) <= 3]
    return sum(max(word_score(q, c) for c in cws) for q in qws) / len(qws)


def _rank(qws: list[str], cands: list[dict], text_of, boost_of, floor: float, limit: int) -> list[dict]:
    ws = [w for c in cands for w in words(text_of(c))]
    _phon(qws + ws + [a + b for a, b in zip(ws, ws[1:]) if len(a) <= 3])
    out = []
    for c in cands:
        s = score(qws, text_of(c))
        if s >= floor:
            out.append({**c, "score": round(min(1.0, s + boost_of(c)), 3), "match": round(s, 3)})
    out.sort(key=lambda x: (-x["score"], -(x.get("id") or 0)))
    if out:     # only what is close to the best: a strong match is not listed beside noise
        out = [x for x in out if x["score"] >= out[0]["score"] - 0.2]
    return out[:limit]


def deals(q: str, company_labels: list[str] | None = None, limit: int = 5, include_closed: bool = False) -> list[dict]:
    """Opportunities and projects by what he calls them: title, client account, contact names and emails."""
    qws = query_words(q)
    if not qws:
        return []
    where = "true" if include_closed else ("(p.stage = any(%s) or p.updated_at > now() - interval '60 days')")
    params: list = [] if include_closed else [list(_OPEN_STAGES)]
    if company_labels:
        where += " and lower(p.company) = any(%s)"
        params.append([c.lower() for c in company_labels])
    rows = db.query("select p.id, p.title, p.company, p.stage, p.contact_email, p.contacts, p.updated_at, "
                    "a.name account from crm_projects p left join crm_accounts a on a.id=p.account_id "
                    f"where {where} order by p.id desc limit 800", tuple(params))

    def text(r):
        names = " ".join(f"{c.get('name') or ''} {c.get('email') or ''}" for c in (r.get("contacts") or []))
        return f"{r['title']} {r.get('account') or ''} {names} {r.get('contact_email') or ''}"

    def boost(r):
        return (0.1 if r.get("stage") in ("Opportunity", "Quote") else 0.05 if r.get("stage") in _OPEN_STAGES else 0)

    return [{k: v for k, v in r.items() if k not in ("contacts", "updated_at")}
            for r in _rank(qws, rows, text, boost, 0.6, limit)]


def contacts(q: str, limit: int = 6) -> list[dict]:
    """People by spoken name (or a phone number), live ones first: on an open deal, recently active, a client."""
    digits = re.sub(r"\D", "", q or "")
    if len(digits) >= 7:
        return db.query("select first_name, last_name, email, phone, organisation, company_name, is_client from "
                        "crm_master where regexp_replace(coalesce(phone,''), '\\D', '', 'g') like %s limit %s",
                        (f"%{digits[-9:]}", limit))
    qws = query_words(q)
    if not qws:
        return []
    _phon(qws)
    codes = sorted({c for w in qws for c in (_PHON.get(w) or set())})
    likes = [f"{w[:3]}%" for w in qws if len(w) >= 3]
    hay = "lower(coalesce(first_name,'')||' '||coalesce(last_name,''))"
    # email-less people count (a WhatsApp contact has a number, not an address); CSV-blob rows from old imports do not
    rows = db.query(
        "select first_name, last_name, email, phone, organisation, company_name, is_client, updated_at from crm_master "
        f"where length({hay}) < 70 and (similarity({hay}, %s) > 0.25 "
        "or dmetaphone(coalesce(first_name,'')) = any(%s) or dmetaphone(coalesce(last_name,'')) = any(%s) "
        f"or {hay} like any(%s) or lower(coalesce(company_name,'')) like any(%s)) "
        f"order by similarity({hay}, %s) desc limit 500",
        (" ".join(qws), codes or [""], codes or [""], [f"%{l}" for l in likes] or ["%%"],
         [f"%{l}" for l in likes] or ["%%"], " ".join(qws)))
    live = {str(r["e"]).lower() for r in db.query(
        "select lower(contact_email) e from crm_projects where stage = any(%s) and contact_email is not null "
        "union select lower(c->>'email') from crm_projects, jsonb_array_elements(contacts) c where stage = any(%s)",
        (list(_OPEN_STAGES), list(_OPEN_STAGES)))}

    def text(r):
        return f"{r.get('first_name') or ''} {r.get('last_name') or ''} {r.get('company_name') or ''}"

    def boost(r):
        recent = r.get("updated_at") and (datetime.now(timezone.utc) - r["updated_at"]).days <= 30
        return ((0.12 if (r.get("email") or "").lower() in live else 0) + (0.04 if r.get("is_client") else 0)
                + (0.05 if recent else 0))

    return [{k: v for k, v in r.items() if k != "updated_at"} for r in _rank(qws, rows, text, boost, 0.6, limit)]


def cards(q: str, company_ids: list[int] | None = None, limit: int = 8, open_only: bool = True) -> list[dict]:
    """Inbox cards by who, what or which deal (list_tasks only ever returned the newest 15)."""
    qws = query_words(q)
    where = "t.status in ('queued','new','drafting','awaiting_approval','awaiting_correction','scheduled')" \
        if open_only else "t.created_at > now() - interval '60 days'"
    params: list = []
    if company_ids:
        where += " and t.company_id = any(%s)"
        params.append(company_ids)
    rows = db.query("select t.id, t.kind, t.status, t.title, t.created_at, c.slug company, t.deal_id, p.title deal, "
                    "t.request->'inquiry'->>'name' who, t.request->'inquiry'->>'email' email, "
                    "t.request->'inquiry'->>'subject' subject, t.request->>'number' number, "
                    "left(coalesce(t.draft,''),160) preview from tasks t join companies c on c.id=t.company_id "
                    f"left join crm_projects p on p.id=t.deal_id where {where} order by t.id desc limit 600",
                    tuple(params))
    if not qws:
        return rows[:limit]

    def text(r):
        return " ".join(str(r.get(k) or "") for k in ("title", "deal", "who", "email", "subject", "number"))

    return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
            for r in _rank(qws, rows, text, lambda r: 0, 0.6, limit)]


def company(name: str) -> str | None:
    """A business named as he says it ('Sky Vision', 'Sensa Productions', 'tab scanner') -> its slug."""
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not n:
        return None
    rows = db.query("select slug, name from companies")
    for r in rows:
        if n in (r["slug"], re.sub(r"[^a-z0-9]", "", (r["name"] or "").lower())):
            return r["slug"]
    best = max(rows, key=lambda r: max(trigram(n, r["slug"]),
                                       trigram(n, re.sub(r"[^a-z0-9]", "", (r["name"] or "").lower()))), default=None)
    if best and max(trigram(n, best["slug"]), trigram(n, re.sub(r"[^a-z0-9]", "", (best["name"] or "").lower()))) > 0.45:
        return best["slug"]
    return None
