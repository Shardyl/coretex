"""FilmSpoke (dark-cinematic) blog renderer — the editorial WEB equivalent of the newsletter's
render_filmspoke. compose (the editable Part-B craft -> rich JSON) -> Gemini imagery (hosted on R2, web
URLs not email cid) -> render a self-contained dark-cinematic post body for a WordPress draft.

Routing (engine `_run_blog_task`): a company whose brand kit `template` starts with "dark" uses this path;
everyone else keeps `worker.draft_article` ({title, html}). The editable guidance is the skill CRAFT; only
the structural JSON schema + the renderer live here (see [[feedback_logic_lives_in_skills]]).
"""
from __future__ import annotations

import html as _html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import brand, imagegen, media, profile, provider, store, worker
from .newsletter import _optimize_jpeg

_DUBAI = timezone(timedelta(hours=4))   # Cortex TZ (GST, no DST)


def _today_iso() -> str:
    return datetime.now(_DUBAI).date().isoformat()


def _read_time(c: dict) -> str:
    """~200 wpm over the post body. Computed in code, never trusted to the model."""
    parts = [c.get("title", ""), c.get("dek", ""), c.get("lead", "")]
    for s in (c.get("sections") or []):
        parts += [s.get("heading", ""), s.get("body", "")]
    for blk in ("key_takeaways", "in_brief"):
        parts += (c.get(blk) or {}).get("points") or []
    if (c.get("pull_quote") or {}).get("text"):
        parts.append(c["pull_quote"]["text"])
    words = sum(len(str(p).split()) for p in parts)
    return f"{max(1, round(words / 200))} min read"


def _stamp_byline(c: dict) -> dict:
    """Date + read-time are STRUCTURAL plumbing, set in code. The LLM used to invent the date (e.g. a
    hallucinated 2025-07-05); it no longer supplies either field."""
    b = dict(c.get("byline") or {})
    b["date"] = _today_iso()
    b["read_time"] = _read_time(c)
    c["byline"] = b
    return c

# Structural output contract — the renderer parses these exact fields, so it stays in code.
_BLOG_SCHEMA = (
    "Return JSON only (set any optional block's \"use\" to false when not needed; most optional blocks are false):\n"
    "seo {title, meta_description (~155 chars), primary_keyword, slug (kebab-case)}; category (short kicker); "
    "title (the post H1, usually = seo.title); dek (one or two sentence standfirst, leads with the point); "
    "byline {author, role}; "
    "featured_image {use:true, image_prompt (on-brand, NO text/letters/numbers in image), alt, caption} (the hero, on every post); "
    "the answer block (use the ONE block the company craft names, same shape): "
    "key_takeaways {use, points:[2-4 plain lines, the extractable answer, payoff first]} "
    "OR in_brief {use, points:[2-3 plain lines, the extractable answer, payoff first]}; "
    "lead (opening paragraph, leads with the most important thing); "
    "drop_cap (true to open the lead with a large decorative first letter, an editorial touch); "
    "sections (array of {heading (H2), body (1-3 short paragraphs, PLAIN text), "
    "figure {use, image_prompt, alt, caption}, callout {use, title, text}, "
    "table {use, columns:[...], rows:[[...]]}, steps {use, items:[{title, text}]}, "
    "code {use, filename, language, body}, stat {use, value, text}, "
    "video {use, youtube_url, vertical (true for a 9:16 short), caption}, "
    "inline_cta {use, text, label, url}}); "
    "signature_graphic {use, kind:\"custom\"|\"control_dials\"|\"capability_dials\"|\"flight_plan\"|\"real_ai_split\", "
    "title (the graphic header), "
    "html (for kind=custom ONLY: a self-contained ORIGINAL on-brand graphic you HAND-CODE for THIS post using "
    "INLINE-STYLED HTML elements only. EVERY style goes in an inline style=\"...\" attribute ON ITS OWN element "
    "(CSS gradients, borders, flex or positioned layout, the brand colours and fonts). Do NOT use CSS class names "
    "(class=\"...\") or a <style> block, do NOT use <svg>, @keyframes/animation, <script>, event handlers or "
    "external resources: WordPress strips all of those and the graphic collapses to plain text. Keep it ONE block "
    "on a single line, no line breaks), "
    "items:[{label, value (0-100 int, dials only), accent:\"orange\"|\"violet\" for one emphasised item}] "
    "(dials = 3-5 value rows; flight_plan = 3-5 ordered waypoints, label only), "
    "real_image_prompt, ai_image_prompt (real_ai_split, NO text in image), left_label, right_label (real_ai_split), "
    "caption} (AT MOST ONE per post, only when it earns its place, default use:false); "
    "pull_quote {use, text}; closing_cta {use, heading, text, primary {label, url}, secondary {label, url}}; "
    "author_bio (one short credible E-E-A-T bio). "
    "(Do NOT output keep_reading or any related-post links, titles or URLs: those are filled from the site's "
    "REAL published posts in code, never invented.) "
    "Body text is PLAIN text (no HTML, no markdown). No em-dashes or en-dashes. No FAQ / Q&A block."
)

