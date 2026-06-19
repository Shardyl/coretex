"""Newsletter machinery.

Stages:
  IDEATION  - plain-text ideas (generate_idea / reset_idea_task), rules-aware.
  BUILD     - compose() the issue (rules-aware JSON) + an optional Gemini hero + render brand-true,
              email-safe HTML and a plain-text twin (build()).
  TEST SEND - on approval of a `newsletter_idea` task: build -> send to the company TEST GROUP from its
              own Mailgun domain -> drop a `newsletter_review` card (execute_idea_approval).
  FULL SEND - on approval of the `newsletter_review` card: send the SAME issue to the full audience,
              list minus opt-out / bounced / not-interested (execute_send_all).

Everything is on-brand from the company's cached brand kit (cortex.brand) and obeys the standing rules
(worker._rules_block). Models follow the global tier (Sonnet during the current trial).
"""
from __future__ import annotations

import base64
import html as _html

from psycopg.types.json import Json

from . import brand, db, imagegen, mailgun, provider, store, worker

# Each company's verified Mailgun newsletter sending domain (matches the per-company company rules).
SEND_DOMAINS = {1: "news.tabscanner.com", 3: "news.sensa.digital", 4: "news.skyvision.film",
                5: "news.filmspoke.ai", 26: "campaigns.snap-rewards.com"}

_IDEATION = (
    "You are proposing ONE newsletter idea at the IDEATION stage for this company's next issue. "
    "Output ONLY a short, plain-text idea: a working subject line, then 2-4 sentences on the angle, "
    "the hook, and why it lands. NEVER write HTML, markup or code (the full HTML build is a separate, "
    "later stage). Obey EVERY standing rule above, especially any rule about who the newsletter is "
    "written to attract."
)


# ---------- ideation ----------

def generate_idea(company_id: int, skill_key: str = "content-newsletter",
                  model: str | None = None) -> str:
    """model=None uses the skill's tier. Pass a model id to override (e.g. provider.MODEL_FAST) for A/B."""
    company = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, skill_key)
    system = "\n\n".join(filter(None, [
        f"You are Cortex's worker for the '{skill['name']}' skill.",
        worker._company_context(company),
        skill.get("craft") or "",
        worker._rules_block(skill),
        _IDEATION,
    ]))
    user = "Propose ONE strong newsletter idea for the next issue. Plain text only, no HTML."
    text = provider.think(system, user, model=model or worker._model_for(skill), think_hard=True,
                          max_tokens=1200, purpose="newsletter_idea", company=company.get("slug"))
    return worker._no_dashes(text.strip())


def reset_idea_task(task_id: int) -> dict:
    """Roll an ideation task back to a fresh first idea (rules-aware), attempts reset to 0."""
    task = store.get_task(task_id)
    idea = generate_idea(task["company_id"])
    store.update_task(task_id, draft=idea, status="awaiting_approval", attempts=0)
    return store.get_task(task_id)


# ---------- audience ----------

def test_group(company_id: int) -> list[dict]:
    return db.query("select email, name from newsletter_test_group "
                    "where company_id = %s and active order by id", (company_id,))


def set_test_group(email: str, company_id: int, on: bool, name: str | None = None) -> None:
    """Add or remove a contact from a company's test group — the LIVE source the [TEST] send reads, so a
    change here lands in the very next test send. Reactivates an existing inactive row rather than duping."""
    email = (email or "").strip().lower()
    if not email:
        return
    ex = db.one("select id from newsletter_test_group where company_id=%s and lower(email)=lower(%s) limit 1",
                (company_id, email))
    if on:
        if ex:
            db.execute("update newsletter_test_group set active=true where id=%s", (ex["id"],))
        else:
            db.execute("insert into newsletter_test_group (company_id, email, name, active) values (%s,%s,%s,true)",
                       (company_id, email, name))
    elif ex:
        db.execute("update newsletter_test_group set active=false where id=%s", (ex["id"],))


