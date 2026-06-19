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
from concurrent.futures import ThreadPoolExecutor

from psycopg.types.json import Json

from . import brand, db, imagegen, mailgun, provider, store, worker

# Seed map of each company's verified Mailgun newsletter sending domain. This is now a FALLBACK only:
# the live value is data-driven (company_profiles.data['send_domain'], set at onboarding) so a new company
# wires its own sending without a code change. Read via send_domain(); never read SEND_DOMAINS directly.
SEND_DOMAINS = {1: "news.tabscanner.com", 3: "news.sensa.digital", 4: "news.skyvision.film",
                5: "news.filmspoke.ai", 26: "campaigns.snap-rewards.com"}


def send_domain(company_id: int) -> str | None:
    """The company's verified newsletter sending domain — data-driven (company_profiles.data['send_domain']),
    falling back to the seed map so existing companies keep working before any backfill."""
    try:
        d = (_profile(company_id).get("send_domain") or "").strip()
    except Exception:  # noqa: BLE001
        d = ""
    return d or SEND_DOMAINS.get(company_id)


def all_send_domains() -> set[str]:
    """Every configured sending domain across all companies (data + seed map) — for suppression sync."""
    out = {d for d in SEND_DOMAINS.values() if d}
    for r in db.query("select data from company_profiles"):
        d = ((r.get("data") or {}).get("send_domain") or "").strip()
        if d:
            out.add(d)
    return out

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

# EDITABLE writing brief (default/fallback) for the LIGHT-card path — the live copy lives in each company's
# `content-newsletter` skill CRAFT, which drives drafting. Edit the SKILL, not this constant.
_LIGHT_GUIDE = (
    "Compose the newsletter issue from the approved idea, in the company voice. Lead with the point, keep it "
    "tight and value-first, one clear primary message. No em-dashes or en-dashes; no emoji; no clickbait. "
    "The issue MUST make full sense with images off."
)
# STRUCTURAL output schema (light card) — the renderer parses these exact fields, so it stays in code.
_LIGHT_SCHEMA = (
    "Return a JSON object with EXACTLY these fields: "
    "subject (compelling, specific, never clickbait), preheader (preview text, ~80 chars), "
    "headline (the in-email H1), intro (1-2 short sentences), sections (array of 2-4 objects, each "
    "{heading, body} where body is 1-3 short plain-text paragraphs separated by a blank line), "
    "cta_label (short button text), cta_url (a real, on-brand absolute URL for this company), "
    "hero_prompt (a short Imagen prompt for a clean, product-led, on-brand hero image with NO text in it)."
)


