"""FilmSpoke (dark-cinematic) blog renderer — the editorial WEB equivalent of the newsletter's
render_filmspoke. compose (the editable Part-B craft -> rich JSON) -> Gemini imagery (hosted on R2, web
URLs not email cid) -> render a self-contained dark-cinematic post body for a WordPress draft.

Routing (engine `_run_blog_task`): a company whose brand kit `template` starts with "dark" uses this path;
everyone else keeps `worker.draft_article` ({title, html}). The editable guidance is the skill CRAFT; only
the structural JSON schema + the renderer live here (see [[feedback_logic_lives_in_skills]]).
"""
from __future__ import annotations

import html as _html
from concurrent.futures import ThreadPoolExecutor

from . import brand, imagegen, media, provider, store, worker
from .newsletter import _optimize_jpeg

# Structural output contract — the renderer parses these exact fields, so it stays in code.
_BLOG_SCHEMA = (
    "Return JSON only (set any optional block's \"use\" to false when not needed; most optional blocks are false):\n"
    "seo {title, meta_description (~155 chars), primary_keyword, slug (kebab-case)}; category (short kicker); "
    "title (the post H1, usually = seo.title); dek (one or two sentence standfirst, leads with the point); "
    "byline {author, role, date (ISO), read_time}; "
    "featured_image {use:true, image_prompt (on-brand, NO text/letters/numbers in image), alt, caption} (the hero, on every post); "
    "the answer block (use the ONE block the company craft names, same shape): "
    "key_takeaways {use, points:[2-4 plain lines, the extractable answer, payoff first]} "
    "OR in_brief {use, points:[2-3 plain lines, the extractable answer, payoff first]}; "
    "lead (opening paragraph, leads with the most important thing); "
    "sections (array of {heading (H2), body (1-3 short paragraphs, PLAIN text), "
    "figure {use, image_prompt, alt, caption}, callout {use, title, text}, "
    "table {use, columns:[...], rows:[[...]]}, steps {use, items:[{title, text}]}, "
    "code {use, filename, language, body}, stat {use, value, text}, "
    "inline_cta {use, text, label, url}}); "
    "signature_graphic {use, kind:\"control_dials\"|\"capability_dials\"|\"flight_plan\"|\"real_ai_split\", "
    "title (the graphic header), "
    "items:[{label, value (0-100 int, dials only), accent:\"orange\"|\"violet\" for one emphasised item}] "
    "(dials = 3-5 value rows; flight_plan = 3-5 ordered waypoints, label only), "
    "real_image_prompt, ai_image_prompt (real_ai_split, NO text in image), left_label, right_label (real_ai_split), "
    "caption} (AT MOST ONE per post, only when it earns its place, default use:false); "
    "pull_quote {use, text}; closing_cta {use, heading, text, primary {label, url}, secondary {label, url}}; "
    "author_bio (one short credible E-E-A-T bio); keep_reading (array of {title, url}). "
    "Body text is PLAIN text (no HTML, no markdown). No em-dashes or en-dashes. No FAQ / Q&A block."
)

_MAX_IMAGES = 4   # hero + up to 3 figures; bounds Gemini cost/latency