_MAX_IMAGES = 4   # hero + up to 3 figures; bounds Gemini cost/latency


def content_text(c: dict) -> str:
    """Render the composed post as clean readable PLAIN TEXT for the Inbox card — the FULL content to read
    and revise BEFORE any formatting or images are built. No HTML, no markup beyond simple # / ## markers."""
    L: list[str] = []
    if c.get("category"):
        L.append(str(c["category"]).upper())
    if c.get("title"):
        L.append("# " + str(c["title"]))
    if c.get("dek"):
        L.append(str(c["dek"]))
    b = c.get("byline") or {}
    if b.get("author"):
        L.append("By " + str(b["author"]) + (", " + str(b["role"]) if b.get("role") else ""))
    ans = c.get("key_takeaways") or c.get("in_brief") or {}
    if ans.get("points"):
        L.append("KEY TAKEAWAYS\n" + "\n".join("- " + str(p) for p in ans["points"]))
    elif ans.get("text"):
        L.append("IN BRIEF: " + str(ans["text"]))
    if c.get("lead"):
        L.append(str(c["lead"]))
    for s in c.get("sections") or []:
        if s.get("heading"):
            L.append("## " + str(s["heading"]))
        if s.get("body"):
            L.append(str(s["body"]))
        cal = s.get("callout") or {}
        if cal.get("use") and cal.get("text"):
            L.append("[" + str(cal.get("title") or "Note") + "] " + str(cal["text"]))
        stp = s.get("steps") or {}
        if stp.get("use") and stp.get("items"):
            L.append("\n".join(f"{i + 1}. {it.get('title', '')}: {it.get('text', '')}"
                               for i, it in enumerate(stp["items"])))
        tb = s.get("table") or {}
        if tb.get("use") and tb.get("rows"):
            L.append(" | ".join(str(x) for x in (tb.get("columns") or []))
                     + "\n" + "\n".join(" | ".join(str(x) for x in row) for row in tb["rows"]))
        sta = s.get("stat") or {}
        if sta.get("use") and sta.get("value"):
            L.append(str(sta["value"]) + " — " + str(sta.get("text") or ""))
    pq = c.get("pull_quote") or {}
    if pq.get("use") and pq.get("text"):
        L.append('"' + str(pq["text"]) + '"')
    cc = c.get("closing_cta") or {}
    if cc.get("use") and cc.get("heading"):
        L.append("CTA: " + str(cc["heading"]) + (("\n" + str(cc["text"])) if cc.get("text") else ""))
    il = c.get("internal_links") or []
    if il:
        L.append("INTERNAL LINKS:\n" + "\n".join(f'- "{l.get("anchor")}" -> {l.get("url")}' for l in il))
    ab = c.get("author_bio")
    if isinstance(ab, dict):                      # author_bio can be a structured object — show only its prose
        ab = ab.get("bio") or ab.get("text") or ""
    if ab:
        L.append("Bio: " + str(ab))
    return "\n\n".join(x for x in L if x)


def _linkable_pages(company: dict, limit: int = 40) -> list:
    """The company's EXISTING published posts + key pages (title + real URL) that a new post can link to."""
    from .integrations import wordpress as wp
    site = wp.for_company(company)
    if not site:
        return []
    out, seen = [], set()
    for typ in ("posts", "pages"):
        try:
            rows = site._req("GET", f"/{typ}?status=publish&per_page={limit}&_fields=link,title")
        except Exception:  # noqa: BLE001
            rows = []
        for r in (rows or []):
            url = r.get("link")
            t = re.sub(r"<[^>]+>", "", ((r.get("title") or {}).get("rendered") or "")).strip()
            if url and t and url not in seen:
                seen.add(url)
                out.append({"title": t, "url": url})
    return out


