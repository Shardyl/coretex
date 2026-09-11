"""The worker — does the task per its skill (produces the deliverable)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import grounding, profile, provider, store

_GST = timezone(timedelta(hours=4))   # Gulf Standard Time (Dubai) — no DST


def _now_line() -> str:
    """Code-stamped current moment in GST. The drafter must anchor every day/date/time it mentions to
    this — proposing 'Monday or Wednesday' on a Tuesday is exactly the failure this prevents."""
    now = datetime.now(_GST)
    return ("Current date and time, code-stamped (GST / Dubai, UTC+4): "
            + now.strftime("%A %d %B %Y, %I:%M %p")
            + ". Anchor every day, date and time you mention to this moment — never guess or assume.")


def _company_context(company: dict, author: str | None = None) -> str:
    ctx = company.get("context") or {}
    parts = [f"Company: {company['name']}"]
    if company.get("north_star"):
        parts.append(f"Primary goal: {company['north_star']}")
    for k, label in (("voice", "Voice/tone"), ("audience", "Audience"),
                     ("products", "Products/services"), ("dos", "Always"), ("donts", "Never")):
        if ctx.get(k):
            parts.append(f"{label}: {ctx[k]}")
    base = "\n".join(parts)
    ground = grounding.for_company(company)   # Company Profile + brand guidelines + site source
    out = base + ("\n\n" + ground if ground else "")
    # Personal voice: when a piece is written AS a specific person (a LinkedIn comment/outreach/inbox reply,
    # a bylined or opinion post), write in THAT author's own voice (profile.voice.people.<author>). Neutral /
    # institutional content passes author=None and stays in the company voice above. Keyed like signatures.
    if author:
        try:
            pv = profile.resolve_voice(company.get("id"), author)
        except Exception:  # noqa: BLE001
            pv = None
        if pv:
            out += ("\n\nWrite this in the author's OWN first-person voice. Match it closely, keep the "
                    "personality, just keep it clean and professional:\n" + pv)
    return out


def _model_for(skill: dict) -> str:
    """Workers run on Sonnet by default; a skill tiered model='opus' overrides for high-quality work."""
    return provider.resolve_model(skill.get("model")) or provider.MODEL_FAST


# A Cc/Bcc SENDING directive anywhere in a rule, e.g. "...CC ben@x.com and BCC me@y.com". Same shape the
# envelope reads (the compiled skills.envelope config). These are actioned by the sending system, NOT instructions for the
# writer — hide any rule that sets a recipient from the drafter so the model can never echo it into the email
# body as a visible "system note".
_CC_DIRECTIVE = re.compile(r"\bb?cc\b\s+[\w.+-]+@", re.I)


def _rule_lines(skill: dict) -> list[str]:
    """The skill's effective rules (universal minus overrides + local), minus CC/BCC sending directives."""
    universal, local = store.effective_rules(skill)  # universal minus this company's overrides, then local
    return [r for r in (list(universal) + list(local)) if not _CC_DIRECTIVE.search(r or "")]


def _rules_block(skill: dict) -> str:
    rules = _rule_lines(skill)
    if not rules:
        return ""
    return "Standing rules you MUST follow:\n" + "\n".join(f"- {r}" for r in rules)


# Related skills whose trained rules must ALSO sit in front of the drafter for certain work — the worker is a
# dumb waiter: it fetches every relevant shelf, it never cooks. WHICH shelves relate to which task is plumbing
# (this map); WHAT the rules say lives only in the DB. Keyed by the drafting task's own skill_key.
_RELATED_SKILLS_DEFAULT = {
    # email-handling holds the company's general email voice; sales-followup governs thread continuations.
    "sales-first-response": ("email-handling", "sales-scheduling", "lead-qualification", "sales-followup"),
    # project correspondence drafts ON email-handling and also reads the project skills' rules.
    "email-handling": ("sales-scheduling", "prod-revisions", "prod-status-reporting", "prod-pipeline"),
}


