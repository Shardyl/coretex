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
    "Return JSON only (set any optional block's \"use\" to false when the post does not need it):\n"
    "seo {title, meta_description (~155 chars), primary_keyword, slug (kebab-case)}; category (short kicker); "
    "title (the post H1, usually = seo.title); dek (one or two sentence standfirst, leads with the point); "
    "byline {author, role, date (ISO), read_time (e.g. '5 min read')}; "
    "hero {use, image_prompt (cinematic dark, red as the only accent, NO text in image), alt, caption}; "
    "in_brief {use, text (2-3 plain sentences, the extractable answer, payoff first)}; "
    "lead (opening paragraph, leads with the most important thing); "
    "sections (array of {heading (H2), body (1-3 short paragraphs separated by a blank line), "
    "inline_cta {use, text, label, url}, figure {use, image_prompt, alt, caption}}); "
    "pull_quote {use, text}; closing_cta {use, heading, text, primary {label, url}, secondary {label, url}}; "
    "author_bio (one short credible E-E-A-T bio); keep_reading (array of {title, url}). "
    "Body text is PLAIN text (no HTML, no markdown). No em-dashes or en-dashes anywhere."
)

_MAX_IMAGES = 4   # hero + up to 3 figures; bounds Gemini cost/latency


def compose(company_id: int, brief: str) -> dict:
    company = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "content-blog-posts")
    system = "\n\n".join(filter(None, [
        f"You are composing ONE blog post for {company['name']}.",
        skill.get("craft") or "",          # the editable craft drives the writing
        worker._company_context(company),
        worker._rules_block(skill),
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


def render(company_id: int, c: dict, imgs: dict) -> dict:
    kit = brand.get_brand_kit(company_id) or {}
    col = kit.get("colors") or {}
    bg = col.get("bg", "#0A0A0A"); surface = col.get("surface", "#121212"); line = col.get("line", "#242424")
    ink = col.get("ink", "#F4F4F5"); body = col.get("body", "#CFD0D5"); muted = col.get("muted", "#9A9AA0")
    red = col.get("primary", "#E50914")
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
        hero = c.get("hero") or {}
        P.append(f'<figure style="margin:0 0 28px"><img src="{imgs["hero"]}" alt="{_esc(hero.get("alt"))}" '
                 f'style="width:100%;height:auto;border-radius:12px;display:block">')
        if hero.get("caption"):
            P.append(f'<figcaption style="color:{muted};font-size:13px;margin-top:8px">{_esc(hero["caption"])}</figcaption>')
        P.append('</figure>')
    # in brief (the AEO answer — 2px red left rule)
    ib = c.get("in_brief") or {}
    if ib.get("use") and ib.get("text"):
        P.append(f'<div style="border-left:2px solid {red};padding:4px 0 4px 18px;margin:0 0 28px">'
                 f'<div style="color:{red};font-family:{headf};font-weight:700;font-size:12px;'
                 f'letter-spacing:.14em;text-transform:uppercase;margin-bottom:8px">In brief</div>'
                 f'<div style="color:{ink};font-size:18px;line-height:1.7">{_esc(ib["text"])}</div></div>')
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
            btns += (f'<a href="{_esc(pr["url"])}" style="display:inline-block;background:{red};color:#fff;'
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
    return {"title": title, "html": "".join(P)}


def build(company_id: int, brief: str) -> dict:
    """Compose -> generate + host imagery on R2 -> render. Returns {title, html} for stage_draft."""
    company = store.get_company(company_id)
    c = compose(company_id, brief)
    jobs: list[tuple[str, str, str]] = []
    hero = c.get("hero") or {}
    if hero.get("use") and hero.get("image_prompt"):
        jobs.append(("hero", hero["image_prompt"], "16:9"))
    for i, s in enumerate(c.get("sections") or []):
        fig = s.get("figure") or {}
        if fig.get("use") and fig.get("image_prompt") and len(jobs) < _MAX_IMAGES:
            jobs.append((f"fig{i}", fig["image_prompt"], "16:9"))
    imgs = _gen_and_host(company.get("slug") or "filmspoke", jobs)
    return render(company_id, c, imgs)