def concepts(company_id: int, brief: str, n: int = 1) -> list[dict]:
    """IDEATION stage — propose N blog CONCEPTS as plain readable text (a working title + a paragraph
    summary of the angle and main talking points). NO HTML, no full post: this is what the owner approves
    before anything is built. Returns [{title, summary}]."""
    company = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "content-blog-posts")
    n = max(1, min(int(n or 1), 10))
    system = "\n\n".join(filter(None, [
        f"You are proposing {n} blog post idea(s) for {company['name']}.",
        skill.get("craft") or "",          # the editable craft sets the voice/angle even at ideation
        worker._company_context(company),
        worker._rules_block(skill),
        store.examples_block(company_id, "blog"),
        (f"Propose EXACTLY {n} distinct, on-brand blog post concept(s). For EACH concept give: a specific, "
         "compelling working TITLE, and a SUMMARY of 3 to 5 sentences describing the angle and the main "
         "talking points the post will cover. Plain, readable prose only. NO HTML, NO markdown, NO headings, "
         "NO code, NO em-dashes or en-dashes. This is a concept for approval, NOT the post itself. "
         'Return JSON {"ideas":[{"title":"...","summary":"..."}]} with exactly ' + str(n) + " item(s)."),
    ]))
    out = provider.think_json(system, brief or "Propose strong, on-brand blog ideas.",
                              model=worker._model_for(skill), max_tokens=2000,
                              purpose="blog:ideate", company=company.get("slug"))
    ideas = [i for i in ((out or {}).get("ideas") or []) if i.get("title") and i.get("summary")]
    return ideas[:n]


def compose(company_id: int, brief: str) -> dict:
    company = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "content-blog-posts")
    system = "\n\n".join(filter(None, [
        f"You are composing ONE blog post for {company['name']}.",
        skill.get("craft") or "",          # the editable craft drives the writing
        worker._company_context(company),
        worker._rules_block(skill),
        store.examples_block(company_id, "blog"),   # distilled approved exemplars (what good looks like)
        _BLOG_SCHEMA,                       # structural output the renderer parses
    ]))
    out = provider.think_json(system, f"Brief: {brief}\n\nWrite the full post now as JSON.",
                              model=worker._model_for(skill), max_tokens=8000,
                              purpose="blog:filmspoke", company=company.get("slug"))
    return out or {}


def _gen_and_host(slug: str, jobs: list[tuple[str, str, str]]) -> dict:
    """jobs = [(key, prompt, aspect)] -> {key: public R2 url}. Gemini -> optimise JPEG -> R2."""
    if not jobs:
        return {}

    def run(job):
        k, prompt, aspect = job
        return k, imagegen.hero(prompt, aspect=aspect)

    urls: dict = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for k, data in ex.map(run, jobs):
            data = _optimize_jpeg(data, max_w=(1600 if k == "hero" else 1200))
            if data:
                urls[k] = media.put(slug, "blog", f"{k}.jpg", data, content_type="image/jpeg")
    return urls


# ---- render (self-contained dark-cinematic post body; works regardless of the WP theme) ----

def _esc(s) -> str:
    return _html.escape(str(s or ""))


def _paras(text: str, color: str) -> str:
    blocks = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    return "".join(f'<p style="margin:0 0 20px;color:{color};font-size:18px;line-height:1.8">'
                   f'{_esc(p)}</p>' for p in blocks)


_MONOF = "'JetBrains Mono',ui-monospace,'SFMono-Regular',Menlo,monospace"


def _sig_dials(sg: dict, accent: str, surface: str, line: str, muted: str, ink: str, emph: str) -> str:
    """The 'control / capability dials' signature graphic — pure CSS parameter sliders (label + value bar).
    Reads the accent from the kit (cyan for Sensa, yellow for SkyVision, red for FilmSpoke); an item flagged
    with an `accent` uses the kit's tertiary/emphasis colour (orange for Sensa, violet for SkyVision)."""
    rows = ""
    for it in (sg.get("items") or [])[:6]:
        try:
            v = max(0, min(100, int(it.get("value", 60))))
        except (TypeError, ValueError):
            v = 60
        bar = emph if (isinstance(it, dict) and it.get("accent")) else accent
        rows += (f'<div style="display:flex;align-items:center;gap:14px;margin:11px 0;font:400 11px/1 {_MONOF};'
                 f'color:{muted};letter-spacing:.06em">'
                 f'<span style="width:96px;flex:none;text-transform:uppercase;color:{ink}">{_esc(it.get("label"))}</span>'
                 f'<div style="flex:1;height:5px;border-radius:3px;background:#202020;overflow:hidden">'
                 f'<div style="height:100%;width:{v}%;background:{bar};box-shadow:0 0 8px {bar}"></div></div></div>')
    return (f'<div style="border:1px solid {line};border-radius:12px;background:{surface};padding:22px 24px;margin:30px 0">'
            f'<div style="font:500 11px/1 {_MONOF};letter-spacing:.14em;text-transform:uppercase;color:{accent};'
            f'margin-bottom:16px;display:flex;align-items:center;gap:9px">'
            f'<span style="width:7px;height:7px;border-radius:50%;background:{accent};box-shadow:0 0 8px {accent};'
            f'display:inline-block"></span>{_esc(sg.get("title") or "The dials")}</div>{rows}</div>')