# literal % must be doubled for psycopg; org label comes in as a bound param.
_SUPPRESS = ("newsletter_opt_out is true "
             "or newsletter_bounced is true "
             "or coalesce(instantly_lead_status,'') ilike '%%bounce%%' "
             "or coalesce(lead_status,'') ilike '%%bounce%%' "
             "or coalesce(instantly_interest_status,'') ilike '%%not interest%%' "
             "or coalesce(lead_status,'') ilike '%%not interest%%'")


def recipients(company_id: int) -> list[dict]:
    """The full newsletter audience for a company: its contacts minus opt-out / bounced / not-interested,
    valid email only, de-duplicated. Returns [{email, first_name}]."""
    org = store.get_company(company_id)["name"]
    rows = db.query(
        "select email, first_name from crm_master where organisation ilike %s "
        "and email ~ '^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$' "
        f"and not ({_SUPPRESS}) order by email",
        (f"%{org}%",))
    seen, out = set(), []
    for r in rows:
        e = (r["email"] or "").strip().lower()
        if e and e not in seen:
            seen.add(e)
            out.append({"email": e, "first_name": (r.get("first_name") or "").strip()})
    return out


# ---------- build ----------

def compose(company_id: int, idea_text: str) -> dict:
    company = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "content-newsletter")
    system = "\n\n".join(filter(None, [
        f"You are Cortex's worker for the '{skill['name']}' skill, composing ONE newsletter issue for "
        f"{company['name']}.",
        worker._company_context(company),
        worker._rules_block(skill),
        ("Compose the issue from the approved idea. Return a JSON object with EXACTLY these fields: "
         "subject (compelling, specific, never clickbait), preheader (preview text, ~80 chars), "
         "headline (the in-email H1), intro (1-2 short sentences), sections (array of 2-4 objects, each "
         "{heading, body} where body is 1-3 short plain-text paragraphs separated by a blank line), "
         "cta_label (short button text), cta_url (a real, on-brand absolute URL for this company), "
         "hero_prompt (a short Imagen prompt for a clean, product-led, on-brand hero image with NO text "
         "in it). Write in the company voice. No em-dashes or en-dashes. The issue MUST make full sense "
         "with images off."),
    ]))
    user = f"Approved idea:\n{idea_text}\n\nCompose the full issue now as JSON."
    out = provider.think_json(system, user, model=worker._model_for(skill), max_tokens=2200,
                              purpose="newsletter_compose", company=company.get("slug"))
    return out or {}


def _profile(company_id: int) -> dict:
    r = db.one("select data from company_profiles where company_id = %s", (company_id,))
    return (r or {}).get("data") or {}


def _esc(s) -> str:
    return _html.escape(str(s or ""))


