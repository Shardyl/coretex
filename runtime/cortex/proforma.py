"""Pro forma invoices, issued against a SIGNED quotation (owner, 21 Sep 2026).

The flow: a client signs a quotation and emails it back -> Cortex recognises the returned PDF, matches it to
the STORED version it was issued as (`quote_versions:<number>`), and raises a confirm card -> the owner
confirms -> the deal's payment terms are set from that version, the deal is Booked, and the pro forma for the
first payment stage is issued as a simple one-line invoice ("70% down payment against quotation
SEN-2026-0014 for media production").

WHO DECIDES WHAT. A model only READS the returned PDF (is it our quotation, is it signed, which total does it
show); it never produces a figure that is used. Every amount comes from the stored version: net summed by
code, the stage percentage parsed by code from the payment lines that version printed, VAT from the profile.
The number is issued by code. The wording and numbering are DATA: the `PROFORMA = {...}` line in the
`finance-quote-to-invoice` skill craft (same pattern as golf-booking's PLAN line)."""
from __future__ import annotations

import base64
import datetime
import html as _html
import json
import os
import re

from . import db, quotation

SKILL_KEY = "finance-quote-to-invoice"      # builds the pro forma
SEND_SKILL_KEY = "finance-invoice-sending"  # drafts the cover email that carries it
OUT_DIR = "/opt/coretex/quotations"          # beside the quotation PDFs (persisted, cortex-owned)

DEFAULTS = {
    "prefix": "PI",
    "seq": "",          # the number sequence's name; brands that invoice as ONE legal entity share one (empty = the slug)
    "description": "{pct}% {stage} against quotation {number} for {work}",
    "work": "media production",
    "first_stage": "down payment",
    "middle_stage": "stage payment",
    "final_stage": "final payment",
    "due_days": 0,
    "footer": "This is a pro forma invoice and not a tax invoice.",
}

_PCT_LINE = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)\s*%\s*(.*)$")


# --------------------------------------------------------------------------- settings from the skill craft

def plan(company_id: int) -> dict:
    """The editable PROFORMA = {...} line on the company's finance-quote-to-invoice craft, over the defaults."""
    out = dict(DEFAULTS)
    try:
        row = db.one("select craft from skills where company_id=%s and skill_key=%s", (company_id, SKILL_KEY)) or {}
        m = re.search(r"^\s*PROFORMA\s*=\s*(\{.*\})\s*$", row.get("craft") or "", re.M)
        if m:
            out.update({k: v for k, v in (json.loads(m.group(1)) or {}).items() if v not in (None, "")})
    except Exception:  # noqa: BLE001 - a malformed line falls back to the defaults, never breaks an issue
        pass
    return out


# --------------------------------------------------------------------------- the stored quotation

def entry_for(number: str, version: int | None = None) -> dict | None:
    reg = db.setting_get(f"quote_versions:{number}") or []
    if not reg:
        return None
    if version:
        return next((e for e in reg if int(e.get("v") or 0) == int(version)), None)
    return reg[-1]


def net_of(entry: dict) -> float:
    """The version's value BEFORE VAT, summed by code from its stored lines (agency fee included when the
    preset carries one). The same sum `pipeline.record_quotation_sent` sets a deal's value from."""
    spec = (entry or {}).get("spec") or {}
    net = 0.0
    for s in spec.get("sections") or []:
        for it in s.get("items") or []:
            try:
                if it.get("unit") not in (None, ""):
                    net += float(it["unit"]) * float(it.get("qty") or 1)
            except (TypeError, ValueError):
                continue
    try:
        if (quotation.presets().get(spec.get("preset")) or {}).get("agency_fee"):
            net += round(net * 0.15, 2)
    except Exception:  # noqa: BLE001
        pass
    return round(net, 2)


def payment_lines_of(entry: dict) -> list[str]:
    """The payment lines that version PRINTED: recorded on the entry from 21 Sep 2026; for older entries,
    what the renderer would have printed (the preset's lines, else the house default)."""
    if (entry or {}).get("payment"):
        return list(entry["payment"])
    spec = (entry or {}).get("spec") or {}
    try:
        pl = (quotation.presets().get(spec.get("preset")) or {}).get("payment_lines")
    except Exception:  # noqa: BLE001
        pl = None
    return list(pl or quotation.DEFAULT_PAYMENT_LINES)