def _related_map() -> dict:
    """The routing table is DATA (setting 'related_skills'), editable without a deploy; the constant
    above is only the seed/fallback."""
    try:
        from . import db
        m = db.setting_get("related_skills")
        if isinstance(m, dict) and m:
            return {k: tuple(v) for k, v in m.items()}
    except Exception:  # noqa: BLE001
        pass
    return _RELATED_SKILLS_DEFAULT


def related_skills(skill: dict, company: dict) -> list[dict]:
    """The related skills (with their live rules) for this task's skill, resolved from the DB."""
    out = []
    for key in _related_map().get((skill or {}).get("skill_key", ""), ()):
        s = store.get_skill_by_key(company["id"], key)
        if s:
            out.append(s)
    return out


def _related_block(skill: dict, company: dict) -> str:
    parts = []
    for s in related_skills(skill, company):
        rules = _rule_lines(s)
        if rules:
            parts.append(f"Standing rules from the related '{s['name']}' skill — follow them whenever this "
                         "message touches that ground (the company's email voice, proposing or arranging a "
                         "call, judging or handling the lead, following up on an open thread):\n"
                         + "\n".join(f"- {r}" for r in rules))
    return "\n\n".join(parts)


_EMAIL_BODY_RULE = (
    "This is an EMAIL. Write ONLY the email body, the greeting and the message, nothing else. These are hard "
    "rules, never break them:\n"
    "- NO From/To/Subject headers, NO Cc/Bcc line, NO recipient list, and NO routing note, 'sending note', "
    "'system note', or ANY meta/instruction text whatsoever (e.g. never write 'CC ... add via the sending "
    "system' or 'replace this'). Cc/Bcc and recipients are handled entirely by the sending system and must NEVER "
    "be mentioned in the message.\n"
    "- NEVER invent a booking link, scheduling link, calendar link, any URL, or a PLACEHOLDER of ANY "
    "kind — no '[OWNER TO CONFIRM]', '[TBD]', '[amount]', 'XXX' or bracketed blanks, ever. If a fact or "
    "figure the reply needs does not exist yet (a price, a date), the email says the concrete thing WILL "
    "follow (e.g. 'the updated quotation will follow') — it never ships a skeleton to fill in. "
    "A link that appears in your standing rules or in the task itself is real: use it exactly as written "
    "when it is relevant.\n"
    "- NO sign-off, NO your name, NO signature or contact details, the signature and logo are attached "
    "automatically, so adding them doubles them up.\n"
    "- Do NOT mention or instruct anyone to add attachments.\n"
    "Output is exactly the message the recipient reads, ready to send.")


_PRECEDENCE = (
    "PRECEDENCE. The company voice and the standing rules above are the authority on HOW this is written "
    "and WHAT it says: wherever anything else in these instructions disagrees with them, THEY WIN. They "
    "do not override five things, because those are facts the system computed or the shape it must "
    "receive back, not preferences: who this message is from, the code-stamped date and time, the real "
    "links and files you were handed, the ban on inventing any value you were not given, and the output "
    "format you were asked to produce. If a rule seems to ask for one of those, follow the rule as far "
    "as it goes and leave that part out.")