def render_html(company_id: int, c: dict, hero_cid: str | None = None) -> str:
    kit = brand.get_brand_kit(company_id) or {}
    col = kit.get("colors") or {}
    prof = _profile(company_id)
    company = store.get_company(company_id)
    primary = col.get("primary", "#1a73e8")
    ink = col.get("ink", "#111827")
    body = col.get("body", "#4C5C70")
    muted = col.get("muted", "#8494A6")
    hairline = col.get("hairline", "#E7EFF7")
    tint = col.get("tint", "#F6FAFE")
    headfont = "'%s', Arial, Helvetica, sans-serif" % (kit.get("fonts", {}).get("heading", "Arial"))
    bodyfont = "'%s', Arial, Helvetica, sans-serif" % (kit.get("fonts", {}).get("body", "Arial"))
    logo = kit.get("logo_light_url", "")

    def p(text):
        paras = [x.strip() for x in str(text or "").split("\n\n") if x.strip()]
        return "".join(
            f'<p style="margin:0 0 14px;font:16px/1.6 {bodyfont};color:{body};">{_esc(par)}</p>'
            for par in paras)

    sections = ""
    for s in (c.get("sections") or []):
        sections += (
            f'<tr><td style="padding:6px 32px 0;">'
            f'<h2 style="margin:22px 0 10px;font:600 19px/1.3 {headfont};color:{ink};">{_esc(s.get("heading"))}</h2>'
            f'{p(s.get("body"))}</td></tr>')

    hero_html = ""
    if hero_cid:
        hero_html = (f'<tr><td style="padding:0 0 4px;"><img src="cid:{hero_cid}" width="600" '
                     f'alt="{_esc(c.get("headline"))}" style="display:block;width:100%;max-width:600px;'
                     f'height:auto;border:0;border-radius:14px;"></td></tr>')

    cta = ""
    if c.get("cta_label") and c.get("cta_url"):
        cta = (f'<tr><td style="padding:18px 32px 6px;"><a href="{_esc(c.get("cta_url"))}" '
               f'style="display:inline-block;background:{primary};color:#fff;text-decoration:none;'
               f'font:600 16px/1 {headfont};padding:14px 26px;border-radius:10px;">'
               f'{_esc(c.get("cta_label"))}</a></td></tr>')

    addr = _esc(worker._no_dashes(" ".join((prof.get("address") or "").split())).rstrip(". "))
    legal = _esc(prof.get("legal_name") or company["name"])
    preheader = _esc(c.get("preheader") or "")
    _root = (SEND_DOMAINS.get(company_id) or "").split(".", 1)   # news.tabscanner.com -> tabscanner.com
    sitetext = _root[1] if len(_root) == 2 else ""
    site = ("https://" + sitetext) if sitetext else ""

    return f"""\
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting"></head>
<body style="margin:0;padding:0;background:{tint};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{tint};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0"
 style="width:600px;max-width:600px;background:#ffffff;border:1px solid {hairline};border-radius:16px;overflow:hidden;">
<tr><td style="padding:26px 32px 8px;">
{'<img src="%s" alt="%s" height="30" style="display:block;height:30px;border:0;">' % (_esc(logo), _esc(company["name"])) if logo else '<span style="font:700 22px/1 %s;color:%s;">%s</span>' % (headfont, ink, _esc(company["name"]))}
</td></tr>
{hero_html}
<tr><td style="padding:14px 32px 0;">
<h1 style="margin:0 0 12px;font:700 26px/1.25 {headfont};color:{ink};">{_esc(c.get("headline"))}</h1>
{p(c.get("intro"))}</td></tr>
{sections}
{cta}
<tr><td style="padding:26px 32px 28px;">
<hr style="border:0;border-top:1px solid {hairline};margin:18px 0;">
<p style="margin:0 0 6px;font:12px/1.6 {bodyfont};color:{muted};">
You are receiving this email from {legal}{(' · ' + addr) if addr else ''}.</p>
<p style="margin:0;font:12px/1.6 {bodyfont};color:{muted};">
{('<a href="%s" style="color:%s;">%s</a> &nbsp;·&nbsp; ' % (_esc(site), muted, _esc(sitetext))) if site else ''}
<a href="%unsubscribe_url%" style="color:{muted};text-decoration:underline;">Unsubscribe</a></p>
</td></tr>
</table></td></tr></table></body></html>"""


def render_text(company_id: int, c: dict) -> str:
    prof = _profile(company_id)
    company = store.get_company(company_id)
    lines = [c.get("headline") or company["name"], "", (c.get("intro") or "").strip(), ""]
    for s in (c.get("sections") or []):
        lines += [str(s.get("heading") or "").upper(), (s.get("body") or "").strip(), ""]
    if c.get("cta_label") and c.get("cta_url"):
        lines += [f"{c['cta_label']}: {c['cta_url']}", ""]
    lines += ["-" * 40,
              f"You are receiving this email from {prof.get('legal_name') or company['name']}.",
              prof.get("address") or "",
              "Unsubscribe: %unsubscribe_url%"]
    return worker._no_dashes("\n".join(x for x in lines if x is not None))


