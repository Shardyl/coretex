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
import re
from urllib.parse import urlparse

import httpx
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
                  model: str | None = None, brief: str | None = None) -> str:
    """model=None uses the skill's tier. Pass a model id to override (e.g. provider.MODEL_FAST) for A/B.
    brief = the operator's own steer from Talk (film to feature, angle, facts): the idea is built FROM it."""
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
    if (brief or "").strip():
        user = ("Propose the newsletter idea for the next issue FROM THIS BRIEF from the operator. Follow it: the "
                "film, the angle and any facts it gives are fixed. Do not add production facts about the work that "
                "the brief does not give. Repeat EVERY URL in the brief verbatim in the idea (the system carries "
                "them into the build). Plain text only, no HTML.\n\nBRIEF:\n" + brief.strip())
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


_FREEMAIL = {"gmail.com", "hotmail.com", "yahoo.com", "outlook.com", "icloud.com", "live.com", "aol.com",
             "protonmail.com", "me.com", "msn.com", "ymail.com", "googlemail.com", "yahoo.co.uk", "hotmail.co.uk",
             "outlook.co.uk", "live.co.uk", "yahoo.co.in", "eim.ae", "emirates.net.ae", "etisalat.ae", "du.ae"}


def audience_config(company_id: int) -> dict:
    """WHO a company's newsletter goes to, from `company_profiles.data.newsletter_audience` (owner-set data,
    not code). Defaults reproduce the old behaviour (organisation label = company name, no exclusions, no
    phasing) so a company without the block is unchanged.
      org_labels                 organisation labels that count as this company's list (ilike, any)
      exclude_sources            lead_source values never on the newsletter (e.g. bought outreach prospects)
      exclude_instantly_campaigns contacts sitting in an Instantly campaign are left to that sequence
      cold_sources               lead_source values of the COLD cohort: never mailed, dripped in capped batches
      cold_cap / cold_cap_max    first batch size, and the ceiling the cap doubles towards on clean sends
      mode                       "label" (default: every contact under the org labels) or "subscribers"
                                 (only contacts with newsletter_subscriber = true: the flag IS the list;
                                 owner decision 13 Sep 2026, Sensa + Tabscanner)"""
    co = store.get_company(company_id) or {}
    a = dict((_profile(company_id).get("newsletter_audience") or {}))
    a.setdefault("org_labels", [co.get("name") or ""])
    a.setdefault("exclude_sources", [])
    a.setdefault("exclude_instantly_campaigns", False)
    a.setdefault("cold_sources", [])
    a.setdefault("cold_cap", 500)
    a.setdefault("cold_cap_max", 4000)
    a.setdefault("mode", "label")
    return a


def cold_cap(company_id: int) -> int:
    """Current cold-cohort batch size (state, in settings); starts at the profile's cold_cap."""
    a = audience_config(company_id)
    v = db.setting_get(f"nl_cold_cap:{company_id}")
    return int(v) if v else int(a["cold_cap"])


def issue_exclusions(task_id: int | None) -> dict:
    """Per-issue exclusions stored on the built issue: {labels, domains, emails, why}."""
    if not task_id:
        return {}
    art = db.setting_get(f"newsletter:{task_id}") or {}
    return dict(art.get("exclude") or {})


def client_exclusions(company_id: int, films: list[dict]) -> dict:
    """A case-study issue never goes to the client it features (owner rule, 13 Sep 2026). From each featured
    film's `client` label in the media library: the label itself (matched on company_name) plus the email
    domains of contacts filed under it, free-mail excluded. Stored on the issue; the send applies it."""
    labels, domains = [], set()
    for f in films:
        row = db.one("select client from media_assets where youtube_video_id=%s and client is not null "
                     "order by (company_id=%s) desc limit 1", (f["id"], company_id))
        label = ((row or {}).get("client") or "").strip()
        if not label or label.lower() in {x.lower() for x in labels}:
            continue
        labels.append(label)
        # Candidate domains come from contacts filed under the client. A domain is only excluded when it is
        # clearly the client's own: at least 80% of ALL contacts on that domain are linked to the client. Card 601
        # (13 Sep 2026) had excluded Al Futtaim, Etisalat webmail and yahoo.co.uk because supplier accounts named
        # "X / Dubai Police" and vendors on ISP mail were swept in: 34 innocent contacts, zero real ones.
        linked = db.query("select lower(split_part(email,'@',2)) d, count(*) n from crm_master where email like '%%@%%' "
                          "and (company_name ilike %s or account_id in (select id from crm_accounts where "
                          "lower(name)=lower(%s) or lower(name) like lower(%s) || ' (%%')) group by 1",
                          (f"%{label}%", label, label))
        for r in linked:
            d = r["d"]
            if not d or d in _FREEMAIL:
                continue
            total = db.one("select count(*) n from crm_master where lower(split_part(email,'@',2))=%s", (d,))["n"]
            if total and r["n"] >= max(1, round(0.8 * total)):
                domains.add(d)
    if not labels:
        return {}
    return {"labels": labels, "domains": sorted(domains), "emails": [],
            "why": "featured client(s) are never sent the issue about them"}


def add_issue_exclusions(task_id: int, labels=None, domains=None, emails=None, why: str = "") -> dict:
    """Merge manual exclusions onto a built issue (review / scheduled / send card). Returns the merged block."""
    key = f"newsletter:{task_id}"
    art = db.setting_get(key)
    if not art:
        raise ValueError(f"card #{task_id} has no built newsletter to exclude from")
    ex = dict(art.get("exclude") or {})
    ex["labels"] = sorted({*(ex.get("labels") or []), *[x.strip() for x in (labels or []) if x.strip()]})
    ex["domains"] = sorted({*(ex.get("domains") or []), *[x.strip().lower().lstrip("@") for x in (domains or []) if x.strip()]})
    ex["emails"] = sorted({*(ex.get("emails") or []), *[x.strip().lower() for x in (emails or []) if x.strip()]})
    if why:
        ex["why"] = ((ex.get("why") or "") + "; " + why).strip("; ")
    art["exclude"] = ex
    db.setting_set(key, art)
    return ex