def add_internal_links(company_id: int, content: dict) -> dict:
    """OUTBOUND internal linking (draft-time): weave 1-3 contextual links from THIS post's body to the most
    relevant EXISTING pages on the company's site. URLs come ONLY from the real published list (never invented);
    the anchor must already appear verbatim in the body. Reads the content-internal-linking skill for guidance.
    Stores them on content['internal_links']; the renderer turns them into in-body links. Never raises."""
    from . import store, provider
    company = store.get_company(company_id)
    if not company:
        return content
    pages = _linkable_pages(company)
    sections = content.get("sections") or []
    body_txt = "\n\n".join(f"[{i}] {s.get('body', '')}" for i, s in enumerate(sections) if s.get("body"))
    if not pages or not body_txt.strip():
        return content
    skill = store.get_skill_by_key(company_id, "content-internal-linking")
    rules = "\n".join("- " + r for r in (skill.get("rules") or [])) if skill else ""
    pages_txt = "\n".join(f"- {p['title']} -> {p['url']}" for p in pages[:30])
    system = (
        f"You add INTERNAL links to a {company.get('name')} blog post body. Choose 1 to 3 of the EXISTING pages "
        "that are genuinely relevant to this post; for each pick a SHORT phrase that appears VERBATIM in that "
        "section's body to use as the anchor. Descriptive anchors (the natural phrase), never 'click here'; at most "
        "one link per section; only link where it truly helps the reader. Never invent URLs or anchors. "
        + (rules + "\n\n" if rules else "")
        + "Return JSON {\"links\":[{\"section\":<int>,\"anchor\":\"exact phrase from that section\",\"url\":\"one listed URL\"}]}.")
    user = f"EXISTING PAGES (use these URLs only):\n{pages_txt}\n\nPOST BODY (by section index):\n{body_txt}"
    try:
        out = provider.think_json(system, user, model=provider.MODEL_FAST, max_tokens=500,
                                  purpose="internal_links", company=company.get("slug"))
    except Exception:  # noqa: BLE001 — linking must never break the draft
        return content
    valid = {p["url"] for p in pages}
    links, used = [], set()
    for l in (out.get("links") or []):
        si, anchor, url = l.get("section"), (l.get("anchor") or "").strip(), l.get("url")
        if (url in valid and url not in used and isinstance(si, int) and 0 <= si < len(sections)
                and anchor and anchor in (sections[si].get("body") or "")):
            used.add(url)
            links.append({"section": si, "anchor": anchor, "url": url})
    content["internal_links"] = links[:3]
    return content


def seed_inbound_links(company: dict, new_post_id: int, new_url: str, new_title: str,
                       max_links: int = 3, dry_run: bool = False) -> list:
    """INBOUND internal linking (runs AFTER the post is published): find the most relevant EXISTING published
    posts and add a contextual link FROM each TO the new post. Edits each existing post in place (first verbatim
    occurrence of a real anchor phrase; skips one that already links to the new URL). Reads the
    content-internal-linking skill. Returns a report [{id,title,anchor}] of what was linked. dry_run computes the
    report WITHOUT editing. Never raises."""
    from .integrations import wordpress as wp
    from . import store, provider
    site = wp.for_company(company)
    if not site or not new_url:
        return []
    try:
        posts = site._req("GET", "/posts?status=publish&per_page=40&_fields=id,title,content")
    except Exception:  # noqa: BLE001
        return []
    cands = [p for p in (posts or []) if p.get("id") != new_post_id]
    if not cands:
        return []
    skill = store.get_skill_by_key(company["id"], "content-internal-linking")
    rules = "\n".join("- " + r for r in (skill.get("rules") or [])) if skill else ""
    blocks = []
    for p in cands[:30]:
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", (p.get("content") or {}).get("rendered") or "")).strip()
        title = re.sub(r"<[^>]+>", "", (p.get("title") or {}).get("rendered") or "").strip()
        blocks.append(f"[{p['id']}] {title}\n{txt[:700]}")
    system = (
        f"A new {company.get('name')} blog post was just published: \"{new_title}\". From the EXISTING posts below, "
        f"choose the 1 to {max_links} MOST RELEVANT that should link TO the new post; for each pick a SHORT phrase "
        "that appears VERBATIM in that post's text to use as the anchor (a natural lead-in to the new post's topic). "
        "Descriptive anchors, never generic, one per post, only where genuinely relevant. "
        + (rules + " " if rules else "")
        + "Return JSON {\"links\":[{\"post_id\":<int>,\"anchor\":\"verbatim phrase from that post\"}]}.")
    try:
        out = provider.think_json(system, "\n\n".join(blocks), model=provider.MODEL_FAST, max_tokens=500,
                                  purpose="inbound_links", company=company.get("slug"))
    except Exception:  # noqa: BLE001
        return []
    report = []
    for l in (out.get("links") or [])[:max_links]:
        pid, anchor = l.get("post_id"), (l.get("anchor") or "").strip()
        if not pid or not anchor:
            continue
        try:
            edit = site.get(pid)                       # context=edit -> content.raw + title.raw
            raw = (edit.get("content") or {}).get("raw") or ""
            title = (edit.get("title") or {}).get("raw") or ""
        except Exception:  # noqa: BLE001
            continue
        if not raw or new_url in raw:                  # post already links to the new one -> skip
            continue
        ea = _esc(anchor)
        target = ea if ea in raw else (anchor if anchor in raw else None)
        if not target:
            continue
        if not dry_run:
            newraw = raw.replace(target, f'<a href="{_esc(new_url)}" style="color:inherit;text-decoration:underline">'
                                         f'{target}</a>', 1)
            try:
                site._req("POST", f"/posts/{pid}", json={"content": newraw})
            except Exception:  # noqa: BLE001
                continue
        report.append({"id": pid, "title": title, "anchor": anchor})
    return report