def build(company_id: int, idea_text: str) -> dict:
    c = compose(company_id, idea_text)
    hero = imagegen.hero(c.get("hero_prompt") or "") if c.get("hero_prompt") else None
    cid = "hero.jpg" if hero else None
    return {"subject": c.get("subject") or f"{store.get_company(company_id)['name']} newsletter",
            "html": render_html(company_id, c, hero_cid=cid), "text": render_text(company_id, c),
            "hero": hero, "content": c}


# ---------- send ----------

def _sender(company_id: int) -> tuple[str, str, str | None]:
    company = store.get_company(company_id)
    domain = SEND_DOMAINS.get(company_id)
    prof = _profile(company_id)
    return domain, f"{company['name']} <news@{domain}>", prof.get("reply_from") or prof.get("inbox_email")


def send_bulk(company_id: int, subject: str, html: str, text: str, recips: list[dict],
              hero: bytes | None, tag: str) -> int:
    domain, sender, reply_to = _sender(company_id)
    inline = [("hero.jpg", hero)] if hero else None
    sent = 0
    for i in range(0, len(recips), 900):
        chunk = recips[i:i + 900]
        rvars = {r["email"]: {"first_name": r.get("first_name") or ""} for r in chunk}
        mailgun.send(domain, sender, [r["email"] for r in chunk], subject, html, text,
                     inline=inline, recipient_vars=rvars, reply_to=reply_to, tag=tag)
        sent += len(chunk)
    return sent


# ---------- approval handlers (called from engine._execute) ----------

def execute_idea_approval(task: dict, skill: dict, company: dict, actor: str) -> dict:
    """Approve a newsletter IDEA -> build the issue, send to the TEST GROUP, drop a review card."""
    cid = company["id"]
    if cid not in SEND_DOMAINS:
        store.update_task(task["id"], status="done")
        return {"error": f"no sending domain configured for {company['name']}"}
    group = test_group(cid)
    if not group:
        store.update_task(task["id"], status="done")
        return {"error": "no test group configured for this company"}
    built = build(cid, task.get("draft") or "")
    send_bulk(cid, "[TEST] " + built["subject"], built["html"], built["text"],
              [{"email": g["email"], "first_name": g.get("name")} for g in group],
              built["hero"], tag="newsletter-test")
    store.update_task(task["id"], status="done")
    store.log_decision(task["id"], skill["id"], actor, "newsletter_test_sent",
                       note=built["subject"], snapshot={"to": [g["email"] for g in group]})
    rev = store.create_task(cid, skill["id"], "newsletter_review",
                            {"title": f"Newsletter ready: {built['subject']}", "subject": built["subject"]})
    # The card is a SIMPLE message (subject line only), never raw HTML. The built HTML lives in the
    # artifact below and in the real test email the operator reviews in their inbox.
    summary = (f"Subject: {built['subject']}\n\n"
               f"Sent to your test group. Approve to send to the full {company['name']} list.")
    store.update_task(rev["id"], draft=summary, status="awaiting_approval")
    db.setting_set(f"newsletter:{rev['id']}", {
        "subject": built["subject"], "html": built["html"], "text": built["text"],
        "hero_b64": base64.b64encode(built["hero"]).decode() if built["hero"] else None})
    return {"sent_to": f"the test group ({len(group)})", "review_task": rev["id"]}


def live_sends_on() -> bool:
    """Global kill-switch for sending to REAL customer lists. Default OFF — must be deliberately enabled."""
    return bool(db.setting_get("newsletter_live_sends"))