def _excluded(r: dict, ex: dict) -> bool:
    if not ex:
        return False
    e = (r.get("email") or "").strip().lower()
    dom = e.rsplit("@", 1)[-1] if "@" in e else ""
    if e in set(ex.get("emails") or []):
        return True
    if dom and dom in set(ex.get("domains") or []):
        return True
    cn = (r.get("company_name") or "").lower()
    return any(cn and lab.lower() in cn for lab in (ex.get("labels") or []))


def audience(company_id: int, task_id: int | None = None) -> dict:
    """The audience for ONE issue, split so the owner can see exactly what a send reaches:
       established  contacts sent every issue (everyone not in the cold cohort, plus cold contacts already
                    delivered once)
       cold_batch   the next capped slice of the cold cohort (never mailed before), in import order
       cold_waiting how many cold contacts are still queued for later issues
       excluded     contacts removed by this issue's exclusions (featured client, manual)"""
    a = audience_config(company_id)
    co = store.get_company(company_id)
    labels = [x for x in a["org_labels"] if x] or [co["name"]]
    org_sql = " or ".join(["organisation ilike %s"] * len(labels))
    params: list = [f"%{x}%" for x in labels] + [Json([co.get("slug")])]
    sql = (f"select id, email, first_name, company_name, lead_source, created_at from crm_master where ({org_sql}) "
           "and email ~ '^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$' "
           "and not (do_not_market @> %s::jsonb) "
           f"and not ({_SUPPRESS})")
    if a.get("mode") == "subscribers":
        sql += " and lower(coalesce(newsletter_subscriber::text,'')) = 'true'"   # column is text ('True'/'False')
    if a["exclude_sources"]:
        sql += " and coalesce(lead_source,'') <> all(%s)"
        params.append(list(a["exclude_sources"]))
    if a["exclude_instantly_campaigns"]:
        sql += " and coalesce(campaign_name,'') = ''"
    sql += " order by created_at, id"
    rows = db.query(sql, tuple(params))
    ex = issue_exclusions(task_id)
    delivered: set[str] = set()
    cold_src = set(a["cold_sources"])
    if cold_src:
        ensure_jobs_table()
        delivered = {r["email"] for r in db.query(
            "select distinct email from newsletter_deliveries where company_id=%s", (company_id,))}
    seen: set[str] = set()
    established, cold, excluded = [], [], []
    for r in rows:
        e = (r["email"] or "").strip().lower()
        if not e or e in seen:
            continue
        seen.add(e)
        item = {"email": e, "first_name": (r.get("first_name") or "").strip()}
        if _excluded(r, ex):
            excluded.append(item)
        elif cold_src and (r.get("lead_source") in cold_src) and e not in delivered:
            cold.append(item)
        else:
            established.append(item)
    cap = cold_cap(company_id) if cold_src else len(cold)
    return {"established": established, "cold_batch": cold[:cap], "cold_waiting": max(0, len(cold) - cap),
            "cold_cap": cap, "excluded": excluded, "exclusions": ex}


def recipients(company_id: int, task_id: int | None = None) -> list[dict]:
    """The list ONE send reaches: established contacts plus this issue's cold batch, minus the issue's
    exclusions. De-duplicated, valid email only. Returns [{email, first_name}]. Pass the card id so the count
    the owner confirms and the list the job freezes are computed the same way."""
    au = audience(company_id, task_id)
    return au["established"] + au["cold_batch"]


def audience_summary(company_id: int, task_id: int | None = None) -> str:
    """One line the owner reads on the card: what this send reaches and why."""
    au = audience(company_id, task_id)
    n = len(au["established"]) + len(au["cold_batch"])
    parts = [f"{n:,} recipients: {len(au['established']):,} established"]
    if au["cold_batch"] or au["cold_waiting"]:
        parts.append(f"+ {len(au['cold_batch']):,} cold-cohort (batch cap {au['cold_cap']:,}, "
                     f"{au['cold_waiting']:,} still waiting)")
    if au["excluded"]:
        ex = au["exclusions"]
        who = ", ".join((ex.get("labels") or []) + (ex.get("domains") or [])) or "manual"
        parts.append(f"| {len(au['excluded']):,} excluded ({who})")
    return " ".join(parts)


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


# ---------- featured films (DATA, never prose) + link guard ----------
#
# Card 592 (13 Sep 2026): the operator gave the exact YouTube link in Talk, ideation dropped it, and the writer
# linked the INTERNAL media library instead. Film links and their cover art are not something a model chooses:
# code extracts every YouTube video from the brief + idea, looks it up in the media library, renders its official
# thumbnail as a card linked to the video, and refuses any link that is not the company's own site or a known film.

_YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^\s&]*&)*v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")


class BadLink(RuntimeError):
    """The composed issue carries a link that is not allowed (internal library, unknown film, foreign site)."""