def _identity_block(company: dict, request: dict, author: str | None):
    """WHO this email is written as. The sending mailbox (request.from_email) is the truth, so the
    drafter writes in that person's first person instead of describing them in the third person (the
    deal timeline names people as 'sent from gino@...', and a drafter with no identity copies that
    framing straight into the email - card #411, 31 Aug 2026). Returns (block, author_key)."""
    frm = (request.get("from_email") or "").strip() if isinstance(request, dict) else ""
    who = author or frm
    if not who:
        return "", author
    try:
        ident = profile.resolve_identity(company.get("id"), frm or who) or {}
    except Exception:  # noqa: BLE001
        ident = {}
    name, role = ident.get("name") or "", ident.get("role") or ""
    addr = ident.get("email") or frm or ""
    bits = "YOU ARE WRITING AS: " + (name or addr or "the company")
    if role:
        bits += ", " + role
    if company.get("name"):
        bits += " at " + company["name"]
    if addr and name:
        bits += " (" + addr + ")"
    block = (bits + ". This message goes out from that mailbox, so write in the FIRST PERSON as that "
             "person: their own past and future actions are 'I' and 'we', NEVER their own name in the "
             "third person. Colleagues are referred to by first name where it helps the reader. Context "
             "you are given (deal timelines, logs, notes) describes people in the third person for the "
             "record: translate anything about YOUR OWN actions into the first person before writing it. "
             "Do not add a sign-off or signature; that is appended automatically.")
    named = _colleagues_named(company, request, addr)
    if named:
        # WHO YOU ARE NOT. The block above told the drafter to turn third-person context into the first
        # person, and it obeyed too well: writing as Ayresh, it read "Rashad is meeting Major Ibrahim at
        # Dubai Police headquarters on Thursday" off the deal note and wrote "I am meeting Major Ibrahim"
        # (8 Sep 2026). No name appears in that sentence, so no output check can catch it. The colleagues
        # actually present in THIS context are found by name, deterministically, and named back here.
        block += ("\n\nTHESE PEOPLE ARE NAMED IN YOUR CONTEXT AND THEY ARE NOT YOU: " + "; ".join(named)
                  + ". Whatever the material below attributes to one of them stays theirs and stays in "
                  "their name. Never write another person's meeting, travel, commitment, decision or "
                  "action as your own. Only what YOU did or will do becomes 'I'.")
    return block, who


_CONTEXT_FIELDS = ("brief", "system_note", "deal_timeline", "contact_notes", "owner_feedback",
                   "meeting_notes")


def _colleagues_named(company: dict, request: dict, me: str) -> list:
    """The company's OTHER people who are actually named in this task's context. Deterministic: the
    roster is the company profile's signatures, and the test is their first name appearing in the
    briefing material. Empty when the context names nobody but the sender."""
    if not isinstance(request, dict):
        return []
    hay = " ".join(str(request.get(k) or "") for k in _CONTEXT_FIELDS)
    if not hay.strip():
        return []
    try:
        sigs = (profile.get(company.get("id")) or {}).get("signatures") or {}
    except Exception:  # noqa: BLE001
        return []
    out = []
    for addr, sig in (sigs or {}).items():
        name = ((sig or {}).get("name") or "").strip()
        if not name or str(addr).strip().lower() == (me or "").strip().lower():
            continue
        first = name.split()[0]
        if re.search(r"\b" + re.escape(first) + r"\b", hay, re.I):
            role = ((sig or {}).get("role") or "").strip()
            out.append(f"{name}" + (f", {role}" if role and role.lower() != "none" else ""))
    return out