def execute_send_all(task: dict, skill: dict, company: dict, actor: str, confirmed: bool = False) -> dict:
    """Send the SAME built issue to the FULL audience (suppressed applied).

    TWO HARD SAFEGUARDS, enforced HERE at the send itself so no approval path (cockpit, Telegram, or a
    direct call) can bypass them:
      1. Global live-send lock (`newsletter_live_sends`) must be ON. Default OFF -> the real list is never
         reachable while testing, no matter what is tapped.
      2. `confirmed=True` is required (set only by the explicit count-confirmation flow). A plain approve
         NEVER sends.
    If a safeguard blocks, the card is parked (left awaiting_approval) and NOTHING is sent.
    """
    cid = company["id"]
    art = db.setting_get(f"newsletter:{task['id']}")
    if not art:
        store.update_task(task["id"], status="done")
        return {"error": "no built newsletter found for this card"}
    n = len(recipients(cid))
    if not live_sends_on():
        store.update_task(task["id"], status="awaiting_approval")
        return {"blocked": True, "recipients": n,
                "error": f"Live newsletter sends are OFF. This would reach {n} real {company['name']} "
                         f"contacts. Turn on live sends first."}
    if not confirmed:
        store.update_task(task["id"], status="awaiting_approval")
        return {"needs_confirm": True, "recipients": n}
    recips = recipients(cid)
    hero = base64.b64decode(art["hero_b64"]) if art.get("hero_b64") else None
    sent = send_bulk(cid, art["subject"], art["html"], art["text"], recips, hero, tag="newsletter")
    store.update_task(task["id"], status="done")
    store.log_decision(task["id"], skill["id"], actor, "newsletter_sent",
                       note=art["subject"], snapshot={"recipients": sent})
    db.setting_set(f"newsletter:{task['id']}", None)
    return {"sent_to": f"the full list ({sent})"}


def sync_unsubscribes() -> dict:
    """Sync Mailgun suppression state -> crm_master for EVERY company's sending domain:
      unsubscribes + spam complaints -> newsletter_opt_out=true
      hard bounces                   -> newsletter_bounced=true
    Both permanently exclude the contact from future sends (see _SUPPRESS). Mailgun also refuses to
    re-send to its own suppression lists; this keeps Cortex's CRM + audience counts honest. Idempotent."""
    from . import crm
    out = {}
    for domain in set(SEND_DOMAINS.values()):
        opt_addrs: set[str] = set()
        for kind in ("unsubscribes", "complaints"):
            try:
                opt_addrs |= {a.lower() for a in mailgun.suppressions(domain, kind)}
            except Exception:  # noqa: BLE001 — one list/domain failing must not block the others
                pass
        try:
            bounce_addrs = {a.lower() for a in mailgun.suppressions(domain, "bounces")}
        except Exception:  # noqa: BLE001
            bounce_addrs = set()
        opted = bounced = 0
        for a in opt_addrs:
            row = db.one("select newsletter_opt_out from crm_master where lower(email) = lower(%s)", (a,))
            if row and not row["newsletter_opt_out"]:
                crm.set_newsletter_opt_out(a, True)
                opted += 1
        for a in bounce_addrs:
            row = db.one("select newsletter_bounced from crm_master where lower(email) = lower(%s)", (a,))
            if row and not row["newsletter_bounced"]:
                crm.set_newsletter_bounced(a, True)
                bounced += 1
        out[domain] = {"opt_out_on_mailgun": len(opt_addrs), "opted_out": opted,
                       "bounces_on_mailgun": len(bounce_addrs), "bounced": bounced}
    return out


# ---------- throttled / drip sending ----------

SEND_BATCHES_PER_HOUR = 10        # one batch every ~6 minutes
DEFAULT_PER_HOUR = 250            # gentle default for a young domain (operator-chosen 2026-06-17)
_BOUNCE_PAUSE_FLOOR = 5           # ignore tiny bounce counts
_BOUNCE_PAUSE_PCT = 0.08          # auto-pause a job if >8% of what it has sent bounces


_JOBS_DDL = """
create table if not exists newsletter_send_jobs (
  id bigserial primary key, company_id bigint not null, task_id bigint,
  subject text not null, html text not null, body_text text not null, hero_b64 text,
  recipients jsonb not null, total int not null, sent int not null default 0,
  per_hour int not null default 250, status text not null default 'running',
  bounces_at_start int not null default 0, last_batch_at timestamptz,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now())
"""