def featured_films(company_id: int, *texts: str) -> list[dict]:
    """Every YouTube video mentioned in the given texts (brief first, then idea), in order of first appearance,
    resolved against the media library: [{id, url, title, in_library}]. The operator's own links count even
    when the library has no row for them (the brief is operator-supplied data)."""
    seen: list[str] = []
    for t in texts:
        for vid in _YT_ID.findall(t or ""):
            if vid not in seen:
                seen.append(vid)
    out = []
    for vid in seen:
        row = db.one("select title, watch_url from media_assets where youtube_video_id=%s "
                     "order by (company_id=%s) desc limit 1", (vid, company_id))
        out.append({"id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                    "title": ((row or {}).get("title") or "").strip(), "in_library": bool(row)})
    return out


def _thumb_bytes(video_id: str) -> bytes | None:
    """The official YouTube cover art for a video, largest available first. None if YouTube has nothing."""
    for name in ("maxresdefault", "sddefault", "hqdefault"):
        try:
            r = httpx.get(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg", timeout=15, follow_redirects=True)
            if r.status_code == 200 and len(r.content) > 2000:
                return r.content
        except Exception:  # noqa: BLE001
            continue
    return None


def _company_hosts(company_id: int) -> set[str]:
    """Hostnames the company owns or publishes on: profile domains / live_site / social, the send domain's root."""
    prof = _profile(company_id)
    hosts: set[str] = set()
    blobs = [str(prof.get("domains") or ""), str(prof.get("live_site") or ""), str(prof.get("social") or "")]
    for blob in blobs:
        for m in re.findall(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", blob, flags=re.I):
            hosts.add(m.lower())
    root = (send_domain(company_id) or "").split(".", 1)
    if len(root) == 2:
        hosts.add(root[1].lower())
    hosts.discard("github.com")   # website_repo links live in live_site for some companies; never a public link
    hosts -= _SOCIAL_HOSTS         # social hosts are shared with the whole world: only the company's own pages pass
    return hosts


_SOCIAL_HOSTS = {"youtube.com", "youtu.be", "instagram.com", "linkedin.com", "vimeo.com", "facebook.com",
                 "x.com", "twitter.com", "tiktok.com"}


def _social_pages(company_id: int) -> set[str]:
    """The company's own social pages from the profile, normalised (host/path, no scheme, no www, no slash)."""
    out = set()
    for m in re.findall(r"(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+[a-z]{2,}/[^\s|,;)]+)",
                        str(_profile(company_id).get("social") or ""), flags=re.I):
        out.add(m.rstrip("/").lower().split("?", 1)[0])
    return out


def _norm(url: str) -> str:
    u = re.sub(r"^https?://", "", url.strip(), flags=re.I)
    u = re.sub(r"^(www|m)\.", "", u, flags=re.I)
    return u.rstrip("/").lower().split("?", 1)[0]


def _link_allowed(url: str, company_id: int, film_ids: set[str], hosts: set[str], social_urls: set[str]) -> bool:
    if url.strip() == "%unsubscribe_url%":
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    if host in ("coretex.uk", "www.coretex.uk"):
        return False   # the internal media library / cockpit, never public
    bare = host[4:] if host.startswith("www.") else host
    if bare in _SOCIAL_HOSTS or bare == "m.youtube.com":
        m = _YT_ID.search(url)
        if m:
            return m.group(1) in film_ids            # a real film: featured, or in the company's library
        n = _norm(url)
        return any(n == p or n.startswith(p + "/") for p in social_urls)   # the company's own page only
    return any(host == h or host.endswith("." + h) for h in hosts)


def _walk_strings(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _walk_strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk_strings(v)
    elif isinstance(x, str):
        yield x


def check_links(c: dict, company_id: int, films: list[dict]) -> None:
    """Every URL in a composed issue must be the company's own site/social, or a featured film's YouTube link.
    Anything else fails the build. Film ids also include the company's whole media library, so a writer may
    cite another of the company's films by its real link, but never an invented id or the internal library."""
    lib = {r["youtube_video_id"] for r in db.query(
        "select youtube_video_id from media_assets where company_id=%s and youtube_video_id is not null", (company_id,))}
    film_ids = lib | {f["id"] for f in films}
    hosts = _company_hosts(company_id)
    social_urls = _social_pages(company_id)
    bad = []
    for s in _walk_strings(c):
        for u in _URL.findall(s):
            u = u.rstrip(".,;:!?")
            if not _link_allowed(u, company_id, film_ids, hosts, social_urls):
                bad.append(u)
    if bad:
        raise BadLink("issue carries a link that is not allowed (only the company's own site/social and known "
                      "YouTube films may be linked): " + ", ".join(sorted(set(bad))))


def _films_block(films: list[dict]) -> str:
    if not films:
        return ""
    lines = [f"- {f['title'] or 'the film'}: {f['url']}" for f in films]
    return ("FEATURED FILM(S), FIXED DATA. The system renders each one as its official YouTube cover art linked to "
            "the video, placed right after the section that presents the work, so you do NOT need to describe or "
            "link the artwork. Refer to the film by name in the work section; if you include a link to it use this "
            "exact URL and nothing else. Never link any other film, library, playlist or media page.\n" + "\n".join(lines))


def _upload_from_request(req: dict | None) -> bytes | None:
    """The FIRST image the operator attached to the card (Talk attachments ride as data: URLs). An attached
    image is the header, full stop: card 613 (13 Sep 2026) carried one and the build ignored it."""
    for a in (req or {}).get("attachments") or []:
        try:
            if isinstance(a, str) and a.startswith("data:image/") and "," in a:
                return base64.b64decode(a.split(",", 1)[1])
        except Exception:  # noqa: BLE001
            continue
    return None


class EmptyIssue(RuntimeError):
    """compose() came back with no issue (empty, truncated or unparseable JSON). Never render or send it."""


def _require_issue(c: dict) -> dict:
    """An issue with no subject and no body is not an issue. Card 592 (13 Sep 2026) rendered header + footer
    around nothing and sent that to the test group; the fallback subject hid the failure. Fail loudly instead."""
    has_body = bool((c.get("sections") or []) or (c.get("intro") or "").strip() or (c.get("headline") or "").strip())
    if not (c.get("subject") or "").strip() or not has_body:
        raise EmptyIssue("the writer returned no usable issue (empty or cut-off JSON); nothing built, nothing sent")
    return c


def compose(company_id: int, idea_text: str, films: list[dict] | None = None) -> dict:
    company = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "content-newsletter")
    system = "\n\n".join(filter(None, [
        f"You are composing ONE newsletter issue for {company['name']}.",
        skill.get("craft") or _LIGHT_GUIDE,       # the editable skill craft drives the writing
        worker._company_context(company),
        worker._rules_block(skill),
        store.examples_block(company_id, "newsletters"),   # distilled approved exemplars
        _LIGHT_SCHEMA,                            # structural output the renderer parses — stays in code
        _films_block(films or []),
    ]))
    user = f"Approved idea:\n{idea_text}\n\nCompose the full issue now as JSON."
    out = provider.think_json(system, user, model=worker._model_for(skill), max_tokens=6000,
                              purpose="newsletter_compose", company=company.get("slug"))
    return _require_issue(out or {})


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

    films_html = ""
    for f in (c.get("films") or []):
        label = _esc(f"Watch {f['title']} on YouTube" if f.get("title") else "Watch the film on YouTube")
        img = (f'<a href="{_esc(f["url"])}"><img src="cid:{f["cid"]}" width="536" alt="{_esc(f.get("title") or "Watch the film")}" '
               f'style="display:block;width:100%;height:auto;border-radius:10px;border:0;"></a>') if f.get("cid") else ""
        films_html += (f'<tr><td style="padding:6px 32px 12px;">{img}<p style="margin:10px 0 0;font:600 14px/1.4 {bodyfont};">'
                       f'<a href="{_esc(f["url"])}" style="color:{primary};text-decoration:none;">{label} &rarr;</a></p></td></tr>')
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
{films_html}
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
    for f in (c.get("films") or []):
        lines += [f"Watch {f['title'] or 'the film'} on YouTube: {f['url']}", ""]
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
    "hero {use, source (\"generate\" = an Imagen prompt, or \"film\" = the featured film's own YouTube frame; "
    "choose \"film\" whenever the brief or an owner correction asks for a still, frame, screenshot, thumbnail "
    "or cover of the film as the header), image_prompt, alt}; eyebrow_pill (short red kicker or null); headline; intro; "
    "primary_cta {label, url}; format_chips (array, e.g. 30s/9:16/16:9, or empty); "
    "sections (array of {heading, body, image:{use, kind:\"feature\"|\"grid\", "
    "items:[{image_prompt, alt, caption}]}}); steps {use, title, items:[{title, text}]}; "
    "stat_band {use, stats:[{value, label, highlight}]}; quote {use, text, attribution}; "
    "closing_cta {use, heading, subtext, label, url}. Use real absolute FilmSpoke URLs (https://filmspoke.ai...)."
)
_FS_COMPOSE = _FS_GUIDE + "\n\n" + _FS_SCHEMA   # back-compat alias


def compose_filmspoke(company_id: int, idea_text: str, films: list[dict] | None = None) -> dict:
    company = store.get_company(company_id)
    skill = store.get_skill_by_key(company_id, "content-newsletter")
    system = "\n\n".join(filter(None, [
        f"You are composing ONE newsletter issue for {company['name']}.",
        skill.get("craft") or _FS_GUIDE,          # the editable skill craft drives the writing
        worker._company_context(company),
        worker._rules_block(skill),
        store.examples_block(company_id, "newsletters"),   # distilled approved exemplars
        _FS_SCHEMA,                               # structural output the renderer parses — stays in code
        _films_block(films or []),
    ]))
    user = f"Approved idea:\n{idea_text}\n\nCompose the full issue now as JSON."
    out = provider.think_json(system, user, model=worker._model_for(skill), max_tokens=6000,
                              purpose="newsletter_compose", company=company.get("slug"))
    return _require_issue(out or {})


_FS_MAX_IMAGES = 5   # excluding the logo; bounds Gemini cost + latency per issue


def _gen_images(jobs: list[tuple[str, str, str]]) -> dict:
    """jobs = [(key, prompt, aspect)]. Generate concurrently; return {key: bytes|None}."""
    if not jobs:
        return {}

    def run(job):
        k, prompt, aspect = job
        return k, imagegen.hero(prompt, aspect=aspect, purpose="image:newsletter")

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


def _build_filmspoke(company_id: int, idea_text: str, kit: dict, films: list[dict] | None = None,
                     hero_upload: bytes | None = None) -> dict:
    company = store.get_company(company_id)
    films = films or []
    c = compose_filmspoke(company_id, idea_text, films)
    check_links(c, company_id, films)

    jobs: list[tuple[str, str, str]] = []
    hero = c.get("hero") or {}
    # HEADER IMAGE PRECEDENCE (owner, 13 Sep 2026): an image the owner attached > the featured film's own
    # YouTube frame when asked for (hero.source == "film") > a generated hero. Previously only "generated" existed,
    # so attachments and "use a screenshot of the film" corrections changed nothing.
    fixed_hero: bytes | None = None
    if hero_upload:
        fixed_hero = hero_upload
        c["hero"] = {**hero, "use": True, "source": "upload", "alt": hero.get("alt") or "Header image"}
    elif hero.get("use") and str(hero.get("source") or "").lower() == "film" and films:
        fixed_hero = _thumb_bytes(films[0]["id"])
        if fixed_hero:
            c["hero"] = {**hero, "use": True, "alt": hero.get("alt") or (films[0].get("title") or "Film frame")}
    if fixed_hero is None and hero.get("use") and hero.get("image_prompt"):
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
    if fixed_hero:
        images.append(("hero.jpg", _optimize_jpeg(fixed_hero, 1200)))
        c.setdefault("hero", {})["cid"] = "hero.jpg"
    elif gen.get("hero"):
        images.append(("hero.jpg", _optimize_jpeg(gen["hero"], 1200)))
        c.setdefault("hero", {})["cid"] = "hero.jpg"
    elif c.get("hero"):
        c["hero"]["use"] = False
    c["films"] = _attach_films(films, images)
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
            "text": render_text_filmspoke(company_id, c), "images": images, "content": c,
            "exclude": client_exclusions(company_id, films)}


def _attach_films(films: list[dict], images: list[tuple[str, bytes]]) -> list[dict]:
    """Fetch each featured film's official YouTube cover art as an inline attachment (film{n}.jpg)."""
    out = []
    for n, f in enumerate(films):
        b = _thumb_bytes(f["id"])
        item = {"title": f.get("title") or "", "url": f["url"], "cid": None}
        if b:
            item["cid"] = f"film{n}.jpg"
            images.append((item["cid"], _optimize_jpeg(b, 1200)))
        out.append(item)
    return out


def render_filmspoke(company_id: int, c: dict, logo_cid: str | None) -> str:
    kit = brand.get_brand_kit(company_id) or {}
    col = kit.get("colors") or {}
    bg = col.get("bg", "#0A0A0A"); surface = col.get("surface", "#121212"); line = col.get("line", "#242424")
    ink = col.get("ink", "#F4F4F5"); body = col.get("body", "#C9CAD0"); muted = col.get("muted", "#9A9AA0")
    red = col.get("primary", "#E50914")
    # text colour that sits ON the accent (buttons / filled discs). White is right for a dark accent like
    # FilmSpoke red; a bright accent like Sensa cyan needs dark ink for contrast. Kit opts in via accent_ink.
    accent_ink = col.get("accent_ink", "#FFFFFF")
    # optional brand gradient on buttons (e.g. Snap Rewards magenta->purple). Email-safe: a solid colour
    # underneath (Outlook ignores the gradient and shows that) + the linear-gradient on top for the rest.
    _grad = col.get("gradient")
    btn_bg = (f"background:{col.get('purple') or red};background-image:linear-gradient({_grad});"
              if _grad else f"background:{red};")
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
                f'<td style="border-radius:6px;{btn_bg}">'
                f'<a href="{_esc(url)}" style="display:inline-block;font:700 16px/1 {headf};color:{accent_ink};'
                f'padding:16px 32px;border-radius:6px;{btn_bg}text-decoration:none;">{_esc(label)} &nbsp;&rarr;</a>'
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

    def film_cards():
        out = ""
        for f in (c.get("films") or []):
            label = _esc(f"Watch {f['title']} on YouTube" if f.get("title") else "Watch the film on YouTube")
            img = (f'<a href="{_esc(f["url"])}" style="text-decoration:none;"><img src="cid:{f["cid"]}" width="512" '
                   f'alt="{_esc(f.get("title") or "Watch the film")}" style="display:block;width:100%;height:auto;'
                   f'border-radius:12px;border:0;"></a>') if f.get("cid") else ""
            out += (f'<tr><td style="padding:16px 44px 0;">{img}<p style="margin:10px 0 0;font:600 14px/1.4 {bodyf};">'
                    f'<a href="{_esc(f["url"])}" style="color:{red};text-decoration:none;">{label} &rarr;</a></p></td></tr>')
        return out

    secs = c.get("sections") or []
    if secs:
        rows.append(divider())
    else:
        rows.append(film_cards())
    for si, s in enumerate(secs):
        rows.append(f'<tr><td style="padding:26px 44px 0;"><h2 style="margin:0 0 10px;font:700 22px/1.25 {headf};'
                    f'color:{ink};">{_esc(s.get("heading"))}</h2>{para(s.get("body"))}</td></tr>')
        if si == 0:
            rows.append(film_cards())
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
    for si, s in enumerate(c.get("sections") or []):
        L += [str(s.get("heading") or "").upper(), (s.get("body") or "").strip(), ""]
        if si == 0:
            for f in (c.get("films") or []):
                L += [f"Watch {f['title'] or 'the film'} on YouTube: {f['url']}", ""]
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


def build(company_id: int, idea_text: str, brief: str = "", hero_upload: bytes | None = None) -> dict:
    """Compose + render one issue. Dispatches on the brand kit's `template`: a 'dark*' template (FilmSpoke)
    uses the dark cinematic renderer with multiple inline images; everything else uses the light card."""
    kit = brand.get_brand_kit(company_id) or {}
    _tmpl = str(kit.get("template") or "")
    films = featured_films(company_id, brief, idea_text)   # the operator's links are data, carried by code
    if _tmpl.startswith("dark") or _tmpl == "light-saas":   # rich, brand-kit-driven renderer (dark OR light)
        return _build_filmspoke(company_id, idea_text, kit, films, hero_upload)
    c = compose(company_id, idea_text, films)
    check_links(c, company_id, films)
    if hero_upload:
        hero = _optimize_jpeg(hero_upload, 1200)
    else:
        hero = imagegen.hero(c.get("hero_prompt") or "", purpose="image:newsletter") if c.get("hero_prompt") else None
    images = [("hero.jpg", hero)] if hero else []
    c["films"] = _attach_films(films, images)
    return {"subject": c.get("subject") or f"{store.get_company(company_id)['name']} newsletter",
            "html": render_html(company_id, c, hero_cid="hero.jpg" if hero else None),
            "text": render_text(company_id, c), "images": images, "content": c,
            "exclude": client_exclusions(company_id, films)}


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
    upload = _upload_from_request(task.get("request"))
    try:
        built = build(cid, task.get("draft") or "", brief=str((task.get("request") or {}).get("brief") or ""),
                      hero_upload=upload)
    except Exception as e:  # noqa: BLE001 - a failed build keeps the idea card approvable; nothing is sent
        msg = f"build failed: {e}"[:200]
        store.update_task(task["id"], status="awaiting_approval", last_status=msg)
        try:
            from .integrations import telegram as tg
            tg.send(f"[{company['name']}] newsletter build failed on card #{task['id']}: {e}. Nothing sent. "
                    f"Approve again to retry.")
        except Exception:  # noqa: BLE001
            pass
        return {"error": msg}
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
               f"Sent to your test group. Approve to schedule it for the full {company['name']} list.")
    store.update_task(rev["id"], draft=summary, status="awaiting_approval")
    db.setting_set(f"newsletter:{rev['id']}", {"exclude": built.get("exclude") or {},
        "subject": built["subject"], "html": built["html"], "text": built["text"],
        "images_b64": [[cid, base64.b64encode(b).decode()] for cid, b in built["images"]],
        "idea_task_id": task["id"], "idea": task.get("draft") or "",
        "brief": str((task.get("request") or {}).get("brief") or ""), "corrections": [],
        "hero_upload_b64": base64.b64encode(upload).decode() if upload else None})
    return {"sent_to": f"the test group ({len(group)})", "review_task": rev["id"]}


def correct_issue(task: dict, skill: dict, company: dict, text: str, actor: str = "owner") -> dict:
    """A correction on a REVIEW / SEND card REBUILDS the issue and re-sends the test. Card 599 (13 Sep 2026):
    the generic correction rewrote the card's two-line summary only, while the built HTML, the subject and
    the card title kept the old wording, so an approve would have sent the uncorrected issue. Now: the idea
    the issue was built from + every correction so far -> compose again -> render -> [TEST] to the group ->
    artifact, title and summary all replaced. Nothing goes to the live list here."""
    cid, tid = company["id"], task["id"]
    art = db.setting_get(f"newsletter:{tid}") or {}
    if not art.get("html"):
        return {"ok": False, "error": "no built newsletter on this card"}
    idea = (art.get("idea") or "").strip()
    if not idea:   # issues built before the artifact carried its idea: find the idea card by subject
        subj = ((task.get("request") or {}).get("subject") or art.get("subject") or "").strip()
        row = db.one("select id, draft, request from tasks where company_id=%s and kind='newsletter_idea' "
                     "and status='done' and title=%s order by id desc limit 1", (cid, subj))
        if row:
            idea = row.get("draft") or ""
            art["idea_task_id"] = row["id"]
            art["brief"] = str((row.get("request") or {}).get("brief") or "")
    if not idea:
        idea = f"Subject line: {art.get('subject')}" + chr(10) + (art.get("text") or "")[:1500]
    corrections = list(art.get("corrections") or []) + [text.strip()]
    idea_text = (idea + chr(10) + chr(10) + "OWNER CORRECTIONS on the previous build. Apply EVERY one of them; "
                 "they override the idea and any earlier wording:" + chr(10)
                 + chr(10).join(f"- {c}" for c in corrections))
    upload = _upload_from_request(task.get("request"))   # an image attached with the correction wins
    if upload:
        art["hero_upload_b64"] = base64.b64encode(upload).decode()
    elif art.get("hero_upload_b64"):
        upload = base64.b64decode(art["hero_upload_b64"])
    elif not art.get("idea_task_id") is None:
        src = store.get_task(int(art["idea_task_id"])) if art.get("idea_task_id") else None
        upload = _upload_from_request((src or {}).get("request")) if src else None
        if upload:
            art["hero_upload_b64"] = base64.b64encode(upload).decode()
    built = build(cid, idea_text, brief=str(art.get("brief") or ""), hero_upload=upload)
    group = test_group(cid)
    if group:
        send_bulk(cid, "[TEST] " + built["subject"], built["html"], built["text"],
                  [{"email": g["email"], "first_name": g.get("name")} for g in group],
                  built["images"], tag="newsletter-test")
    ex = dict(art.get("exclude") or {})
    auto = built.get("exclude") or {}
    for k in ("labels", "domains", "emails"):
        ex[k] = sorted({*(ex.get(k) or []), *(auto.get(k) or [])})
    db.setting_set(f"newsletter:{tid}", {**art, "exclude": ex, "subject": built["subject"], "html": built["html"],
        "text": built["text"], "images_b64": [[c, base64.b64encode(b).decode()] for c, b in built["images"]],
        "idea": idea, "corrections": corrections})
    req = dict(task.get("request") or {})
    req["title"] = f"Newsletter ready: {built['subject']}"
    req["subject"] = built["subject"]
    verb = "send" if task["kind"] == "newsletter_send" else "schedule"
    summary = (f"Subject: {built['subject']}" + chr(10) + chr(10) +
               f"Rebuilt with your correction and sent again to your test group ({len(group)}). "
               f"Approve to {verb} it for the full {company['name']} list.")
    store.update_task(tid, title=built["subject"], draft=summary, request=req, status="awaiting_approval",
                      attempts=int(task.get("attempts") or 0) + 1)
    store.log_decision(tid, skill["id"], actor, "newsletter_rebuilt", note=text[:200],
                       snapshot={"subject": built["subject"], "corrections": len(corrections)})
    return {"ok": True, "subject": built["subject"], "test_group": len(group)}


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
    n = len(recipients(cid, task["id"]))
    if not live_sends_on():
        store.update_task(task["id"], status="awaiting_approval")
        return {"blocked": True, "recipients": n,
                "error": f"Live newsletter sends are OFF. This would reach {n} real {company['name']} "
                         f"contacts. Turn on live sends first."}
    if not confirmed:
        store.update_task(task["id"], status="awaiting_approval")
        return {"needs_confirm": True, "recipients": n}
    recips = recipients(cid, task["id"])
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


_DELIVERIES_DDL = """
create table if not exists newsletter_deliveries (
    id bigserial primary key,
    company_id bigint not null,
    email text not null,
    job_id bigint,
    sent_at timestamptz not null default now()
);
create index if not exists newsletter_deliveries_co_email on newsletter_deliveries (company_id, email);
"""
_COLD_CLEAN_PCT = 0.03   # a finished send with bounces under this share of sent doubles the cold cap


_DELIVERIES_DDL = """
create table if not exists newsletter_deliveries (
    id bigserial primary key,
    company_id bigint not null,
    email text not null,
    job_id bigint,
    sent_at timestamptz not null default now()
);
create index if not exists newsletter_deliveries_co_email on newsletter_deliveries (company_id, email);
"""
_COLD_CLEAN_PCT = 0.03   # a finished send with bounces under this share of sent doubles the cold cap


def ensure_jobs_table() -> None:
    db.execute(_JOBS_DDL)
    db.execute("alter table newsletter_send_jobs add column if not exists images_b64 jsonb")
    db.execute("alter table newsletter_send_jobs add column if not exists stats jsonb")
    db.execute("alter table newsletter_send_jobs add column if not exists stats_at timestamptz")
    db.execute("alter table newsletter_send_jobs add column if not exists finished_at timestamptz")
    db.execute(_DELIVERIES_DDL)


# ---------- monthly cap (the Mailgun plan) ----------
DEFAULT_MONTHLY_CAP = 50000   # emails per calendar month across ALL companies (owner, 13 Sep 2026)


def monthly_usage() -> dict:
    """Where the shared Mailgun allowance stands this calendar month (UTC): what every company's jobs have sent,
    what in-flight jobs still have to send, the cap, and what is left for new sends. Code computes it; the send
    gate reads it."""
    ensure_jobs_table()
    cap = int(db.setting_get("mailgun_monthly_cap") or DEFAULT_MONTHLY_CAP)
    r = db.one("select coalesce(sum(sent),0) s from newsletter_send_jobs "
               "where created_at >= date_trunc('month', now() at time zone 'utc')")
    f = db.one("select coalesce(sum(total - sent),0) s from newsletter_send_jobs where status in ('running','paused')")
    sent = int(r["s"] or 0)
    inflight = int(f["s"] or 0)
    return {"cap": cap, "sent_month": sent, "in_flight": inflight, "committed": sent + inflight,
            "remaining": max(0, cap - sent - inflight), "month": db.one("select to_char(now() at time zone 'utc','Mon YYYY') m")["m"]}


def check_monthly_cap(n_new: int) -> dict:
    """Would sending `n_new` more this month break the plan cap? {ok, ...usage, would_be}. The Stage-3 send and
    the auto-send both refuse when ok is False; Stage-2 scheduling only warns (the send month may differ)."""
    u = monthly_usage()
    u["would_be"] = u["committed"] + int(n_new)
    u["ok"] = u["would_be"] <= u["cap"]
    return u


def usage_line(n_new: int | None = None) -> str:
    u = check_monthly_cap(n_new or 0)
    s = f"Mailgun {u['month']}: {u['sent_month']:,} sent + {u['in_flight']:,} in flight of {u['cap']:,}"
    if n_new:
        s += f"; this send takes it to {u['would_be']:,}" + ("" if u["ok"] else " - OVER THE CAP")
    return s


# ---------- per-send stats (cached on the job; Mailgun keeps events only for a while) ----------
_STAT_KEYS = ("accepted", "delivered", "failed", "opened", "clicked", "unsubscribed", "complained", "hard_bounces")


def job_stats(job_id: int, max_age_min: int = 10, force: bool = False) -> dict:
    """Stats for one send, refreshed from Mailgun when older than max_age_min and merged so a counter never goes
    DOWN when Mailgun's event retention expires (the cached history is the record). Returns the stats dict."""
    ensure_jobs_table()
    job = db.one("select * from newsletter_send_jobs where id=%s", (job_id,))
    if not job:
        return {}
    cached = dict(job.get("stats") or {})
    fresh_enough = job.get("stats_at") and (db.one("select now() - %s < make_interval(mins => %s) as ok",
                                                     (job["stats_at"], max_age_min)) or {}).get("ok")
    finished_long_ago = job.get("finished_at") and (db.one("select now() - %s > interval '14 days' as ok",
                                                            (job["finished_at"],)) or {}).get("ok")
    if cached and not force and (fresh_enough or finished_long_ago):
        return cached
    live = campaign_stats(job["company_id"], job_id)
    if live.get("error"):
        return cached
    merged = dict(cached)
    for k in _STAT_KEYS:
        merged[k] = max(int(cached.get(k) or 0), int(live.get(k) or 0))
    merged["sent"], merged["total"], merged["status"] = live["sent"], live["total"], live["status"]
    d = merged.get("delivered") or 0
    s = merged.get("sent") or 0
    merged["open_rate"] = round(100 * merged["opened"] / d, 1) if d else 0.0
    merged["click_rate"] = round(100 * merged["clicked"] / d, 1) if d else 0.0
    merged["bounce_rate"] = round(100 * merged["failed"] / s, 1) if s else 0.0
    merged["hard_bounce_rate"] = round(100 * (merged.get("hard_bounces") or 0) / s, 1) if s else 0.0
    db.execute("update newsletter_send_jobs set stats=%s, stats_at=now() where id=%s", (Json(merged), job_id))
    return merged


def history(company_id: int | None = None, limit: int = 50) -> list[dict]:
    """Every send, newest first, with its cached stats (no Mailgun call here; the detail view refreshes)."""
    ensure_jobs_table()
    flt = "" if company_id is None else " where company_id=%s"
    p: tuple = () if company_id is None else (company_id,)
    rows = db.query("select id, company_id, task_id, subject, status, sent, total, per_hour, created_at, "
                    f"finished_at, stats, stats_at from newsletter_send_jobs{flt} order by created_at desc limit %s",
                    p + (limit,))
    out = []
    for j in rows:
        co = store.get_company(j["company_id"])
        out.append({"job_id": j["id"], "id": j["task_id"], "kind": "newsletter_history", "title": j["subject"],
                    "company": co["name"] if co else "", "company_id": j["company_id"], "status": j["status"], "sent": j["sent"], "total": j["total"],
                    "started": j["created_at"], "finished": j.get("finished_at"), "stats": j.get("stats") or {},
                    "link": f"/api/content/preview/{j['task_id']}", "link_label": "View issue", "link_fetch": True})
    return out


def _record_deliveries(company_id: int, job_id: int, chunk: list[dict]) -> None:
    """Who has now received a newsletter from this company: the cold cohort graduates through this table."""
    emails = [c["email"] for c in chunk if c.get("email")]
    if emails:
        db.execute("insert into newsletter_deliveries (company_id, email, job_id) "
                   "select %s, unnest(%s::text[]), %s", (company_id, emails, job_id))


def _ramp_cold_cap(job: dict, sent: int) -> None:
    """A finished send that stayed clean (new bounces under 3% of sent) earns the next cold batch double the
    size, up to the profile's ceiling. A dirty send leaves the cap where it is (the 8% spike already pauses)."""
    cid = job["company_id"]
    a = audience_config(cid)
    if not a["cold_sources"] or sent <= 0:
        return
    try:
        bnew = len(mailgun.suppressions(send_domain(cid), "bounces")) - (job["bounces_at_start"] or 0)
    except Exception:  # noqa: BLE001
        return
    if bnew <= sent * _COLD_CLEAN_PCT:
        cur = cold_cap(cid)
        nxt = min(cur * 2, int(a["cold_cap_max"]))
        if nxt != cur:
            db.setting_set(f"nl_cold_cap:{cid}", nxt)
    db.execute(_DELIVERIES_DDL)


def _record_deliveries(company_id: int, job_id: int, chunk: list[dict]) -> None:
    """Who has now received a newsletter from this company: the cold cohort graduates through this table."""
    emails = [c["email"] for c in chunk if c.get("email")]
    if emails:
        db.execute("insert into newsletter_deliveries (company_id, email, job_id) "
                   "select %s, unnest(%s::text[]), %s", (company_id, emails, job_id))


def _ramp_cold_cap(job: dict, sent: int) -> None:
    """A finished send that stayed clean (new bounces under 3% of sent) earns the next cold batch double the
    size, up to the profile's ceiling. A dirty send leaves the cap where it is (the 8% spike already pauses)."""
    cid = job["company_id"]
    a = audience_config(cid)
    if not a["cold_sources"] or sent <= 0:
        return
    try:
        bnew = len(mailgun.suppressions(send_domain(cid), "bounces")) - (job["bounces_at_start"] or 0)
    except Exception:  # noqa: BLE001
        return
    if bnew <= sent * _COLD_CLEAN_PCT:
        cur = cold_cap(cid)
        nxt = min(cur * 2, int(a["cold_cap_max"]))
        if nxt != cur:
            db.setting_set(f"nl_cold_cap:{cid}", nxt)


def enqueue_send(company_id: int, task_id: int, art: dict, recips: list[dict],
                 per_hour: int = DEFAULT_PER_HOUR) -> int:
    """Queue a full-list send to DRIP OUT over time instead of blasting. The engine drains it.
    IDEMPOTENT per task (audit): a retry/crash-rerun for the same card returns the existing job
    instead of mailing the entire list a second time."""
    ensure_jobs_table()   # self-heal: works on any box / fresh DB without a manual migration step
    if db.setting_get("outbound_paused"):
        raise RuntimeError("outbound is PAUSED - resume it before queueing a newsletter send")
    if not live_sends_on():
        raise RuntimeError("live newsletter sends are OFF (newsletter_live_sends) - the full-list "
                           "safeguard applies to every enqueue path")
    dup = db.one("select id from newsletter_send_jobs where task_id=%s limit 1", (task_id,))
    if dup:
        return dup["id"]
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
        db.execute("update newsletter_send_jobs set status='done', finished_at=coalesce(finished_at, now()), "
                   "updated_at=now() where id=%s", (jid,))
        _ramp_cold_cap(job, sent)
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
    try:
        _record_deliveries(cid, jid, chunk[:n])
    except Exception:  # noqa: BLE001 - bookkeeping must never stop a send
        pass
    if done:
        db.execute("update newsletter_send_jobs set finished_at=coalesce(finished_at, now()) where id=%s", (jid,))
        _ramp_cold_cap(job, newsent)
        return {"status": "done", "job_id": jid, "task_id": job["task_id"], "company_id": cid,
                "subject": job["subject"], "sent": newsent, "total": total}
    return None


def campaign_stats(company_id: int, job_id: int | None = None) -> dict:
    """Live numbers for one newsletter send (the latest job for the company by default), from Mailgun's event
    log, unique recipients per event: sent, accepted, delivered, failed (with the bounce share), opened, clicked,
    unsubscribed, complained, plus open/click rates on delivered. Code computes; nothing is estimated."""
    if job_id:
        job = db.one("select * from newsletter_send_jobs where id=%s", (job_id,))
    else:
        job = db.one("select * from newsletter_send_jobs where company_id=%s order by id desc limit 1", (company_id,))
    if not job:
        return {"error": "no send found"}
    domain = send_domain(job["company_id"])
    begin = int(job["created_at"].timestamp())
    out = {"job_id": job["id"], "subject": job["subject"], "status": job["status"], "sent": job["sent"],
           "total": job["total"], "started": job["created_at"].isoformat()}
    for ev in ("accepted", "delivered", "failed", "opened", "clicked", "unsubscribed", "complained"):
        try:
            items = mailgun.events(domain, ev, begin, tag="newsletter")
        except Exception:  # noqa: BLE001
            items = []
        out[ev] = len({i.get("recipient") for i in items})
    # HARD bounces = what the 8% auto-pause actually watches: NEW addresses on Mailgun's bounce list since the
    # job started. "failed" also counts temporary failures and one-off rejections, so it reads high (owner
    # saw 10% and asked; the hard-bounce rate was 4.3%).
    try:
        out["hard_bounces"] = max(0, len(mailgun.suppressions(domain, "bounces")) - int(job.get("bounces_at_start") or 0))
    except Exception:  # noqa: BLE001
        out["hard_bounces"] = None
    d = out.get("delivered") or 0
    out["open_rate"] = round(100 * out["opened"] / d, 1) if d else 0.0
    out["click_rate"] = round(100 * out["clicked"] / d, 1) if d else 0.0
    s = out.get("sent") or 0
    out["bounce_rate"] = round(100 * out["failed"] / s, 1) if s else 0.0
    out["hard_bounce_rate"] = round(100 * (out["hard_bounces"] or 0) / s, 1) if s else 0.0
    return out


def stats_line(company_id: int, job_id: int | None = None) -> str:
    st = campaign_stats(company_id, job_id)
    if st.get("error"):
        return st["error"]
    return (f"'{st['subject']}' ({st['status']}): {st['sent']:,}/{st['total']:,} sent, {st['delivered']:,} delivered, "
            f"{st['failed']:,} failed ({st['bounce_rate']}%, of which {st.get('hard_bounces') or 0} hard bounces = "
            f"{st['hard_bounce_rate']}%, the auto-pause line is 8%), {st['opened']:,} opened ({st['open_rate']}%), "
            f"{st['clicked']:,} clicked ({st['click_rate']}%), {st['unsubscribed']} unsubscribed, "
            f"{st['complained']} complaints")


def brand_kit(company_id: int) -> dict | None:
    return brand.get_brand_kit(company_id)