def _sig_split(sg: dict, imgs: dict, accent: str, line: str) -> str:
    """The 'real / AI clip-split' signature graphic — two frames clipped on a diagonal with a glowing accent
    seam. Image-driven (build() generates sig_real / sig_ai); falls back to tonal panels when images absent."""
    real, ai = imgs.get("sig_real"), imgs.get("sig_ai")
    real_bg = (f"background-image:url({real});background-size:cover;background-position:center" if real
               else "background:linear-gradient(135deg,#1c1c1c,#0d0d0d)")
    ai_bg = (f"background-image:url({ai});background-size:cover;background-position:center" if ai
             else "background:linear-gradient(135deg,#0c2731,#0a1418)")
    ll, rl = _esc(sg.get("left_label") or "LIVE ACTION"), _esc(sg.get("right_label") or "AI")
    lab = ("position:absolute;top:16px;font:500 12px/1 " + _MONOF + ";letter-spacing:.14em;z-index:4;"
           "background:rgba(10,10,10,.5);padding:6px 10px;border-radius:5px")
    return (f'<div style="position:relative;border-radius:12px;overflow:hidden;aspect-ratio:16/9;background:#000;'
            f'border:1px solid {line};margin:30px 0">'
            f'<div style="position:absolute;inset:0;{real_bg};clip-path:polygon(0 0,56% 0,44% 100%,0 100%)"></div>'
            f'<div style="position:absolute;inset:0;{ai_bg};clip-path:polygon(56% 0,100% 0,100% 100%,44% 100%)"></div>'
            f'<div style="position:absolute;top:-10%;left:50%;width:2px;height:120%;background:{accent};'
            f'transform:rotate(7deg);box-shadow:0 0 18px {accent};z-index:3"></div>'
            f'<span style="{lab};left:16px;color:#fff">{ll}</span>'
            f'<span style="{lab};right:16px;color:{accent}">{rl}</span></div>')


def _sig_flight(sg: dict, accent: str, surface: str, line: str, muted: str, bg: str, violet: str) -> str:
    """The 'flight plan' signature graphic — a dashed accent track with numbered waypoint nodes (the last in
    the tertiary colour). SkyVision's production-process device; pure CSS, accent from the kit."""
    items = (sg.get("items") or [])[:6]
    n = len(items)
    nodes = ""
    for i, it in enumerate(items):
        label = it.get("label") if isinstance(it, dict) else str(it)
        dot = violet if (i == n - 1 and n > 1) else accent
        nodes += (f'<div style="flex:1;text-align:center;position:relative">'
                  f'<div style="width:14px;height:14px;border-radius:50%;background:{bg};border:2px solid {dot};'
                  f'box-shadow:0 0 10px {dot};margin:0 auto"></div>'
                  f'<div style="font:500 10px/1 {_MONOF};color:{accent};margin-top:11px;letter-spacing:.06em">'
                  f'{i + 1:02d}</div>'
                  f'<div style="margin:6px 0 0;font:400 12px/1.3 \'Inter\',-apple-system,Arial,sans-serif;'
                  f'color:{muted}">{_esc(label)}</div></div>')
    track = (f'<div style="position:relative;height:2px;margin:0 8px 0;background:repeating-linear-gradient('
             f'90deg,{accent} 0 8px,transparent 8px 16px)"></div>')
    return (f'<div style="border:1px solid {line};border-radius:12px;background:{surface};padding:30px 26px 24px;margin:30px 0">'
            f'<div style="font:500 11px/1 {_MONOF};letter-spacing:.14em;text-transform:uppercase;color:{accent};'
            f'margin-bottom:26px;display:flex;align-items:center;gap:9px">'
            f'<span style="width:7px;height:7px;border-radius:50%;background:{accent};box-shadow:0 0 8px {accent};'
            f'display:inline-block"></span>{_esc(sg.get("title") or "Flight plan")}</div>'
            f'{track}<div style="display:flex;justify-content:space-between;margin-top:-7px">{nodes}</div></div>')