def draft(skill: dict, company: dict, request: dict,
          correction: str | None = None, manager_feedback: list[str] | None = None,
          author: str | None = None, prev_draft: str | None = None) -> str:
    is_email = isinstance(request, dict) and bool(request.get("outbound") or request.get("inquiry"))
    ident_block = ""
    if is_email:
        ident_block, author = _identity_block(company, request, author)
    # ORDER IS AUTHORITY. Everything written in code goes FIRST and the editable rules go LAST, nearest
    # the task, closed by an explicit precedence statement. The code blocks used to sit AFTER the rules,
    # so wherever the two disagreed the hardcoded line won on position alone.
    system = "\n\n".join(filter(None, [
        f"You are Cortex's worker for the '{skill['name']}' skill.",
        _now_line(),
        ident_block,
        _EMAIL_BODY_RULE if is_email else
        "Produce the deliverable only — no preamble, no explanation, no meta-commentary.",
        _company_context(company, author),
        skill.get("craft") or "",
        _rules_block(skill),
        _related_block(skill, company) if is_email else "",
        _PRECEDENCE,
    ]))
    atts = request.get("attachments") if isinstance(request, dict) else None
    user = [f"Task: {request.get('brief') if isinstance(request, dict) else request}"]
    if is_email:   # tell the worker WHO it's writing to, so it greets the recipient (not Rashad/itself)
        inq = request.get("inquiry") or {}
        # THEIR EMAIL must always reach the drafter. Some lanes embed it in the brief (_email_brief);
        # any lane that doesn't gets it appended here — a reply drafted blind is never acceptable
        # (bit MAH Gold card #328: 'no message content to reply to', Aug 2026).
        their = (inq.get("message") or inq.get("snippet") or "").strip()
        if their and their[:200] not in (request.get("brief") or ""):
            user.append("THEIR EMAIL (this is the message you are replying to — address exactly what it says; "
                        "quoted earlier messages below it are thread history for context):\n" + their[:6000])
        _sn = (request.get("system_note") or "").strip()
        if _sn:
            user.append("SITUATION (system knowledge, NOT the client's words — never quote or reference "
                        "this text in the email): " + _sn[:500])
        bits = []
        if inq.get("name") or inq.get("email"):
            bits.append(f"This email is addressed TO {inq.get('name') or inq.get('email')} — greet THEM by "
                        "name and write to them in the second person.")
        if inq.get("subject"):
            bits.append(f"Subject: {inq.get('subject')}.")
        # (An "it is sent BY the owner ... Rashad IS the sender" line lived here from June 2026, when
        # every Sensa email genuinely was his. `_identity_block` replaced it on 31 Aug but it was never
        # removed, so every draft carried both instructions, and being nearer the task the hardcoded one
        # won: card 501 wrote as Rashad out of Gino's mailbox, "Gino has kept me across the project"
        # (7 Sep 2026). WHO a message is from is resolved from the company profile. Never named in code.)
        # Pass the triage/qualification FACTS through to the drafter — how they shape the reply is governed
        # entirely by the (related) skill rules, never by this code.
        tri = request.get("triage") or {}
        if tri.get("category"):
            bits.append(f"Triage category: {tri['category']}.")
        sug = request.get("qual_suggest") or {}
        if sug.get("verdict") or sug.get("bucket"):
            q = f"Lead-qualification suggestion (advisory; the owner decides): verdict={sug.get('verdict') or 'n/a'}"
            if sug.get("bucket"):
                q += f", handling bucket={sug['bucket']}"
            if sug.get("reason"):
                q += f" — {sug['reason']}"
            bits.append(q + ". Apply the lead-qualification standing rules for that bucket.")
        person = sug.get("person") or {}
        if person.get("location"):
            loc = f"Research on the sender (public sources, advisory): based in {person['location']}"
            if person.get("timezone"):
                loc += f" ({person['timezone']})"
            if person.get("role"):
                loc += f", role: {person['role']}"
            bits.append(loc + ". Apply the standing rules on tailoring suggested call times to the lead's "
                        "region/timezone.")
        user.insert(0, " ".join(bits))
    if atts:
        user.append(f"{len(atts)} file(s)/image(s) are attached below — use them as source material for the deliverable.")
    if (skill.get("skill_key") or "") in ("sales-quotation", "sales-first-response", "email-handling"):
        try:   # the rate card rides every quotation-adjacent draft: approved prices only, never invented
            from . import ratecard
            _rc = ratecard.render((company or {}).get("slug") or "")
            if _rc:
                user.append(_rc)
        except Exception:  # noqa: BLE001
            pass
    _did = request.get("deal_id") if isinstance(request, dict) else None
    if _did and "DEAL TIMELINE" not in (request.get("brief") or ""):
        try:   # every deal-linked draft reads the deal's timeline (research brief, commitments, flow)
            from . import pipeline as _pl
            _tl = _pl.deal_context(int(_did))
            if _tl:
                user.append(_tl)
        except Exception:  # noqa: BLE001
            pass
    _hist = (request.get("thread_history") or "").strip() if isinstance(request, dict) else ""
    if _hist:
        user.append("CONVERSATION HISTORY with this contact (newest first — includes emails WE already sent). "
                    "Stay consistent with it: never re-introduce yourself or the company, never repeat or "
                    "contradict something already sent, and NEVER share a meeting link different from one "
                    "already sent in this history.\n" + _hist[:5000])
    _tl = (request.get("deal_timeline") or "").strip() if isinstance(request, dict) else ""
    if _tl:
        user.append("DEAL STATE (real logged timeline — stay consistent with what was promised and where "
                    "the deal stands):\n" + _tl[:3000])
    _cn = (request.get("contact_notes") or "").strip() if isinstance(request, dict) else ""
    if _cn:
        user.append("NOTES the team saved on this contact (factor them in; never quote them verbatim):\n"
                    + _cn[:2000])
    _of = (request.get("owner_feedback") or "").strip() if isinstance(request, dict) else ""
    if _of:
        user.append("THE OWNER'S PAST CORRECTIONS on this relationship (lessons already taught — obey them "
                    "without being asked again):\n" + _of[:2000])
    _ml = (request.get("media_library") or "").strip() if isinstance(request, dict) else ""
    if _ml:
        # HOW MANY samples to send, and what to do when none fit, is a sales judgement: it lives on the
        # skill. This block is the shelf plus the invariant that the links are real and exact.
        user.append("MEDIA LIBRARY — our REAL portfolio films (title [categories]: link). These are the "
                    "ONLY sample-work links that exist. Copy any you use EXACTLY as written, and NEVER "
                    "write, guess or adapt any other portfolio/library/media URL. Your standing rules "
                    "govern how many to share and when to share none.\n" + _ml[:4000])
    _av = (request.get("availability") or "").strip() if isinstance(request, dict) else ""
    if _av:
        # Which timezone to state them in, and how to word the offer, are scheduling rules on the skill.
        user.append(_av + "\nIf you propose a call, offer times ONLY from that list: never invent one. "
                    "If none of them suit the conversation, ask them to suggest a time instead.")
    _mn = (request.get("meeting_notes") or "").strip() if isinstance(request, dict) else ""
    if _mn:
        user.append("NOTES FROM OUR LAST MEETING with this contact (distilled from the real meeting notes — "
                    "ground your reply in what was actually discussed, decided and committed; never "
                    "contradict it):\n" + _mn[:3000])
    _exm = request.get("existing_meeting") if isinstance(request, dict) else None
    if _exm and _exm.get("meet"):
        user.append(f"A meeting with this contact is ALREADY BOOKED: '{_exm.get('summary')}' at "
                    f"{_exm.get('start')}, Google Meet {_exm['meet']}. Do NOT propose, arrange or imply any "
                    "other meeting; when the draft mentions the call, use THAT exact link and time.")
    _docs = [r.get("filename") for r in (request.get("attach_docs") or []) if r.get("filename")]         if isinstance(request, dict) else []
    if _docs:
        user.append("FILES ATTACHED TO THIS OUTGOING EMAIL (they genuinely send with it): "
                    + ", ".join(_docs) + ". They are on the message already: refer to them as attached "
                    "now, and NEVER promise to send them later. How you word that is your voice.")
    _meet = ((request.get("meeting") or {}).get("meet") or "").strip() if isinstance(request, dict) else ""
    if _meet:
        user.append(f"A meeting is CONFIRMED and already booked. Its REAL Google Meet link is {_meet} — "
                    "include this exact link where natural in the email. NEVER write any other meeting link.")
    for at in (request.get("attachment_texts") or []) if isinstance(request, dict) else []:
        user.append(f"ATTACHED DOCUMENT '{at.get('filename') or 'document'}' (text extracted from the "
                    f"client's attachment — read it and address its content):\n{(at.get('text') or '')[:15000]}")
    if prev_draft:
        user.append("YOUR PREVIOUS DRAFT (the one under revision — change ONLY what the correction or "
                    "feedback requires, keep every other sentence exactly as it is):\n" + prev_draft[:6000])
    if manager_feedback:
        user.append("Your manager flagged these to fix:\n- " + "\n- ".join(manager_feedback))
    if correction:
        user.append("The owner corrected your previous draft. Apply it LITERALLY and MINIMALLY: when he asks "
                    "to remove something, remove exactly that and nothing around it (removing 'the date' "
                    "takes out the date, not the day of the week); when he asks to change or add something, "
                    "touch only that. Produce the new version:\n" + correction)
    # FIRST replies on the sales lane draft on Fable 5 (owner-approved exception, 2026-08-30): the
    # opener + insight set the whole conversation's direction, and the research brief deserves the
    # model that can use it. Thread continuations fall back to the skill's own tier. The mapping is
    # DATA (setting 'first_reply_models'), not a model id in code, so the tier is visible and editable
    # rather than a silent upgrade nobody can see (7 Sep 2026).
    _mdl = _model_for(skill)
    if isinstance(request, dict) and not request.get("thread_reply"):
        try:
            from . import db as _db
            _fm = (_db.setting_get("first_reply_models") or {}).get(skill.get("skill_key") or "")
            if _fm:
                _mdl = provider.resolve_model(_fm) or _fm
        except Exception:  # noqa: BLE001 — a settings hiccup never blocks a draft
            pass
    # 16,000, not 6,000: max_tokens is a ceiling the model cannot see, and adaptive thinking spends from
    # it before a word is written. At 6,000 a hard brief exhausted it in thought (card 545). This streams,
    # so the cap costs nothing unless it is actually used.
    out = provider.think(system, "\n\n".join(user), model=_mdl, think_hard=True,
                         max_tokens=16000, purpose=f"draft:{skill.get('skill_key', '')}",
                         company=company.get("slug"), images=atts)
    return _no_dashes(out) if is_email else out   # house rule: no em/en dashes in visible email copy