def splits_of(entry: dict) -> list[dict]:
    """[{pct, due}] parsed by code from the printed payment lines (a line that opens with a percentage is a
    stage). Refuses anything that does not come to 100%: the owner states the split instead."""
    out = []
    for ln in payment_lines_of(entry):
        m = _PCT_LINE.match(str(ln))
        if m:
            out.append({"pct": float(m.group(1)), "due": m.group(2).strip().rstrip(".")})
    total = sum(x["pct"] for x in out)
    if not out or not 99 <= total <= 101:
        raise ValueError(f"the payment lines on this quotation do not read as stages that total 100% "
                         f"(found {total:g}%): {payment_lines_of(entry)}")
    return out


def vat_rate(data: dict) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", str((data or {}).get("vat") or "5"))
    return float(m.group(1)) / 100 if m else 0.05


def gross_of(entry: dict, data: dict) -> float:
    n = net_of(entry)
    return round(n + round(n * vat_rate(data), 2), 2)


def deal_quotations(deal_id: int) -> list[dict]:
    """The quotation numbers that belong to a deal, with the versions actually SENT (timeline facts)."""
    nums: dict[str, set] = {}
    d = db.one("select history from crm_projects where id=%s", (int(deal_id),)) or {}
    for ev in d.get("history") or []:
        m = re.match(r"quotation-sent:([A-Z]{2,5}-\d{4}-\d{3,5}):v(\d+)", str(ev.get("ref") or ""))
        if m and not ev.get("voided"):
            nums.setdefault(m.group(1), set()).add(int(m.group(2)))
    for r in db.query("select request->>'number' n from tasks where kind='quotation' and deal_id=%s and "
                      "status in ('awaiting_approval','done')", (int(deal_id),)):
        if r.get("n"):
            nums.setdefault(r["n"], set())
    return [{"number": n, "sent": sorted(v)} for n, v in nums.items() if db.setting_get(f"quote_versions:{n}")]


# --------------------------------------------------------------------------- reading a returned PDF

_READ_SYS = (
    "You are checking a PDF that a client emailed to a production company. Decide whether it is a copy of one "
    "of the company's own QUOTATIONS that the client has SIGNED and returned. Report only what is visibly on the "
    "pages; never guess. A typed name alone is not a signature: a signature is handwriting, a drawn or pasted "
    "signature image, or an e-signature mark in the acceptance area. JSON keys: is_quotation (bool), "
    "quotation_number (string or null, exactly as printed), total_due_seen (number or null: the TOTAL DUE "
    "figure printed on it, including VAT), acceptance_filled (bool: any of client name / position / date "
    "filled in), signature_visible (bool), stamp_visible (bool: a company stamp or seal), signer_name, "
    "signer_position, signed_date (strings or null, as written), handwritten_changes (bool: any figure, line "
    "or term struck out, amended or annotated by hand), changes_note (string, empty when none).")


def worth_reading(filename: str, text: str, size: int) -> bool:
    """Cheap code gate before any model call: a scan (no readable text) or a PDF that names a quotation."""
    if size > 12_000_000:
        return False
    t = (text or "")
    if len(t.strip()) < 200:
        return True                      # a scan or photo-PDF: exactly what a signed return looks like
    return bool(re.search(r"quotation|acceptance|[A-Z]{2,5}-\d{4}-\d{3,5}", t + " " + (filename or ""), re.I))


def read_returned(pdf: bytes, company_slug: str | None = None) -> dict:
    from . import provider
    url = "data:application/pdf;base64," + base64.b64encode(pdf).decode()
    return provider.think_json(_READ_SYS, "Check the attached PDF.", model=provider.MODEL_ROUTER,
                               max_tokens=900, purpose="proforma-signed-check", company=company_slug,
                               cache=True, images=[url]) or {}