def add_service_logos(company_id: int, content: dict) -> dict:
    """Detect EXTERNAL third-party services/products/websites genuinely mentioned by name in the post, and attach
    each one's brand logo (Google favicon CDN, reliably hotlinkable) + its website. The renderer shows a
    'Tools & services mentioned' row of logos, each linking out. Excludes the company itself. Never raises."""
    from . import store, provider
    company = store.get_company(company_id)
    if not company:
        return content
    sections = content.get("sections") or []
    body = (content.get("lead") or "") + "\n" + "\n".join(
        ((s.get("heading") or "") + " " + (s.get("body") or "")) for s in sections)
    if not body.strip():
        return content
    system = (
        f"From this {company.get('name')} blog post, list the EXTERNAL third-party services, products, tools or "
        "websites that are genuinely mentioned BY NAME (e.g. Stripe, OpenAI, Shopify, QuickBooks, Xero). For each, "
        f"give its display name and its official ROOT domain (e.g. stripe.com). EXCLUDE {company.get('name')} "
        "itself, generic terms, and anything that is not a real named product/company. If none are mentioned, "
        "return an empty list. Return JSON {\"services\":[{\"name\":\"...\",\"domain\":\"example.com\"}]}.")
    try:
        out = provider.think_json(system, body[:4000], model=provider.MODEL_FAST, max_tokens=400,
                                  purpose="service_logos", company=company.get("slug"))
    except Exception:  # noqa: BLE001 — enrichment must never break the draft
        return content
    own = (company.get("slug") or "").lower()
    seen, svc = set(), []
    for s in (out.get("services") or [])[:6]:
        name = (s.get("name") or "").strip()
        dom = (s.get("domain") or "").strip().lower().replace("https://", "").replace("http://", "").strip("/").split("/")[0]
        if not name or not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", dom) or dom in seen or own in dom:
            continue
        seen.add(dom)
        svc.append({"name": name, "website": f"https://{dom}",
                    "logo": f"https://www.google.com/s2/favicons?domain={dom}&sz=128"})
    content["service_logos"] = svc
    return content


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
    return _stamp_byline(out or {})


def _gen_and_host(slug: str, jobs: list[tuple[str, str, str]], prefix: str = "") -> dict:
    """jobs = [(key, prompt, aspect)] -> {key: public R2 url}. Gemini -> optimise JPEG -> R2. `prefix` (the post
    slug) makes the R2 filename unique PER POST, so two posts of the same company never overwrite each other's
    hero/figures at a shared `<slug>/blog/published/hero.jpg`."""
    if not jobs:
        return {}

    def run(job):
        k, prompt, aspect = job
        return k, imagegen.hero(prompt, aspect=aspect, purpose="image:blog", company=slug)

    pre = re.sub(r"[^a-z0-9-]", "", (prefix or "").lower())[:60]
    urls: dict = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for k, data in ex.map(run, jobs):
            data = _optimize_jpeg(data, max_w=(1600 if k == "hero" else 1200))
            if data:
                name = f"{pre}-{k}.jpg" if pre else f"{k}.jpg"
                urls[k] = media.put(slug, "blog", name, data, content_type="image/jpeg")
    return urls


