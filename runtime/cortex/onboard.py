"""Apply the Company Standard to a company — see docs/COMPANY-STANDARD.md.

`onboard_company(slug, name, ...)` runs the safe, automatable parts of the standard (idempotent) and returns
a report of what was done + what still needs the operator. A new company gets, by default: the full uniform
skill roster, a profile row, the brand-kit file inventory from its Drive folder, the newsletter conventions
checklist, and (when inputs are supplied) a house-format signature. The global code (composer, /assets +
media delivery, contact counter, universal rules incl. brand-fidelity) already applies to every company.
"""
from __future__ import annotations

from . import brand, catalog, db, profile, signature, store


def seed_company_skills(company_id: int) -> int:
    """Seed the full uniform roster for ONE company (insert-only for a new company, so it never clobbers an
    existing company's live-edited skills). Universal rules apply automatically by skill_key."""
    n = 0
    for cat, dept, mgr, skills in catalog.CATALOG:
        for key, name, *rest in skills:
            craft = rest[0] if rest else catalog._craft(name)
            stakes = "high" if key in catalog.GATED else "low"
            model = "opus" if key in catalog.OPUS_SKILLS else None   # None = Sonnet default
            store.upsert_skill(company_id, key, name, craft=craft, authority="ask", stakes=stakes,
                               category=cat, department=dept, manager=mgr, model=model)
            n += 1
    return n


def _ensure_profile(company_id: int) -> None:
    db.execute("insert into company_profiles (company_id) values (%s) on conflict (company_id) do nothing",
               (company_id,))


def onboard_company(slug: str, name: str | None = None, *, kind: str = "owned",
                    sig: dict | None = None, refresh_brand: bool = True) -> dict:
    """Idempotent. `sig` (optional) = kwargs for signature.store_for to auto-build the signature now."""
    report: dict = {"slug": slug, "did": [], "todo": []}
    co = store.get_company_by_slug(slug)
    if not co:
        co = store.upsert_company(slug, name or slug, kind=kind)
        report["did"].append("created company row")
    cid = co["id"]
    _ensure_profile(cid)

    have = db.one("select count(*) n from skills where company_id=%s", (cid,))
    if not have or not have.get("n"):
        report["did"].append(f"seeded uniform skill roster ({seed_company_skills(cid)} skills)")
    else:
        report["did"].append(f"skills already present ({have['n']}) — left untouched")

    data = profile.get(cid) or {}
    if data.get("asset_folder"):
        if refresh_brand:
            try:
                kit = brand.refresh_files(cid)
                report["did"].append(f"refreshed brand-kit file inventory ({len(kit.get('files', []))} files)")
            except Exception as e:  # noqa: BLE001
                report["todo"].append(f"brand kit: could not read the Drive asset folder ({e})")
    else:
        report["todo"].append("set the Drive asset_folder link on the profile (the brand source)")

    if sig:
        signature.store_for(cid, **sig)
        report["did"].append("generated + stored the house-format signature (plain + rich HTML)")
    elif not data.get("signature_html"):
        report["todo"].append("signature: give accent + a hosted logo URL + name/phones/email/web "
                              "(signature.store_for then builds it in the house format)")

    if not data.get("send_domain"):
        report["todo"].append("newsletter send_domain (verify a Mailgun news.<domain> first)")
    if not data.get("test_group"):
        report["todo"].append("newsletter/blog test group (~10 addresses)")
    if not data.get("reply_from"):
        report["todo"].append(f"per-company Google OAuth (read + send): /oauth/google/start?company={slug}")

    report["media_base"] = f"https://media.coretex.uk/{slug}/"
    return report