def match_signed(deal_id: int, seen: dict, data: dict) -> dict | None:
    """Which STORED version did they sign? The number is matched to the deal's own quotations (or, when the
    scan's number is unreadable, the deal's single quotation); the version is the one whose total equals the
    total on the returned copy, else the last one sent. Pure code."""
    quotes = deal_quotations(deal_id)
    if not quotes:
        return None
    seen_num = re.sub(r"\s+", "", str(seen.get("quotation_number") or "")).upper()
    q = next((x for x in quotes if x["number"].upper() == seen_num), None)
    flags = []
    if not q:
        if len(quotes) != 1:
            return None
        q = quotes[0]
        flags.append(f"the number on the returned copy could not be read ({seen.get('quotation_number') or 'none'}); "
                     f"matched to this opportunity's only quotation")
    reg = db.setting_get(f"quote_versions:{q['number']}") or []
    last_sent = max(q["sent"]) if q["sent"] else int(reg[-1].get("v") or 1)
    entry, how = None, ""
    try:
        tot = float(seen.get("total_due_seen")) if seen.get("total_due_seen") not in (None, "") else None
    except (TypeError, ValueError):
        tot = None
    if tot:
        hits = [e for e in reg if abs(gross_of(e, data) - tot) < 1.0]
        if hits:
            entry = next((e for e in hits if int(e.get("v") or 0) == last_sent), hits[-1])
            how = "total on the signed copy matches"
    if not entry:
        entry = next((e for e in reg if int(e.get("v") or 0) == last_sent), reg[-1])
        how = "last version sent"
        flags.append("the total on the returned copy " + (f"({tot:,.2f}) matches no stored version"
                                                         if tot else "could not be read")
                     + "; check it against the last version sent")
    v = int(entry.get("v") or 1)
    if q["sent"] and v != max(q["sent"]):
        flags.append(f"they signed v{v}, but v{max(q['sent'])} was the last version sent")
    if q["sent"] and v not in q["sent"]:
        flags.append(f"v{v} is not recorded as sent on this opportunity")
    if seen.get("handwritten_changes"):
        flags.append("handwritten changes on the signed copy: " + (str(seen.get("changes_note") or "")[:200] or "see the file"))
    if not seen.get("signature_visible"):
        flags.append("no signature could be seen" + (" (a stamp is visible)" if seen.get("stamp_visible") else ""))
    return {"number": q["number"], "v": v, "how": how, "flags": flags, "entry": entry}


# --------------------------------------------------------------------------- numbering + registry

def _next_number(prefix: str, slug: str) -> str:
    row = db.execute(
        "insert into settings (key, value) values (%s, '1'::jsonb) on conflict (key) do update "
        "set value = to_jsonb(((settings.value #>> '{}')::int) + 1) returning value", (f"proforma_seq:{slug}",))
    n = int(row["value"]) if row else 1
    return f"{prefix}-{datetime.date.today().year}-{n:04d}"


def issued(number: str) -> list[dict]:
    return [p for p in (db.setting_get(f"proformas:{number}") or []) if p.get("status") != "void"]


def for_deal(deal_id: int) -> list[dict]:
    """Every live pro forma raised on a deal, across its quotation numbers (low volume: a settings scan)."""
    out = []
    for r in db.query("select value from settings where key like 'proformas:%%'"):
        out += [p for p in (r.get("value") or []) if p.get("deal_id") == int(deal_id) and p.get("status") != "void"]
    return out


def _record(number: str, row: dict) -> None:
    reg = db.setting_get(f"proformas:{number}") or []
    reg.append(row)
    db.setting_set(f"proformas:{number}", reg)


def update_record(number: str, pi: str, **fields) -> None:
    reg = db.setting_get(f"proformas:{number}") or []
    for p in reg:
        if p.get("pi") == pi:
            p.update(fields)
    db.setting_set(f"proformas:{number}", reg)


def stage_label(pl: dict, idx: int, n: int) -> str:
    if idx == 0:
        return pl["first_stage"]
    return pl["final_stage"] if idx == n - 1 else pl["middle_stage"]


# --------------------------------------------------------------------------- issue + render