# ---- render (self-contained dark-cinematic post body; works regardless of the WP theme) ----

def _esc(s) -> str:
    return _html.escape(str(s or ""))


def _paras(text: str, color: str) -> str:
    blocks = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    return "".join(f'<p style="margin:0 0 20px;color:{color};font-size:18px;line-height:1.8">'
                   f'{_esc(p)}</p>' for p in blocks)


def _paras_linked(text: str, color: str, links: list, accent: str) -> str:
    """Like _paras, but weaves in contextual internal links: for each {anchor,url}, the FIRST verbatim occurrence
    of the anchor phrase becomes a link. Matched on the ESCAPED text so the rest of the body stays safe."""
    blocks = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    out = []
    for p in blocks:
        ep = _esc(p)
        for l in links:
            a = _esc((l.get("anchor") or "").strip())
            if a and a in ep:
                href = _esc(l.get("url") or "")
                ep = ep.replace(a, f'<a href="{href}" style="color:{accent};text-decoration:none;'
                                   f'border-bottom:1px solid {accent}55">{a}</a>', 1)
        out.append(f'<p style="margin:0 0 20px;color:{color};font-size:18px;line-height:1.8">{ep}</p>')
    return "".join(out)


def _sanitize_graphic_html(html: str) -> str:
    """A HAND-AUTHORED, on-brand graphic built from INLINE-STYLED HTML (divs/spans) — the writer designs it per
    post, like a web page. We STRIP <svg> and <style> outright: WordPress's wpautop injects </p>/<br> inside
    inline SVG and style blocks and corrupts them (black gap + spilled text), so those formats can't be used in
    post content. Also strip scripts, event handlers, javascript:, @import, expression(), breakout tags, and
    collapse newlines so wpautop has nothing to act on."""
    if not html:
        return ""
    h = html
    bad = r"svg|style|script|iframe|object|embed|link|meta|base|form|input|button|textarea|select"
    h = re.sub(rf"(?is)<({bad})\b.*?</\1\s*>", "", h)     # paired tags + their content (incl. <svg>..</svg>, <style>..)
    h = re.sub(rf"(?is)<({bad})\b[^>]*/?>", "", h)        # self-closing / unclosed ones
    h = re.sub(r'(?is)\son\w+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', "", h)   # on* event handlers
    h = re.sub(r"(?i)javascript:", "", h)
    h = re.sub(r"(?i)@import\b[^;]*;?", "", h)
    h = re.sub(r"(?i)expression\s*\(", "(", h)
    h = re.sub(r"\s*\n\s*", " ", h)                       # one line -> wpautop can't inject <p>/<br>
    # WordPress strips <style>, so a graphic styled with CSS CLASSES loses all styling and renders as plain text.
    # If what remains leans on classes with almost no inline styles, drop it (better no graphic than broken text).
    if h.count("class=") >= 2 and h.count("style=") < 3:
        return ""
    return h.strip()


def _yt_id(url: str) -> str:
    m = re.search(r"(?:youtube\.com/(?:shorts/|watch\?v=|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{6,})", url or "")
    return m.group(1) if m else ""


def _video_embed(url: str, vertical: bool, caption: str, muted: str) -> str:
    """Responsive YouTube embed. `vertical` for 9:16 shorts (centred, narrower); else 16:9 full width."""
    vid = _yt_id(url)
    if not vid:
        return ""
    pad = "177.78%" if vertical else "56.25%"
    maxw = "340px" if vertical else "100%"
    cap = (f'<figcaption style="color:{muted};font-size:13px;margin-top:8px;text-align:center">{_esc(caption)}'
           "</figcaption>") if caption else ""
    return (f'<figure style="margin:26px auto;max-width:{maxw}"><div style="position:relative;padding-top:{pad};'
            'border-radius:12px;overflow:hidden;background:#000">'
            f'<iframe src="https://www.youtube.com/embed/{vid}" style="position:absolute;inset:0;width:100%;'
            'height:100%;border:0" loading="lazy" allow="accelerometer;autoplay;clipboard-write;encrypted-media;'
            'gyroscope;picture-in-picture" allowfullscreen></iframe></div>' + cap + "</figure>")