def _no_dashes(s: str) -> str:
    """House rule: no em/en dashes in visible copy (keep numeric-range hyphens)."""
    return (s.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ").replace("–", "-"))


def draft_article(skill: dict, company: dict, request: dict,
                  correction: str | None = None, manager_feedback: list[str] | None = None) -> dict:
    """Write a blog article for the company website. Returns {"title", "html"}."""
    system = "\n\n".join(filter(None, [
        f"You are Cortex's worker for the '{skill['name']}' skill, writing a blog article for the company website.",
        _company_context(company),
        skill.get("craft") or "",
        _rules_block(skill),
        ('Output a JSON object with exactly two fields: "title" (plain text, no markdown) and '
         '"html" (the article body as clean HTML). Rules for the html: use only <h2>, <h3>, <p>, '
         "<ul>/<li>, <ol>/<li>, <strong>, <em>, <a href>, <blockquote>. Do NOT include an <h1> "
         "(the CMS adds the title from the title field). No markdown, no <html>/<head>/<body>, no "
         "inline styles. Do NOT use em-dashes or en-dashes anywhere; use commas, colons or periods."),
        # (Editorial shape — leading with the answer, question-style H2s, paragraph length — was written
        # here in code. It is editorial judgement, so it lives on the content-blog-posts skill.)
        _PRECEDENCE,
    ]))
    user = [f"Brief: {request.get('brief') if isinstance(request, dict) else request}"]
    if manager_feedback:
        user.append("Your manager flagged these to fix:\n- " + "\n- ".join(manager_feedback))
    if correction:
        user.append("The owner corrected your previous draft. Apply it LITERALLY and MINIMALLY: when he asks "
                    "to remove something, remove exactly that and nothing around it (removing 'the date' "
                    "takes out the date, not the day of the week); when he asks to change or add something, "
                    "touch only that. Produce the new version:\n" + correction)
    out = provider.think_json(system, "\n\n".join(user), model=_model_for(skill), fast=False,
                              max_tokens=8000, purpose=f"blog:{skill.get('skill_key', '')}",
                              company=company.get("slug"))
    title = _no_dashes((out.get("title") or "").strip()) or "Untitled"
    html = _no_dashes((out.get("html") or "").strip())
    return {"title": title, "html": html}


def infer_rule(skill: dict, correction: str, old_draft: str, new_draft: str) -> dict:
    """Turn a one-off correction into a standing rule, if it implies one."""
    return provider.think_json(
        "You convert an owner's correction into a concise general standing rule for a skill — but ONLY "
        "if the correction implies a durable preference. One-offs with no general lesson are not rules.",
        f"Skill: {skill['name']}\nThe owner's correction: {correction}\n\n"
        'Return JSON: {"is_rule": boolean, "rule": "a short imperative rule (or empty string)"}',
    )