def render(company_id: int, c: dict, imgs: dict) -> dict:
    kit = brand.get_brand_kit(company_id) or {}
    col = kit.get("colors") or {}
    bg = col.get("bg", "#0A0A0A"); surface = col.get("surface", "#121212"); line = col.get("line", "#242424")
    ink = col.get("ink", "#F4F4F5"); body = col.get("body", "#CFD0D5"); muted = col.get("muted", "#9A9AA0")
    red = col.get("primary", "#E50914")
    # text that sits ON the accent (buttons / filled discs): white suits a dark accent (FilmSpoke red),
    # a bright accent (Sensa cyan) needs dark ink. Kit opts in via accent_ink; default white.
    accent_ink = col.get("accent_ink", "#fff")
    # optional brand gradient on the primary CTA (e.g. Snap Rewards magenta->purple); solid fallback under it
    _grad = col.get("gradient")
    btn_bg = (f"background:{col.get('purple') or red};background-image:linear-gradient({_grad});"
              if _grad else f"background:{red};")
    headf = "'Poppins',-apple-system,'Segoe UI',Arial,sans-serif"
    bodyf = "'Inter',-apple-system,'Segoe UI',Arial,sans-serif"
    title = (c.get("seo") or {}).get("title") or c.get("title") or "Untitled"

    P = []
    P.append('<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">')
    P.append(f'<div style="background:{bg};color:{body};font-family:{bodyf};max-width:820px;margin:0 auto;'
             f'padding:8px 22px 48px;border-radius:14px">')

    # kicker
    if c.get("category"):
        P.append(f'<div style="color:{red};font-family:{headf};font-weight:700;font-size:12px;'
                 f'letter-spacing:.18em;text-transform:uppercase;margin:18px 0 14px">{_esc(c["category"])}</div>')
    # dek (standfirst)
    if c.get("dek"):
        P.append(f'<p style="color:{ink};font-family:{headf};font-weight:600;font-size:23px;line-height:1.5;'
                 f'margin:0 0 18px">{_esc(c["dek"])}</p>')
    # byline
    b = c.get("byline") or {}
    if b.get("author"):
        bits = " &nbsp;&middot;&nbsp; ".join(_esc(x) for x in
                                             [b.get("author"), b.get("role"), b.get("date"), b.get("read_time")] if x)
        P.append(f'<div style="color:{muted};font-size:13px;border-top:1px solid {line};'
                 f'border-bottom:1px solid {line};padding:12px 0;margin:0 0 26px">{bits}</div>')
    # hero
    if imgs.get("hero"):
        hero = c.get("featured_image") or c.get("hero") or {}
        P.append(f'<figure style="margin:0 0 28px"><img src="{imgs["hero"]}" alt="{_esc(hero.get("alt"))}" '
                 f'style="width:100%;height:auto;border-radius:12px;display:block">')
        if hero.get("caption"):
            P.append(f'<figcaption style="color:{muted};font-size:13px;margin-top:8px">{_esc(hero["caption"])}</figcaption>')
        P.append('</figure>')
    # the AEO answer block (accent left rule). The company's craft picks the block: key_takeaways -> the
    # "Key takeaways" label, in_brief -> "In brief"; each takes points[] (bullets) or text (one paragraph).
    for blk, lbl in ((c.get("key_takeaways") or {}, "Key takeaways"), (c.get("in_brief") or {}, "In brief")):
        if not blk.get("use"):
            continue
        pts = blk.get("points") or []
        if pts:
            items = "".join(f'<li style="color:{ink};font-size:17px;line-height:1.7;margin:0 0 8px">{_esc(p)}</li>'
                            for p in pts)
            inner = f'<ul style="margin:0;padding-left:18px">{items}</ul>'
        elif blk.get("text"):
            inner = f'<div style="color:{ink};font-size:18px;line-height:1.7">{_esc(blk["text"])}</div>'
        else:
            continue
        P.append(f'<div style="border-left:2px solid {red};padding:4px 0 4px 18px;margin:0 0 28px">'
                 f'<div style="color:{red};font-family:{headf};font-weight:700;font-size:12px;letter-spacing:.14em;'
                 f'text-transform:uppercase;margin-bottom:8px">{lbl}</div>{inner}</div>')
        break   # only one answer block per post
    # lead
    if c.get("lead"):
        P.append(_paras(c["lead"], body))
    # sections
    for i, s in enumerate(c.get("sections") or []):
        if s.get("heading"):
            P.append(f'<h2 style="color:{ink};font-family:{headf};font-weight:700;font-size:27px;'
                     f'line-height:1.25;margin:36px 0 14px">{_esc(s["heading"])}</h2>')
        if s.get("body"):
            P.append(_paras(s["body"], body))
        cal = s.get("callout") or {}
        if cal.get("use") and (cal.get("text") or cal.get("title")):
            P.append(f'<div style="background:{surface};border:1px solid {line};border-left:3px solid {red};'
                     f'border-radius:10px;padding:16px 18px;margin:0 0 24px">'
                     + (f'<div style="color:{ink};font-family:{headf};font-weight:700;font-size:15px;'
                        f'margin-bottom:5px">{_esc(cal.get("title"))}</div>' if cal.get("title") else "")
                     + f'<div style="color:{body};font-size:16px;line-height:1.65">{_esc(cal.get("text"))}</div></div>')
        stp = s.get("steps") or {}
        if stp.get("use") and stp.get("items"):
            P.append('<div style="margin:0 0 24px">')
            for n, it in enumerate(stp["items"], 1):
                P.append(f'<div style="display:flex;gap:12px;margin:0 0 12px"><div style="flex:none;width:28px;'
                         f'height:28px;border-radius:999px;background:{red};color:{accent_ink};font-family:{headf};'
                         f'font-weight:700;font-size:14px;line-height:28px;text-align:center">{n}</div>'
                         f'<div><div style="color:{ink};font-family:{headf};font-weight:700;font-size:16px">'
                         f'{_esc(it.get("title"))}</div><div style="color:{body};font-size:16px;line-height:1.6">'
                         f'{_esc(it.get("text"))}</div></div></div>')
            P.append('</div>')
        cd = s.get("code") or {}
        if cd.get("use") and cd.get("body"):
            P.append(f'<div style="background:#0A1828;border-radius:10px;margin:0 0 24px;overflow:hidden">'
                     f'<div style="color:#8494A6;font-family:monospace;font-size:12px;padding:10px 16px;'
                     f'border-bottom:1px solid #1b2b3d">{_esc(cd.get("filename") or cd.get("language") or "code")}</div>'
                     f'<pre style="margin:0;padding:16px;color:#D6E2F0;font-family:monospace;font-size:13px;'
                     f'line-height:1.6;overflow-x:auto;white-space:pre-wrap">{_esc(cd["body"])}</pre></div>')
        tb = s.get("table") or {}
        if tb.get("use") and tb.get("rows"):
            thead = "".join(f'<th style="text-align:left;padding:10px 14px;color:{muted};font-family:{headf};'
                            f'font-size:13px;border-bottom:1px solid {line}">{_esc(col)}</th>'
                            for col in (tb.get("columns") or []))
            trows = "".join("<tr>" + "".join(f'<td style="padding:10px 14px;color:{body};font-size:15px;'
                            f'border-bottom:1px solid {line}">{_esc(cell)}</td>' for cell in row) + "</tr>"
                            for row in tb["rows"])
            P.append(f'<table style="width:100%;border-collapse:collapse;margin:0 0 24px;background:{surface};'
                     f'border:1px solid {line};border-radius:10px;overflow:hidden">'
                     + (f"<thead><tr>{thead}</tr></thead>" if thead else "") + f"<tbody>{trows}</tbody></table>")
        sta = s.get("stat") or {}
        if sta.get("use") and sta.get("value"):
            P.append(f'<div style="text-align:center;margin:0 0 24px;padding:18px;background:{surface};'
                     f'border:1px solid {line};border-radius:12px"><div style="color:{red};font-family:{headf};'
                     f'font-weight:800;font-size:40px;line-height:1">{_esc(sta["value"])}</div>'
                     f'<div style="color:{muted};font-size:14px;margin-top:6px">{_esc(sta.get("text"))}</div></div>')
        fig = s.get("figure") or {}
        if fig.get("use") and imgs.get(f"fig{i}"):
            P.append(f'<figure style="margin:24px 0"><img src="{imgs[f"fig{i}"]}" alt="{_esc(fig.get("alt"))}" '
                     f'style="width:100%;height:auto;border-radius:12px;display:block">')
            if fig.get("caption"):
                P.append(f'<figcaption style="color:{muted};font-size:13px;margin-top:8px">{_esc(fig["caption"])}</figcaption>')
            P.append('</figure>')
        cta = s.get("inline_cta") or {}
        if cta.get("use") and cta.get("url"):
            P.append(f'<p style="margin:6px 0 24px;color:{body};font-size:18px">{_esc(cta.get("text"))} '
                     f'<a href="{_esc(cta["url"])}" style="color:{red};font-weight:600;text-decoration:none">'
                     f'{_esc(cta.get("label") or "Learn more")} &rarr;</a></p>')
    # signature graphic (at most one per post): a brand device. control_dials = pure CSS;
    # real_ai_split = two clipped frames. Accent comes from the kit (cyan for Sensa, red for FilmSpoke).
    sg = c.get("signature_graphic") or {}
    if sg.get("use"):
        tertiary = col.get("tertiary", "#FF6A2C")
        kind = sg.get("kind")
        gfx = ""
        if kind in ("control_dials", "capability_dials"):
            gfx = _sig_dials(sg, red, surface, line, muted, ink, tertiary)
        elif kind == "flight_plan":
            gfx = _sig_flight(sg, red, surface, line, muted, bg, tertiary)
        elif kind == "real_ai_split":
            gfx = _sig_split(sg, imgs, red, line)
        if gfx:
            P.append(gfx)
            if sg.get("caption"):
                P.append(f'<p style="color:{muted};font-size:13px;margin:-14px 0 24px">{_esc(sg["caption"])}</p>')

    # pull quote
    pq = c.get("pull_quote") or {}
    if pq.get("use") and pq.get("text"):
        P.append(f'<blockquote style="border-left:3px solid {red};margin:34px 0;padding:6px 0 6px 22px;'
                 f'color:{ink};font-family:{headf};font-weight:600;font-size:24px;line-height:1.45">'
                 f'{_esc(pq["text"])}</blockquote>')
    # closing CTA card
    cc = c.get("closing_cta") or {}
    if cc.get("use") and (cc.get("heading") or cc.get("primary")):
        pr = cc.get("primary") or {}; se = cc.get("secondary") or {}
        btns = ""
        if pr.get("url"):
            btns += (f'<a href="{_esc(pr["url"])}" style="display:inline-block;{btn_bg}color:{accent_ink};'
                     f'font-weight:700;font-size:15px;text-decoration:none;padding:13px 22px;border-radius:6px;'
                     f'margin:4px 10px 4px 0">{_esc(pr.get("label") or "Get started")}</a>')
        if se.get("url"):
            btns += (f'<a href="{_esc(se["url"])}" style="display:inline-block;border:1.5px solid {red};'
                     f'color:{red};font-weight:700;font-size:15px;text-decoration:none;padding:12px 22px;'
                     f'border-radius:6px;margin:4px 0">{_esc(se.get("label") or "Learn more")}</a>')
        P.append(f'<div style="background:{surface};border:1px solid {line};border-radius:14px;padding:26px;'
                 f'margin:38px 0">')
        if cc.get("heading"):
            P.append(f'<div style="color:{ink};font-family:{headf};font-weight:700;font-size:21px;'
                     f'margin-bottom:8px">{_esc(cc["heading"])}</div>')
        if cc.get("text"):
            P.append(f'<p style="color:{body};font-size:17px;line-height:1.7;margin:0 0 16px">{_esc(cc["text"])}</p>')
        P.append(btns + '</div>')
    # author bio (E-E-A-T)
    if c.get("author_bio"):
        who = (c.get("byline") or {}).get("author") or ""
        P.append(f'<div style="background:{surface};border:1px solid {line};border-radius:14px;padding:20px;'
                 f'margin:30px 0 0;color:{muted};font-size:14px;line-height:1.65">'
                 f'{("<b style=color:"+ink+">"+_esc(who)+"</b><br>") if who else ""}{_esc(c["author_bio"])}</div>')
    # keep reading
    kr = c.get("keep_reading") or []
    if kr:
        P.append(f'<div style="border-top:1px solid {line};margin-top:30px;padding-top:18px">'
                 f'<div style="color:{muted};font-family:{headf};font-weight:700;font-size:12px;'
                 f'letter-spacing:.14em;text-transform:uppercase;margin-bottom:10px">Keep reading</div>')
        for k in kr:
            if k.get("url"):
                P.append(f'<a href="{_esc(k["url"])}" style="display:block;color:{ink};text-decoration:none;'
                         f'font-size:16px;margin:0 0 8px">{_esc(k.get("title") or k["url"])} '
                         f'<span style="color:{red}">&rarr;</span></a>')
        P.append('</div>')

    P.append('</div>')
    return {"title": title, "html": "".join(P), "dek": (c.get("dek") or "").strip()}