def _paras_dropcap(text: str, color: str, accent: str, headf: str) -> str:
    """Like _paras but opens with a large decorative drop-cap on the lead's first letter (editorial touch).
    Inline-styled (floated span) so it works without a stylesheet, like the rest of the self-contained body."""
    blocks = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    if not blocks:
        return ""
    first = blocks[0]
    cap, rest = (first[:1], first[1:]) if first else ("", "")
    dc = (f'<span style="float:left;font-family:{headf};font-size:60px;line-height:0.8;font-weight:800;'
          f'color:{accent};margin:8px 11px -2px 0">{_esc(cap)}</span>')
    out = [f'<p style="margin:0 0 20px;color:{color};font-size:18px;line-height:1.8">{dc}{_esc(rest)}</p>']
    out += [f'<p style="margin:0 0 20px;color:{color};font-size:18px;line-height:1.8">{_esc(p)}</p>'
            for p in blocks[1:]]
    return "".join(out)


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


def _apply_cta_links(prof: dict, c: dict) -> dict:
    """Code-stamp blog CTA button URLs to the company's REAL destinations by classifying each label/text — so CTA
    links are never LLM-invented. Reads prof['cta_links'] e.g. {'demo': url, 'get_started': url}; safe no-op
    without it. e.g. Snap Rewards: a 'Book a demo' button -> the contact form, 'Get started free' -> Shopify."""
    cfg = (prof or {}).get("cta_links") or {}
    if not cfg:
        return c
    demo_url = cfg.get("demo") or cfg.get("contact")
    start_url = cfg.get("get_started") or cfg.get("shopify") or cfg.get("install")

    def route(label, text=""):
        s = (str(label or "") + " " + str(text or "")).lower()
        if demo_url and re.search(r"demo|contact|talk|call|get in touch|speak|enquir|inquir", s):
            return demo_url
        if start_url and re.search(r"start|install|free|try|sign ?up|launch|download|get going|add to", s):
            return start_url
        return None

    cc = c.get("closing_cta") or {}
    for key in ("primary", "secondary"):
        b = cc.get(key)
        if isinstance(b, dict) and b.get("label"):
            u = route(b.get("label"), cc.get("text") or cc.get("heading"))
            if u:
                b["url"] = u
    for s in c.get("sections") or []:
        ic = s.get("inline_cta") if isinstance(s, dict) else None
        if isinstance(ic, dict) and ic.get("use"):
            u = route(ic.get("label"), ic.get("text"))
            if u:
                ic["url"] = u
    return c


