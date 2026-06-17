"""Per-company brand kit.

Source of truth = the company's Drive `asset_folder` (captured by the questionnaire, stored in
`company_profiles.data.asset_folder`, living inside the Cortex Drive folder and readable via
`cortex.drive`). The STRUCTURED kit (palette / fonts / logos / gradient / voice) is cached in
`company_profiles.data['brand']` so every builder (newsletter, web page, quotation) reads it without
hitting Drive on each call. `refresh_files()` re-lists the Drive folder to keep logo/asset ids current.
"""
from __future__ import annotations

from psycopg.types.json import Json

from . import db, drive


def _profile(company_id: int) -> dict | None:
    return db.one("select company_id, data from company_profiles where company_id = %s", (company_id,))


def asset_folder(company_id: int) -> str | None:
    d = (_profile(company_id) or {}).get("data") or {}
    return d.get("asset_folder")


def get_brand_kit(company_id: int) -> dict | None:
    """The cached structured brand kit, or None if not set yet."""
    d = (_profile(company_id) or {}).get("data") or {}
    return d.get("brand")


def set_brand_kit(company_id: int, kit: dict) -> dict:
    """Merge a brand kit into company_profiles.data['brand']."""
    p = _profile(company_id)
    data = dict((p or {}).get("data") or {})
    data["brand"] = {**(data.get("brand") or {}), **kit}
    db.execute("update company_profiles set data = %s, updated_at = now() where company_id = %s",
               (Json(data), company_id))
    return data["brand"]


def list_assets(company_id: int) -> list[dict]:
    """Live listing of the company's Drive asset folder (files: id, name, mimeType, size)."""
    folder = asset_folder(company_id)
    if not folder:
        return []
    return drive.list_folder(folder)


def refresh_files(company_id: int) -> dict:
    """Re-read the Drive asset folder; record the file inventory (ids + names) under brand['files'].
    Does NOT overwrite the curated palette/fonts — those follow the Brand Guidelines doc."""
    files = list_assets(company_id)
    inventory = [{"id": f["id"], "name": f["name"], "mimeType": f.get("mimeType")} for f in files]
    return set_brand_kit(company_id, {"files": inventory})