def build(company_id: int, brief: str) -> dict:
    """Compose -> generate + host imagery on R2 -> render. Returns {title, html} for stage_draft."""
    company = store.get_company(company_id)
    c = compose(company_id, brief)
    jobs: list[tuple[str, str, str]] = []
    hero = c.get("featured_image") or c.get("hero") or {}
    if hero.get("image_prompt") and hero.get("use", True):   # featured_image is mandatory (use defaults True)
        jobs.append(("hero", hero["image_prompt"], "16:9"))
    for i, s in enumerate(c.get("sections") or []):
        fig = s.get("figure") or {}
        if fig.get("use") and fig.get("image_prompt") and len(jobs) < _MAX_IMAGES:
            jobs.append((f"fig{i}", fig["image_prompt"], "16:9"))
    # the real/AI split signature graphic needs its two frames generated (the dials are pure CSS)
    sg = c.get("signature_graphic") or {}
    if sg.get("use") and sg.get("kind") == "real_ai_split":
        if sg.get("real_image_prompt"):
            jobs.append(("sig_real", sg["real_image_prompt"], "16:9"))
        if sg.get("ai_image_prompt"):
            jobs.append(("sig_ai", sg["ai_image_prompt"], "16:9"))
    imgs = _gen_and_host(company.get("slug") or "filmspoke", jobs)
    return render(company_id, c, imgs)