def render(company_id: int, c: dict, imgs: dict) -> dict:
    kit = brand.get_brand_kit(company_id) or {}
    prof = profile.get(company_id) or {}
    c = _apply_cta_links(prof, c)   # CTA buttons -> the company's real destinations (code-stamped, never LLM-chosen)
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
    # byline — prefer the company's designated REAL blog author (name + role + LinkedIn), code-set so it is
    # NEVER LLM-invented; the name links to LinkedIn at the top (E-E-A-T).
    ba = prof.get("blog_author") or {}
    _ab = c.get("author_bio") if isinstance(c.get("author_bio"), dict) else {}
    b = c.get("byline") or {}
    _author = ba.get("name") or _ab.get("name") or b.get("author")
    _role = ba.get("role") or _ab.get("role") or b.get("role")
    _li = ba.get("linkedin") or _ab.get("linkedin")
    if _author:
        _name = (f'<a href="{_esc(_li)}" target="_blank" rel="noopener" style="color:{ink};text-decoration:none;'
                 f'border-bottom:1px solid {red}">{_esc(_author)}</a>') if _li else _esc(_author)
        _rest = " &nbsp;&middot;&nbsp; ".join(_esc(x) for x in [_role, _today_iso(), _read_time(c)] if x)
        _bits = _name + ((" &nbsp;&middot;&nbsp; " + _rest) if _rest else "")
        P.append(f'<div style="color:{muted};font-size:13px;border-top:1px solid {line};'
                 f'border-bottom:1px solid {line};padding:12px 0;margin:0 0 26px">{_bits}</div>')
    # hero: NOT inlined in the body — it is set as the WordPress FEATURED IMAGE (featured_media) on publish, so
    # the theme renders it as the post banner (e.g. Sensa single.php .phead). Inlining it too would double it.
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
    # lead (optionally with an editorial drop-cap on the opening letter)
    if c.get("lead"):
        P.append(_paras_dropcap(c["lead"], body, red, headf) if c.get("drop_cap") else _paras(c["lead"], body))
    # sections
    for i, s in enumerate(c.get("sections") or []):
        if s.get("heading"):
            P.append(f'<h2 style="color:{ink};font-family:{headf};font-weight:700;font-size:27px;'
                     f'line-height:1.25;margin:36px 0 14px">{_esc(s["heading"])}</h2>')
        if s.get("body"):
            _il = [l for l in (c.get("internal_links") or []) if l.get("section") == i]
            P.append(_paras_linked(s["body"], body, _il, red) if _il else _paras(s["body"], body))
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
        vid = s.get("video") or {}
        if vid.get("use") and vid.get("youtube_url"):
            P.append(_video_embed(vid["youtube_url"], vid.get("vertical"), vid.get("caption"), muted))
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
        elif kind == "custom":                          # a bespoke, hand-authored SVG/HTML graphic for THIS post
            inner = _sanitize_graphic_html(sg.get("html") or "")
            gfx = f'<div style="margin:0 0 24px">{inner}</div>' if inner else ""
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
    # author bio (E-E-A-T) — prefer the profile blog_author bio + a LinkedIn link on the name
    # external services/tools mentioned in the post -> a row of brand logos, each linking out (universal rule)
    svc = c.get("service_logos") or []
    if svc:
        chips = "".join(
            f'<a href="{_esc(s.get("website"))}" target="_blank" rel="noopener" style="display:inline-flex;'
            f'align-items:center;gap:8px;text-decoration:none;color:{ink};border:1px solid {line};border-radius:8px;'
            f'padding:6px 12px;margin:0 8px 8px 0"><img src="{_esc(s.get("logo"))}" alt="{_esc(s.get("name"))}" '
            f'style="width:20px;height:20px;border-radius:4px;object-fit:contain">'
            f'<span style="font-size:14px;font-weight:600">{_esc(s.get("name"))}</span></a>'
            for s in svc if s.get("website") and s.get("logo") and s.get("name"))
        if chips:
            P.append(f'<div style="border-top:1px solid {line};margin-top:30px;padding-top:18px">'
                     f'<div style="color:{muted};font-family:{headf};font-weight:700;font-size:12px;letter-spacing:.14em;'
                     f'text-transform:uppercase;margin-bottom:10px">Tools &amp; services mentioned</div>'
                     f'<div style="display:flex;flex-wrap:wrap;align-items:center">{chips}</div></div>')
    _biotext = ba.get("bio") or _ab.get("bio") or (c.get("author_bio") if isinstance(c.get("author_bio"), str) else "")
    _shot = ba.get("headshot") or _ab.get("headshot")
    if _biotext:
        _who = _author or ""
        _whoh = (f'<a href="{_esc(_li)}" target="_blank" rel="noopener" style="color:{ink};text-decoration:none;'
                 f'border-bottom:1px solid {red}">{_esc(_who)}</a>') if (_who and _li) else _esc(_who)
        _img = (f'<img src="{_esc(_shot)}" alt="{_esc(_who)}" style="width:56px;height:56px;border-radius:50%;'
                f'object-fit:cover;float:left;margin:2px 16px 8px 0">') if _shot else ""
        _liln = (f'<br><a href="{_esc(_li)}" target="_blank" rel="noopener" style="color:{red};'
                 f'text-decoration:none;font-weight:600">Connect on LinkedIn &rarr;</a>') if _li else ""
        P.append(f'<div style="background:{surface};border:1px solid {line};border-radius:14px;padding:20px;'
                 f'margin:30px 0 0;color:{muted};font-size:14px;line-height:1.65;overflow:hidden">'
                 f'{_img}{("<b style=color:"+ink+">"+_whoh+"</b><br>") if _who else ""}{_esc(_biotext)}{_liln}</div>')
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


def _image_jobs(c: dict) -> list[tuple[str, str, str]]:
    """The image-generation jobs a post needs (hero + figures + the real/AI split frames)."""
    jobs: list[tuple[str, str, str]] = []
    hero = c.get("featured_image") or c.get("hero") or {}
    if hero.get("image_prompt") and hero.get("use", True):   # featured_image is mandatory (use defaults True)
        jobs.append(("hero", hero["image_prompt"], "16:9"))
    for i, s in enumerate(c.get("sections") or []):
        fig = s.get("figure") or {}
        if fig.get("use") and fig.get("image_prompt") and len(jobs) < _MAX_IMAGES:
            jobs.append((f"fig{i}", fig["image_prompt"], "16:9"))
    sg = c.get("signature_graphic") or {}
    if sg.get("use") and sg.get("kind") == "real_ai_split":   # the dials are pure CSS; the split needs frames
        if sg.get("real_image_prompt"):
            jobs.append(("sig_real", sg["real_image_prompt"], "16:9"))
        if sg.get("ai_image_prompt"):
            jobs.append(("sig_ai", sg["ai_image_prompt"], "16:9"))
    return jobs