def issue(company: dict, data: dict, number: str, *, version: int | None = None, stage: int | None = None,
          deal_id: int | None = None, billed_to: list | None = None, out_dir: str = OUT_DIR) -> dict:
    """Issue ONE pro forma for one payment stage of a stored quotation version. `stage` is 1-based; omitted =
    the first stage with no live pro forma. A stage is never invoiced twice. Returns the registry row + path."""
    entry = entry_for(number, version)
    if not entry:
        raise ValueError(f"no stored quotation {number}" + (f" v{version}" if version else ""))
    net = net_of(entry)
    if net <= 0:
        raise ValueError(f"quotation {number} v{entry.get('v')} has no priced lines to invoice against")
    splits = splits_of(entry)
    live = issued(number)
    if stage is None:
        done = {int(p.get("stage") or 0) for p in live}
        stage = next((i + 1 for i in range(len(splits)) if i + 1 not in done), None)
        if stage is None:
            raise ValueError(f"every payment stage of {number} already has a pro forma: "
                             + ", ".join(p["pi"] for p in live))
    if not 1 <= int(stage) <= len(splits):
        raise ValueError(f"{number} has {len(splits)} payment stage(s); stage {stage} does not exist")
    dup = next((p for p in live if int(p.get("stage") or 0) == int(stage)), None)
    if dup:
        raise ValueError(f"stage {stage} of {number} was already invoiced as {dup['pi']} on {dup.get('date')}")
    sp = splits[int(stage) - 1]
    pl = plan(company["id"])
    spec = entry.get("spec") or {}
    rate = vat_rate(data)
    amount = round(net * sp["pct"] / 100, 2)
    vat = round(amount * rate, 2)
    today = datetime.date.today()
    row = {"pi": _next_number(pl["prefix"], pl.get("seq") or company.get("slug") or ""), "number": number,
           "v": int(entry.get("v") or 1), "stage": int(stage), "stages": len(splits), "pct": sp["pct"],
           "due_text": sp["due"], "net": net, "amount": amount, "vat": vat, "total": round(amount + vat, 2),
           "currency": (data.get("currency") or "AED").upper(), "customer": spec.get("customer") or "",
           "date": today.isoformat(), "due": (today + datetime.timedelta(days=int(pl.get("due_days") or 0))).isoformat(),
           "deal_id": int(deal_id) if deal_id else None, "status": "issued"}
    row["description"] = pl["description"].format(
        pct=f"{sp['pct']:g}", stage=stage_label(pl, int(stage) - 1, len(splits)), number=number, work=pl["work"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"proforma-{company.get('slug')}-{row['pi']}.pdf")
    render(company, data, row, billed_to or [row["customer"]], pl, path)
    _record(number, row)
    return {**row, "path": path}


def _e(s) -> str:
    return _html.escape(str(s or ""))


def render(company: dict, data: dict, row: dict, billed_to: list, pl: dict, path: str) -> str:
    """The simple invoice layout the owner approved (21 Sep 2026): logo, our legal block, PRO FORMA INVOICE
    centred, billed-to (the company, never a person), one description line, totals, bank details."""
    from . import deck
    logo = ((data.get("brand") or {}).get("logo_dark_b64") or "")
    if logo and not logo.startswith("data:"):
        logo = "data:image/png;base64," + logo
    reg = data.get("registration") or ""
    trn = reg.split("VAT")[-1].strip() if "VAT" in reg else ""
    legal = data.get("quote_header_name") or company.get("name") or ""
    addr = [x.strip() for x in (data.get("address") or "").replace("\n", ",").split(",") if x.strip()]
    bank = [x.strip() for x in (data.get("bank_details") or "").replace("\n", " - ").split(" - ") if x.strip()]
    cur = row["currency"]
    m = lambda v: f"{v:,.2f}"  # noqa: E731
    d = lambda iso: datetime.date.fromisoformat(iso).strftime("%d %b %Y")  # noqa: E731
    doc = f"""<html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 18mm 17mm; }}
body {{ font-family: Inter, Arial, sans-serif; font-size: 10pt; color: #1A1A1A; line-height: 1.45; }}
.top {{ display: flex; justify-content: space-between; align-items: flex-start; }}
.logo {{ background: #0A0A0A; padding: 12px 18px; display: inline-block; }}
.logo img {{ height: 30px; display: block; }}
.us {{ text-align: right; }} .us b {{ font-weight: 600; }}
h1 {{ font-family: Poppins, sans-serif; font-weight: 700; font-size: 19pt; letter-spacing: 2px; text-align: center;
     margin: 30px 0 34px; }}
.meta {{ display: flex; justify-content: space-between; }}
.lab {{ color: #0A7C8C; font-size: 9.5pt; margin-bottom: 2px; }}
.meta .blk {{ margin-bottom: 10px; }}
.due {{ text-align: right; }}
.due .big {{ font-family: Poppins, sans-serif; font-size: 22pt; font-weight: 500; line-height: 1.1; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 34px; }}
th {{ color: #0A7C8C; font-weight: 400; font-size: 9.5pt; text-align: right; padding: 10px 0;
     border-top: 2.5px solid #0E2A30; }}
th:first-child, td:first-child {{ text-align: left; }}
td {{ text-align: right; padding: 12px 0; border-bottom: 1px solid #D5DADC; vertical-align: top; }}
td small {{ display: block; font-size: 8pt; }}
.tot {{ width: 50%; margin-left: 50%; margin-top: 26px; }}
.tot div {{ display: flex; justify-content: space-between; padding: 3px 0; }}
.tot span:first-child {{ flex: 1; text-align: right; padding-right: 60px; }}
.tot .line {{ border-top: 1px solid #D5DADC; margin-top: 8px; padding-top: 10px; }}
.tot .final {{ border-top: 3px double #D5DADC; margin-top: 8px; padding-top: 10px; }}
.tot .final span:first-child {{ color: #0A7C8C; }}
.bank {{ margin-top: 44px; font-size: 9.5pt; }}
.foot {{ position: fixed; bottom: 0; left: 0; right: 0; font-size: 8.5pt; color: #5F6B70; text-align: center; }}
</style></head><body>
<div class="top">
  <div class="logo">{f'<img src="{logo}">' if logo else _e(company.get("name"))}</div>
  <div class="us"><b>{_e(legal)}</b><br>{("TRN " + _e(trn) + "<br>") if trn else ""}{"<br>".join(_e(a) for a in addr)}</div>
</div>
<h1>PRO FORMA INVOICE</h1>
<div class="meta">
  <div><div class="lab">Billed To</div>{"<br>".join(_e(x) for x in billed_to if x)}</div>
  <div><div class="blk"><div class="lab">Date of Issue</div>{d(row["date"])}</div>
       <div class="blk"><div class="lab">Due Date</div>{d(row["due"])}</div></div>
  <div><div class="blk"><div class="lab">Pro Forma Number</div>{_e(row["pi"])}</div>
       <div class="blk"><div class="lab">Quotation</div>{_e(row["number"])}</div></div>
  <div class="due"><div class="lab">Amount Due ({cur})</div><div class="big">{m(row["total"])}</div></div>
</div>
<table>
  <tr><th>Description</th><th style="width:18%">Rate</th><th style="width:10%">Qty</th><th style="width:18%">Line Total</th></tr>
  <tr><td>{_e(row["description"])}</td><td>{m(row["amount"])}<small>+VAT</small></td><td>1</td><td>{m(row["amount"])}</td></tr>
</table>
<div class="tot">
  <div><span>Subtotal</span><span>{m(row["amount"])}</span></div>
  <div><span>VAT ({vat_rate(data) * 100:g}%)</span><span>{m(row["vat"])}</span></div>
  <div class="line"><span>Total</span><span>{m(row["total"])}</span></div>
  <div class="final"><span>Amount Due ({cur})</span><span>{m(row["total"])}</span></div>
</div>
<div class="bank"><div class="lab">Bank Details</div>{"<br>".join(_e(b) for b in bank)}</div>
<div class="foot">{_e(pl.get("footer"))}</div>
</body></html>"""
    return deck.to_pdf(doc, path)