def ensure_jobs_table() -> None:
    db.execute(_JOBS_DDL)


def enqueue_send(company_id: int, task_id: int, art: dict, recips: list[dict],
                 per_hour: int = DEFAULT_PER_HOUR) -> int:
    """Queue a full-list send to DRIP OUT over time instead of blasting. The engine drains it."""
    ensure_jobs_table()   # self-heal: works on any box / fresh DB without a manual migration step
    domain = SEND_DOMAINS.get(company_id)
    try:
        b0 = len(mailgun.suppressions(domain, "bounces"))   # baseline, to attribute NEW bounces to this job
    except Exception:  # noqa: BLE001
        b0 = 0
    row = db.execute(
        "insert into newsletter_send_jobs (company_id, task_id, subject, html, body_text, hero_b64, "
        "recipients, total, per_hour, bounces_at_start) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
        (company_id, task_id, art["subject"], art["html"], art["text"], art.get("hero_b64"),
         Json(recips), len(recips), int(per_hour), b0))
    return row["id"]


def drain_send_jobs() -> list[dict]:
    """Engine calls this each ~60s tick. Sends ONE throttled batch per due job (≈6-min cadence) and returns
    lifecycle events (done / paused) for the engine to alert + log. The `newsletter_paused` emergency stop
    halts all draining (in-flight sends pause until resumed)."""
    if db.setting_get("newsletter_paused"):
        return []
    events = []
    jobs = db.query("select * from newsletter_send_jobs where status='running' and "
                    "(last_batch_at is null or now() - last_batch_at > interval '6 minutes') order by id")
    for job in jobs:
        ev = _drain_one(job)
        if ev:
            events.append(ev)
    return events


def _drain_one(job: dict) -> dict | None:
    cid, jid = job["company_id"], job["id"]
    recips, sent, total = job["recipients"], job["sent"], job["total"]
    domain = SEND_DOMAINS.get(cid)
    if sent > 0:   # auto-pause on a bounce spike attributable to this job
        try:
            bnew = len(mailgun.suppressions(domain, "bounces")) - (job["bounces_at_start"] or 0)
            if bnew > max(_BOUNCE_PAUSE_FLOOR, sent * _BOUNCE_PAUSE_PCT):
                db.execute("update newsletter_send_jobs set status='paused', updated_at=now() where id=%s", (jid,))
                return {"status": "paused", "job_id": jid, "task_id": job["task_id"], "company_id": cid,
                        "subject": job["subject"], "sent": sent, "total": total, "bounces": bnew}
        except Exception:  # noqa: BLE001
            pass
    batch = max(1, round((job["per_hour"] or DEFAULT_PER_HOUR) / SEND_BATCHES_PER_HOUR))
    chunk = recips[sent:sent + batch]
    if not chunk:
        db.execute("update newsletter_send_jobs set status='done', updated_at=now() where id=%s", (jid,))
        return {"status": "done", "job_id": jid, "task_id": job["task_id"], "company_id": cid,
                "subject": job["subject"], "sent": sent, "total": total}
    hero = base64.b64decode(job["hero_b64"]) if job["hero_b64"] else None
    try:
        n = send_bulk(cid, job["subject"], job["html"], job["body_text"], chunk, hero, tag="newsletter")
    except Exception:  # noqa: BLE001 — transient; don't advance, retry next tick
        db.execute("update newsletter_send_jobs set last_batch_at=now(), updated_at=now() where id=%s", (jid,))
        return None
    newsent = sent + n
    done = newsent >= total
    db.execute("update newsletter_send_jobs set sent=%s, status=%s, last_batch_at=now(), updated_at=now() where id=%s",
               (newsent, "done" if done else "running", jid))
    if done:
        return {"status": "done", "job_id": jid, "task_id": job["task_id"], "company_id": cid,
                "subject": job["subject"], "sent": newsent, "total": total}
    return None


def brand_kit(company_id: int) -> dict | None:
    return brand.get_brand_kit(company_id)