def compose(company_id: int, idea_text: str) -> dict:
    company = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "content-newsletter")
    system = "\n\n".join(filter(None, [
        f"You are composing ONE newsletter issue for {company['name']}.",
        skill.get("craft") or _LIGHT_GUIDE,       # the editable skill craft drives the writing
        worker._company_context(company),
        worker._rules_block(skill),
        _LIGHT_SCHEMA,                            # structural output the renderer parses — stays in code
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
    _root = (send_domain(company_id) or "").split(".", 1)   # news.tabscanner.com -> tabscanner.com
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


# ---------- FilmSpoke dark-cinematic template (baked from the approved newsletter guide) ----------
# A brand kit whose `template` starts with "dark" routes here. The look/voice/flow are firm; the block
# mix is composed per issue (guide, not straitjacket). Images are generated via Gemini and inlined as cid
# attachments alongside the real on-dark logo (kept in the brand kit as base64).

# EDITABLE writing brief (voice / look / guidance) — this constant is only the DEFAULT/fallback. The live
# copy lives in the FilmSpoke `content-newsletter` skill CRAFT, which is what actually drives drafting. To
# change how FilmSpoke newsletters are written, edit the SKILL, not this code. See docs/COMPANY-STANDARD.md.
_FS_GUIDE = (
    "You are FilmSpoke's newsletter writer and designer. FilmSpoke is an AI commercial store: broadcast-grade "
    "commercials, customised in clicks, delivered in under 24 hours, built by an award-winning team. An AI "
    "production studio by Sensa.\n\n"
    "Compose ONE newsletter issue that maps onto the FilmSpoke dark cinematic template (all-black canvas, "
    "accent red #E50914 used deliberately as the only loud colour, Poppins headings + Inter body, 600px, "
    "email-safe). This template is a GUIDE, not a straitjacket: hold the look, voice and rough flow firm, "
    "but compose the blocks freely for what the issue actually has to say. Voice: fast, confident, premium; "
    "short declarative sentences; one clear point; lead with it. No em-dashes or en-dashes; no emoji; no "
    "clickbait. The issue MUST read fully with images off.\n\n"
    "Choose blocks by substance, do not force them. Required: headline, intro, and one primary CTA. "
    "Everything else is optional, use what fits. Scale content sections to the number of real items "
    "(around three or four reads best; more only if each earns its place, otherwise group or link out). "
    "Each image you want needs a short prompt for a cinematic, dark, high-contrast frame, mostly black and "
    "shadow, with red as a single RESTRAINED accent (a rim light or a subtle glow, never a large red fill "
    "or a big glowing red screen) and NO text in the image, plus alt text."
)
# STRUCTURAL output schema — the renderer parses these exact fields, so it stays in code (NOT editable).
_FS_SCHEMA = (
    "Return JSON only with these fields (set any optional block's \"use\" to false when not needed):\n"
    "subject; preheader (~80 chars); header_eyebrow (short issue type); "
    "hero {use, image_prompt, alt}; eyebrow_pill (short red kicker or null); headline; intro; "
    "primary_cta {label, url}; format_chips (array, e.g. 30s/9:16/16:9, or empty); "
    "sections (array of {heading, body, image:{use, kind:\"feature\"|\"grid\", "
    "items:[{image_prompt, alt, caption}]}}); steps {use, title, items:[{title, text}]}; "
    "stat_band {use, stats:[{value, label, highlight}]}; quote {use, text, attribution}; "
    "closing_cta {use, heading, subtext, label, url}. Use real absolute FilmSpoke URLs (https://filmspoke.ai...)."
)
_FS_COMPOSE = _FS_GUIDE + "\n\n" + _FS_SCHEMA   # back-compat alias


def compose_filmspoke(company_id: int, idea_text: str) -> dict:
    company = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "content-newsletter")
    system = "\n\n".join(filter(None, [
        f"You are composing ONE newsletter issue for {company['name']}.",
        skill.get("craft") or _FS_GUIDE,          # the editable skill craft drives the writing
        worker._company_context(company),
        worker._rules_block(skill),
        _FS_SCHEMA,                               # structural output the renderer parses — stays in code
    ]))
    user = f"Approved idea:\n{idea_text}\n\nCompose the full issue now as JSON."
    out = provider.think_json(system, user, model=worker._model_for(skill), max_tokens=2600,
                              purpose="newsletter_compose", company=company.get("slug"))
    return out or {}


_FS_MAX_IMAGES = 5   # excluding the logo; bounds Gemini cost + latency per issue


def _gen_images(jobs: list[tuple[str, str, str]]) -> dict:
    """jobs = [(key, prompt, aspect)]. Generate concurrently; return {key: bytes|None}."""
    if not jobs:
        return {}

    def run(job):
        k, prompt, aspect = job
        return k, imagegen.hero(prompt, aspect=aspect)

    out: dict = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for k, b in ex.map(run, jobs):
            out[k] = b
    return out


def _optimize_jpeg(data: bytes | None, max_w: int, q: int = 82) -> bytes | None:
    """Downsize + recompress a generated image to a small, email-friendly JPEG. Best-effort; on any
    failure (or no Pillow) returns the original bytes so a send is never blocked on optimisation."""
    if not data:
        return data
    try:
        from io import BytesIO

        from PIL import Image
        im = Image.open(BytesIO(data)).convert("RGB")
        if im.width > max_w:
            im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return data


def _build_filmspoke(company_id: int, idea_text: str, kit: dict) -> dict:
    company = store.get_company(company_id)
    c = compose_filmspoke(company_id, idea_text)

    jobs: list[tuple[str, str, str]] = []
    hero = c.get("hero") or {}
    if hero.get("use") and hero.get("image_prompt"):
        jobs.append(("hero", hero["image_prompt"], "16:9"))
    for i, s in enumerate(c.get("sections") or []):
        img = s.get("image") or {}
        if img.get("use"):
            aspect = "1:1" if (img.get("kind") == "grid") else "16:9"
            for j, item in enumerate(img.get("items") or []):
                if item.get("image_prompt"):
                    jobs.append((f"s{i}_{j}", item["image_prompt"], aspect))
    gen = _gen_images(jobs[:_FS_MAX_IMAGES])

    images: list[tuple[str, bytes]] = []
    logo_b64 = kit.get("logo_dark_b64")
    if logo_b64:
        images.append(("logo.png", base64.b64decode(logo_b64)))
    if gen.get("hero"):
        images.append(("hero.jpg", _optimize_jpeg(gen["hero"], 1200)))
        c.setdefault("hero", {})["cid"] = "hero.jpg"
    elif c.get("hero"):
        c["hero"]["use"] = False
    for i, s in enumerate(c.get("sections") or []):
        img = s.get("image") or {}
        if not img.get("use"):
            continue
        max_w = 600 if img.get("kind") == "grid" else 1000
        kept = []
        for j, item in enumerate(img.get("items") or []):
            b = gen.get(f"s{i}_{j}")
            if b:
                cid = f"s{i}_{j}.jpg"
                images.append((cid, _optimize_jpeg(b, max_w)))
                item["cid"] = cid
                kept.append(item)
        img["items"] = kept
        img["use"] = bool(kept)

    return {"subject": c.get("subject") or f"{company['name']} newsletter",
            "html": render_filmspoke(company_id, c, "logo.png" if logo_b64 else None),
            "text": render_text_filmspoke(company_id, c), "images": images, "content": c}


def render_filmspoke(company_id: int, c: dict, logo_cid: str | None) -> str:
    kit = brand.get_brand_kit(company_id) or {}
    col = kit.get("colors") or {}
    bg = col.get("bg", "#0A0A0A"); surface = col.get("surface", "#121212"); line = col.get("line", "#242424")
    ink = col.get("ink", "#F4F4F5"); body = col.get("body", "#C9CAD0"); muted = col.get("muted", "#9A9AA0")
    red = col.get("primary", "#E50914")
    # text colour that sits ON the accent (buttons / filled discs). White is right for a dark accent like
    # FilmSpoke red; a bright accent like Sensa cyan needs dark ink for contrast. Kit opts in via accent_ink.
    accent_ink = col.get("accent_ink", "#FFFFFF")
    head_family = kit.get("fonts", {}).get("heading", "Poppins")
    body_family = kit.get("fonts", {}).get("body", "Inter")
    headf = f"'{head_family}','Helvetica Neue',Helvetica,Arial,sans-serif"
    bodyf = f"'{body_family}','Helvetica Neue',Helvetica,Arial,sans-serif"
    # light vs dark chrome follows the brand template, so the email matches the company look
    _tmpl = str(kit.get("template") or "")
    scheme = "light" if _tmpl == "light-saas" else "dark"
    _fams = []
    for _f, _w in [(head_family, "600;700;800"), (body_family, "400;500;600;700")]:
        _fams.append(f"family={_f.replace(' ', '+')}:wght@{_w}")
    font_link = "https://fonts.googleapis.com/css2?" + "&".join(_fams) + "&display=swap"
    company = store.get_company(company_id)
    prof = _profile(company_id)

    def para(text, color=None, size=16):
        color = color or body
        ps = [x.strip() for x in str(text or "").split("\n\n") if x.strip()]
        return "".join(f'<p style="margin:0 0 14px;font:400 {size}px/1.65 {bodyf};color:{color};">{_esc(x)}</p>'
                       for x in ps)

    def btn(label, url):
        return (f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
                f'<td style="border-radius:6px;background:{red};">'
                f'<a href="{_esc(url)}" style="display:inline-block;font:700 16px/1 {headf};color:{accent_ink};'
                f'padding:16px 32px;border-radius:6px;background:{red};text-decoration:none;">{_esc(label)} &nbsp;&rarr;</a>'
                f'</td></tr></table>')

    def divider():
        return (f'<tr><td style="padding:30px 44px 0;"><div style="border-top:1px solid {line};'
                f'font-size:0;line-height:0;">&nbsp;</div></td></tr>')

    rows = []
    eyebrow = _esc(c.get("header_eyebrow") or company["name"])
    if logo_cid:
        logo_html = (f'<img src="cid:{logo_cid}" alt="{_esc(company["name"])}" height="26" '
                     f'style="display:block;height:26px;width:auto;border:0;">')
    else:
        logo_html = (f'<span style="font:800 22px/1 {headf};color:{ink};">{_esc(company["name"])}</span>')
    rows.append(f'<tr><td style="padding:24px 36px 18px;"><table role="presentation" width="100%" '
                f'cellpadding="0" cellspacing="0"><tr><td align="left" style="vertical-align:middle;">{logo_html}</td>'
                f'<td align="right" style="vertical-align:middle;font:600 11px/1 {bodyf};letter-spacing:.22em;'
                f'color:{muted};text-transform:uppercase;">{eyebrow}</td></tr></table></td></tr>')

    hero = c.get("hero") or {}
    if hero.get("use") and hero.get("cid"):
        rows.append(f'<tr><td style="padding:0;"><img src="cid:{hero["cid"]}" width="600" '
                    f'alt="{_esc(hero.get("alt"))}" style="display:block;width:100%;max-width:600px;'
                    f'height:auto;border:0;"></td></tr>')

    if c.get("eyebrow_pill"):
        rows.append(f'<tr><td style="padding:28px 44px 0;"><span style="display:inline-block;'
                    f'font:700 11px/1 {bodyf};letter-spacing:.2em;text-transform:uppercase;color:{red};'
                    f'border:1px solid {red};border-radius:999px;padding:7px 13px;">{_esc(c["eyebrow_pill"])}</span></td></tr>')

    if c.get("headline"):
        rows.append(f'<tr><td style="padding:16px 44px 0;"><h1 style="margin:0;font:800 36px/1.14 {headf};'
                    f'color:{ink};letter-spacing:-.5px;">{_esc(c["headline"])}</h1></td></tr>')
    if c.get("intro"):
        rows.append(f'<tr><td style="padding:16px 44px 0;">{para(c.get("intro"))}</td></tr>')

    cta = c.get("primary_cta") or {}
    if cta.get("label") and cta.get("url"):
        rows.append(f'<tr><td style="padding:24px 44px 0;">{btn(cta["label"], cta["url"])}</td></tr>')

    chips = "".join(f'<span style="display:inline-block;font:600 12px/1 {bodyf};color:{muted};'
                    f'border:1px solid {line};border-radius:999px;padding:7px 12px;margin:0 6px 6px 0;">{_esc(ch)}</span>'
                    for ch in (c.get("format_chips") or []))
    if chips:
        rows.append(f'<tr><td style="padding:18px 44px 0;">{chips}</td></tr>')

    secs = c.get("sections") or []
    if secs:
        rows.append(divider())
    for s in secs:
        rows.append(f'<tr><td style="padding:26px 44px 0;"><h2 style="margin:0 0 10px;font:700 22px/1.25 {headf};'
                    f'color:{ink};">{_esc(s.get("heading"))}</h2>{para(s.get("body"))}</td></tr>')
        img = s.get("image") or {}
        if img.get("use") and img.get("items"):
            if img.get("kind") == "grid":
                items = img["items"]
                w = round(100 / max(1, len(items)), 2)
                cells = ""
                for it in items:
                    cap = (f'<div style="font:600 12px/1 {bodyf};color:{body};padding:9px 2px 0;">'
                           f'{_esc(it.get("caption"))}</div>') if it.get("caption") else ""
                    cells += (f'<td width="{w}%" style="vertical-align:top;padding:0 5px;">'
                              f'<img src="cid:{it["cid"]}" width="168" alt="{_esc(it.get("alt"))}" '
                              f'style="display:block;width:100%;height:auto;border-radius:12px;border:0;">{cap}</td>')
                rows.append(f'<tr><td style="padding:18px 40px 0;"><table role="presentation" width="100%" '
                            f'cellpadding="0" cellspacing="0"><tr>{cells}</tr></table></td></tr>')
            else:
                it = img["items"][0]
                rows.append(f'<tr><td style="padding:16px 44px 0;"><img src="cid:{it["cid"]}" width="512" '
                            f'alt="{_esc(it.get("alt"))}" style="display:block;width:100%;height:auto;'
                            f'border-radius:12px;border:0;"></td></tr>')

    st = c.get("steps") or {}
    if st.get("use") and st.get("items"):
        rows.append(divider())
        rows.append(f'<tr><td style="padding:26px 44px 0;"><h2 style="margin:0 0 4px;font:700 22px/1.25 {headf};'
                    f'color:{ink};">{_esc(st.get("title") or "How it works")}</h2></td></tr>')
        items = st["items"]; last = len(items) - 1
        trs = ""
        for idx, it in enumerate(items):
            islast = idx == last
            disc = (f'background:{red};color:{accent_ink};' if islast else f'border:1px solid {red};color:{red};')
            pad = "0" if islast else "0 0 16px"
            trs += (f'<tr><td width="44" style="vertical-align:top;padding:{pad};"><div style="width:34px;'
                    f'height:34px;border-radius:999px;{disc}text-align:center;font:700 15px/34px {headf};">{idx + 1}</div></td>'
                    f'<td style="vertical-align:top;padding:{pad};"><div style="font:700 16px/1.3 {headf};'
                    f'color:{ink};">{_esc(it.get("title"))}</div><div style="font:400 14px/1.55 {bodyf};'
                    f'color:{muted};padding-top:3px;">{_esc(it.get("text"))}</div></td></tr>')
        rows.append(f'<tr><td style="padding:14px 44px 0;"><table role="presentation" width="100%" '
                    f'cellpadding="0" cellspacing="0">{trs}</table></td></tr>')

    sb = c.get("stat_band") or {}
    if sb.get("use") and sb.get("stats"):
        stats = sb["stats"][:3]; n = len(stats); w = round(100 / max(1, n), 2)
        cells = ""
        for k, stt in enumerate(stats):
            br = f'border-right:1px solid {line};' if k < n - 1 else ""
            vcol = red if stt.get("highlight") else ink
            cells += (f'<td width="{w}%" align="center" style="padding:22px 8px;{br}">'
                      f'<div style="font:800 24px/1 {headf};color:{vcol};">{_esc(stt.get("value"))}</div>'
                      f'<div style="font:500 12px/1.4 {bodyf};color:{muted};padding-top:6px;">{_esc(stt.get("label"))}</div></td>')
        rows.append(f'<tr><td style="padding:30px 40px 0;"><table role="presentation" width="100%" '
                    f'cellpadding="0" cellspacing="0" style="background:{surface};border:1px solid {line};'
                    f'border-radius:14px;"><tr>{cells}</tr></table></td></tr>')

    q = c.get("quote") or {}
    if q.get("use") and q.get("text"):
        attr = (f'<p style="margin:8px 0 0;font:400 13px/1.4 {bodyf};color:{muted};">{_esc(q.get("attribution"))}</p>'
                if q.get("attribution") else "")
        rows.append(f'<tr><td style="padding:28px 44px 0;"><div style="font:800 30px/1 {headf};color:{red};">&ldquo;</div>'
                    f'<p style="margin:2px 0 0;font:500 18px/1.5 {headf};color:{ink};">{_esc(q.get("text"))}</p>{attr}</td></tr>')

    cc = c.get("closing_cta") or {}
    if cc.get("use") and cc.get("label") and cc.get("url"):
        rows.append(f'<tr><td style="padding:30px 40px 0;"><table role="presentation" width="100%" '
                    f'cellpadding="0" cellspacing="0" style="border-radius:16px;background:{surface};'
                    f'border:1px solid {line};"><tr><td align="center" style="padding:34px 30px 36px;">'
                    f'<h2 style="margin:0;font:800 26px/1.2 {headf};color:{ink};">{_esc(cc.get("heading"))}</h2>'
                    f'<p style="margin:12px 0 22px;font:400 15px/1.6 {bodyf};color:{muted};">{_esc(cc.get("subtext"))}</p>'
                    f'{btn(cc["label"], cc["url"])}</td></tr></table></td></tr>')

    legal = _esc(prof.get("legal_name") or company["name"])
    addr = _esc(worker._no_dashes(" ".join((prof.get("address") or "").split())).rstrip(". "))
    _root = (send_domain(company_id) or "").split(".", 1)
    sitetext = _root[1] if len(_root) == 2 else ""
    site = ("https://" + sitetext) if sitetext else ""
    social = prof.get("social") or ""
    flogo = (f'<img src="cid:{logo_cid}" alt="{_esc(company["name"])}" height="20" '
             f'style="display:block;height:20px;width:auto;border:0;">' if logo_cid else
             f'<span style="font:800 16px/1 {headf};color:{ink};">{_esc(company["name"])}</span>')
    _tagline = prof.get("tagline") or prof.get("strapline") or ""
    _tag_html = (f'<p style="margin:14px 0 0;font:600 10px/1.5 {bodyf};color:{muted};'
                 f'letter-spacing:.22em;text-transform:uppercase;">{_esc(_tagline)}</p>' if _tagline else "")
    rows.append(f'<tr><td style="padding:34px 44px 8px;">{flogo}{_tag_html}</td></tr>')
    links = ""
    if site:
        links += f'<a href="{site}" style="color:{muted};text-decoration:underline;">{_esc(sitetext)}</a> &nbsp;&middot;&nbsp; '
    if social:
        links += f'<a href="{_esc(social)}" style="color:{muted};text-decoration:underline;">YouTube</a> &nbsp;&middot;&nbsp; '
    rows.append(f'<tr><td style="padding:14px 44px 30px;"><p style="margin:0 0 6px;font:400 12px/1.6 {bodyf};'
                f'color:#6F7078;">You are receiving this email from {legal}{(", " + addr) if addr else ""}.</p>'
                f'<p style="margin:0;font:400 12px/1.6 {bodyf};color:#6F7078;">{links}'
                f'<a href="%unsubscribe_url%" style="color:{muted};text-decoration:underline;">Unsubscribe</a></p></td></tr>')

    preheader = _esc(c.get("preheader") or "")
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="x-apple-disable-message-reformatting">'
            f'<meta name="color-scheme" content="{scheme}"><meta name="supported-color-schemes" content="{scheme}">'
            '<link rel="preconnect" href="https://fonts.googleapis.com">'
            f'<link href="{font_link}" rel="stylesheet"></head>'
            f'<body style="margin:0;padding:0;background:{bg};">'
            f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{bg};">'
            f'<tr><td align="center" style="padding:24px 12px;">'
            f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;'
            f'max-width:600px;background:{bg};border:1px solid {line};border-radius:18px;overflow:hidden;">'
            f'{"".join(rows)}</table></td></tr></table></body></html>')


def render_text_filmspoke(company_id: int, c: dict) -> str:
    company = store.get_company(company_id)
    prof = _profile(company_id)
    L = [c.get("headline") or company["name"], "", (c.get("intro") or "").strip(), ""]
    cta = c.get("primary_cta") or {}
    if cta.get("label") and cta.get("url"):
        L += [f"{cta['label']}: {cta['url']}", ""]
    for s in (c.get("sections") or []):
        L += [str(s.get("heading") or "").upper(), (s.get("body") or "").strip(), ""]
    st = c.get("steps") or {}
    if st.get("use") and st.get("items"):
        L += [str(st.get("title") or "How it works").upper()]
        for i, it in enumerate(st["items"]):
            L += [f"{i + 1}. {it.get('title')}: {it.get('text')}"]
        L += [""]
    sb = c.get("stat_band") or {}
    if sb.get("use") and sb.get("stats"):
        L += [" / ".join(f"{x.get('value')} {x.get('label')}" for x in sb["stats"]), ""]
    q = c.get("quote") or {}
    if q.get("use") and q.get("text"):
        L += [f"\"{q.get('text')}\" {q.get('attribution') or ''}".strip(), ""]
    cc = c.get("closing_cta") or {}
    if cc.get("use") and cc.get("label") and cc.get("url"):
        L += [f"{cc['label']}: {cc['url']}", ""]
    L += ["-" * 40, f"You are receiving this email from {prof.get('legal_name') or company['name']}.",
          prof.get("address") or "", "Unsubscribe: %unsubscribe_url%"]
    return worker._no_dashes("\n".join(x for x in L if x is not None))


def build(company_id: int, idea_text: str) -> dict:
    """Compose + render one issue. Dispatches on the brand kit's `template`: a 'dark*' template (FilmSpoke)
    uses the dark cinematic renderer with multiple inline images; everything else uses the light card."""
    kit = brand.get_brand_kit(company_id) or {}
    _tmpl = str(kit.get("template") or "")
    if _tmpl.startswith("dark") or _tmpl == "light-saas":   # rich, brand-kit-driven renderer (dark OR light)
        return _build_filmspoke(company_id, idea_text, kit)
    c = compose(company_id, idea_text)
    hero = imagegen.hero(c.get("hero_prompt") or "") if c.get("hero_prompt") else None
    images = [("hero.jpg", hero)] if hero else []
    return {"subject": c.get("subject") or f"{store.get_company(company_id)['name']} newsletter",
            "html": render_html(company_id, c, hero_cid="hero.jpg" if hero else None),
            "text": render_text(company_id, c), "images": images, "content": c}


# ---------- send ----------

def _sender(company_id: int) -> tuple[str, str, str | None]:
    company = store.get_company(company_id)
    domain = send_domain(company_id)
    prof = _profile(company_id)
    return domain, f"{company['name']} <news@{domain}>", prof.get("reply_from") or prof.get("inbox_email")


def send_bulk(company_id: int, subject: str, html: str, text: str, recips: list[dict],
              images: list[tuple[str, bytes]] | None, tag: str) -> int:
    domain, sender, reply_to = _sender(company_id)
    inline = [(cid, b) for cid, b in (images or []) if b] or None
    sent = 0
    for i in range(0, len(recips), 900):
        chunk = recips[i:i + 900]
        rvars = {r["email"]: {"first_name": r.get("first_name") or ""} for r in chunk}
        mailgun.send(domain, sender, [r["email"] for r in chunk], subject, html, text,
                     inline=inline, recipient_vars=rvars, reply_to=reply_to, tag=tag)
        sent += len(chunk)
    return sent


def _decode_images(images_b64, hero_b64=None) -> list[tuple[str, bytes]]:
    """Artifact/job image list -> [(cid, bytes)]. Falls back to a legacy single hero_b64 (pre-multi-image)."""
    out: list[tuple[str, bytes]] = []
    for pair in (images_b64 or []):
        try:
            cid, b64 = pair
            out.append((cid, base64.b64decode(b64)))
        except Exception:  # noqa: BLE001
            pass
    if not out and hero_b64:
        out.append(("hero.jpg", base64.b64decode(hero_b64)))
    return out


# ---------- approval handlers (called from engine._execute) ----------

def execute_idea_approval(task: dict, skill: dict, company: dict, actor: str) -> dict:
    """Approve a newsletter IDEA -> build the issue, send to the TEST GROUP, drop a review card."""
    cid = company["id"]
    if not send_domain(cid):
        store.update_task(task["id"], status="done")
        return {"error": f"no sending domain configured for {company['name']}"}
    group = test_group(cid)
    if not group:
        store.update_task(task["id"], status="done")
        return {"error": "no test group configured for this company"}
    built = build(cid, task.get("draft") or "")
    send_bulk(cid, "[TEST] " + built["subject"], built["html"], built["text"],
              [{"email": g["email"], "first_name": g.get("name")} for g in group],
              built["images"], tag="newsletter-test")
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
        "images_b64": [[cid, base64.b64encode(b).decode()] for cid, b in built["images"]]})
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
    images = _decode_images(art.get("images_b64"), art.get("hero_b64"))
    sent = send_bulk(cid, art["subject"], art["html"], art["text"], recips, images, tag="newsletter")
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
    for domain in all_send_domains():
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
  subject text not null, html text not null, body_text text not null, hero_b64 text, images_b64 jsonb,
  recipients jsonb not null, total int not null, sent int not null default 0,
  per_hour int not null default 250, status text not null default 'running',
  bounces_at_start int not null default 0, last_batch_at timestamptz,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now())
