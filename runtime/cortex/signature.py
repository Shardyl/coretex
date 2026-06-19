"""House-format email signatures — the Cortex standard (see docs/COMPANY-STANDARD.md).

Two layers stored on the profile: `signature` (plain text — name/role/contact lines, used for the plain-text
email part + worker grounding) and `signature_html` (the designed rich signature — table, brand-accent left
border, hosted logo — rendered on sends and shown in the cockpit). This module is the single builder so every
company's signature comes out in the same format; onboarding calls `store_for(...)` so a new company gets one
by default.

Logo MUST be referenced by a public URL (live-site, R2 media.coretex.uk, or the interim coretex.uk/assets),
never base64 (that would bloat worker grounding).
"""
from __future__ import annotations

from . import profile

ADDR_DEFAULT = "P316 The Binary, Business Bay, Dubai, UAE · PO Box 414195"


def logo_tile(url: str, alt: str, h: int = 30, pad: str = "12px 16px") -> str:
    """White/light logo on a dark rounded tile (for brands whose logo is white)."""
    return (f'<span style="display:inline-block;background:#0c0c0c;padding:{pad};border-radius:8px;">'
            f'<img src="{url}" alt="{alt}" height="{h}" style="display:block;height:{h}px;border:0;"></span>')


def logo_plain(url: str, alt: str, h: int = 40) -> str:
    """Colour/dark logo placed directly on white (no tile)."""
    return f'<img src="{url}" alt="{alt}" height="{h}" style="display:block;height:{h}px;border:0;">'


def _addr_html(address: str) -> str:
    return (f'<span style="color:#888;font-size:12px;">'
            f'{address.replace(" · ", " &nbsp;&middot;&nbsp; ")}</span>')


def build_html(*, logo_html: str, accent: str, name: str, company: str,
               phones: list[str] | None, email: str, web: str, address: str = ADDR_DEFAULT) -> str:
    ph = (f'<span style="color:#555;">{" &nbsp;&middot;&nbsp; ".join(phones)}</span><br>'
          if phones else "")
    web_clean = web.replace("https://", "").replace("http://", "").rstrip("/")
    web_disp = web_clean if web_clean.startswith("www.") else "www." + web_clean
    return ('<table cellpadding="0" cellspacing="0" border="0" '
            'style="font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;">'
            f'<tr><td style="padding-bottom:12px;">{logo_html}</td></tr>'
            f'<tr><td style="border-left:3px solid {accent};padding:2px 0 2px 12px;font-size:13px;line-height:1.55;">'
            f'<span style="font-size:15px;font-weight:bold;color:#111;">{name}</span><br>'
            f'<span style="color:#555;">{company}</span><br>{ph}'
            f'<a href="mailto:{email}" style="color:#1a1a1a;text-decoration:none;">{email}</a> &nbsp;|&nbsp; '
            f'<a href="https://{web_clean}" style="color:#1a1a1a;text-decoration:none;">{web_disp}</a><br>'
            f'{_addr_html(address)}</td></tr></table>')


def build_plain(*, name: str, company: str, phones: list[str] | None,
                email: str, web: str, address: str = ADDR_DEFAULT) -> str:
    web_clean = web.replace("https://", "").replace("http://", "").rstrip("/")
    web_disp = web_clean if web_clean.startswith("www.") else "www." + web_clean
    lines = [name, company]
    if phones:
        lines.append(" · ".join(phones))
    lines.append(f"{email} | {web_disp}")
    lines.append(address)
    return "\n".join(lines)


def store_for(company_id: int, *, logo_html: str, accent: str, name: str, company: str,
              phones: list[str] | None, email: str, web: str, address: str = ADDR_DEFAULT) -> dict:
    """Build BOTH the plain and rich signatures in the house format and save them on the profile."""
    html = build_html(logo_html=logo_html, accent=accent, name=name, company=company,
                      phones=phones, email=email, web=web, address=address)
    plain = build_plain(name=name, company=company, phones=phones, email=email, web=web, address=address)
    profile.set_field(company_id, "signature", plain)
    profile.set_field(company_id, "signature_html", html)
    return {"signature": plain, "signature_html": html}