def _keep_reading(company: dict, c: dict, exclude_id=None, n: int = 3) -> list:
    """REAL 'keep reading' links: actual PUBLISHED posts on the company's site (same category first, then most
    recent), never invented. Returns [] when there are no real posts, so the block is simply skipped."""
    try:
        from .integrations import wordpress as _wp
        site = _wp.for_company(company)
    except Exception:  # noqa: BLE001
        site = None
    if not site:
        return []
    out, seen = [], ({exclude_id} if exclude_id else set())
    queries = []
    cat = (c.get("category") or "").split("·")[0].strip()
    if cat:
        cidn = site.ensure_category(cat)
        if cidn:
            queries.append(f"/posts?categories={cidn}&per_page=6&status=publish&orderby=date&_fields=id,title,link")
    queries.append("/posts?per_page=6&status=publish&orderby=date&_fields=id,title,link")   # fallback: most recent
    for q in queries:
        if len(out) >= n:
            break
        try:
            for p in (site._req("GET", q) or []):
                if p["id"] in seen:
                    continue
                title = _html.unescape((p.get("title") or {}).get("rendered") or "").strip()
                if title and p.get("link"):
                    out.append({"title": title, "url": p["link"]})
                    seen.add(p["id"])
                if len(out) >= n:
                    break
        except Exception:  # noqa: BLE001
            continue
    return out


def build_from_content(company_id: int, c: dict) -> dict:
    """Generate + host the imagery, then render the GIVEN content. Returns {title, html, dek, content, images}
    — content + images are returned so a later REVISION can reuse the same images (no regeneration)."""
    company = store.get_company(company_id)
    c = dict(c)
    c["keep_reading"] = _keep_reading(company, c)   # REAL published posts only, never the model's invented links
    post_slug = (c.get("seo") or {}).get("slug") or c.get("title") or ""
    imgs = _gen_and_host(company.get("slug") or "filmspoke", _image_jobs(c), prefix=post_slug)
    out = render(company_id, c, imgs)
    out["content"], out["images"] = c, imgs
    return out


def build(company_id: int, brief: str) -> dict:
    """Compose -> generate + host imagery on R2 -> render. Returns {title, html, dek, content, images}."""
    return build_from_content(company_id, compose(company_id, brief))


def revise_surgical(company_id: int, c: dict, correction: str) -> dict:
    """Apply ONLY the owner's requested change to an existing post's content, keeping everything else exactly
    as it was — so a revision never re-drafts the whole post or loses formatting. Returns the revised content
    (the caller re-renders it with the SAME images, no regeneration)."""
    company = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "content-blog-posts")
    system = "\n\n".join(filter(None, [
        f"You are making a SMALL, TARGETED revision to an EXISTING blog post for {company['name']}.",
        skill.get("craft") or "",
        worker._rules_block(skill),
        _BLOG_SCHEMA,
        ("CRITICAL: apply ONLY the change the owner asks for. Keep EVERY other field exactly as given — the "
         "title, dek, byline, every section heading and body, their order, the image fields (image_prompt/alt/"
         "caption), the takeaways and the CTAs — all UNCHANGED unless the requested change directly requires "
         "it. Do NOT rewrite, re-style, re-order or 'improve' anything else. Return the FULL post JSON."),
    ]))
    user = (f"Current post (JSON):\n{json.dumps(c, ensure_ascii=False)}\n\nThe ONLY change to make:\n"
            f"{correction}\n\nReturn the full post JSON with just that change applied, everything else identical.")
    out = provider.think_json(system, user, model=worker._model_for(skill), max_tokens=8000,
                              purpose="blog:revise", company=company.get("slug"))
    if not out or not out.get("title"):
        return c
    # belt-and-suspenders: reuse the existing image fields verbatim (we render with the SAME images anyway)
    out["featured_image"] = c.get("featured_image") or c.get("hero") or out.get("featured_image")
    return out