"""


def ensure_jobs_table() -> None:
    db.execute(_JOBS_DDL)
    db.execute("alter table newsletter_send_jobs add column if not exists images_b64 jsonb")


def enqueue_send(company_id: int, task_id: int, art: dict, recips: list[dict],
                 per_hour: int = DEFAULT_PER_HOUR) -> int:
    """Queue a full-list send to DRIP OUT over time instead of blasting. The engine drains it."""
    ensure_jobs_table()   # self-heal: works on any box / fresh DB without a manual migration step
    domain = send_domain(company_id)
    try:
        b0 = len(mailgun.suppressions(domain, "bounces"))   # baseline, to attribute NEW bounces to this job
    except Exception:  # noqa: BLE001
        b0 = 0
    row = db.execute(
        "insert into newsletter_send_jobs (company_id, task_id, subject, html, body_text, hero_b64, "
        "images_b64, recipients, total, per_hour, bounces_at_start) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
        (company_id, task_id, art["subject"], art["html"], art["text"], art.get("hero_b64"),
         Json(art.get("images_b64") or []), Json(recips), len(recips), int(per_hour), b0))
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
    domain = send_domain(cid)
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
    images = _decode_images(job.get("images_b64"), job.get("hero_b64"))
    try:
        n = send_bulk(cid, job["subject"], job["html"], job["body_text"], chunk, images, tag="newsletter")
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
