"""The TAX INVOICE (owner, 21 Oct 2026).

Cortex had no tax invoice. A next-step card titled "Tax invoice for quotation SEN-2026-0021" was read as a quotation
request, and approving it built quotation SEN-2026-0055 with the acceptance block and all 24 terms. A tax invoice is a
statement of a supply, not an offer: no signature, no terms, no validity date.

What it carries is what UAE VAT law asks of a tax invoice (Executive Regulation, Art. 59): the words TAX INVOICE, the
supplier's name, address and TRN, the recipient's name and address (and TRN when they are VAT registered), a
sequential invoice number, the date of issue (and the date of supply when it differs), a description of the supply
with quantity and unit price, the VAT rate and amount, and the totals in AED. Plus our bank details, so it can be paid.

EVERY FIGURE COMES FROM THE SIGNED QUOTATION'S STORED VERSION (`quote_versions:<number>`), never from a model; the
invoice number from a sequence the owner seeds (`tax_invoice_seq:<entity>` = {"prefix": ..., "next": ...}): a tax
invoice number is a legal record and the accountant's run must not be collided with, so nothing is issued until it
is set. The layout is the approved pro forma layout (proforma.render), retitled and extended."""
from __future__ import annotations

import datetime
import html as _html
import os

from . import db, proforma

OUT = "/opt/coretex/quotations"


def _e(s) -> str:
    return _html.escape(str(s or ""))


def lines_of(entry: dict) -> list[dict]:
    """The supply, line by line, from the quotation version: description, qty, unit, line total (excl. VAT)."""
    out = []
    for s in ((entry or {}).get("spec") or {}).get("sections") or []:
        for it in s.get("items") or []:
            try:
                q, u = float(it.get("qty") or 1), float(it.get("unit") or 0)
            except (TypeError, ValueError):
                continue
            if u:
                out.append({"desc": str(it.get("desc") or it.get("item") or "").strip(), "qty": q, "unit": u,
                            "total": round(q * u, 2)})
    return out


def next_number(entity: str, claim: bool = False) -> str | None:
    """The next tax invoice number, or None while the owner has not seeded the sequence."""
    key = f"tax_invoice_seq:{entity}"
    seq = db.setting_get(key) or {}
    if not seq.get("prefix") and not seq.get("next"):
        return None
    n = int(seq.get("next") or 1)
    num = f"{seq.get('prefix') or ''}{n:0{int(seq.get('width') or 4)}d}"
    if claim:
        db.setting_set(key, {**seq, "next": n + 1})
    return num


def render(company: dict, data: dict, inv: dict, path: str) -> str:
    from . import deck
    logo = ((data.get("brand") or {}).get("logo_dark_b64") or "")
    if logo and not logo.startswith("data:"):
        logo = "data:image/png;base64," + logo
    reg = data.get("registration") or ""
    trn = reg.split("VAT")[-1].strip() if "VAT" in reg else ""
    legal = data.get("quote_header_name") or company.get("name") or ""
    addr = [x.strip() for x in (data.get("address") or "").replace("\n", ",").split(",") if x.strip()]
    addr_lines = [", ".join(addr[:2]), ", ".join(addr[2:])] if len(addr) > 2 else addr
    bank = [x.strip() for x in (data.get("bank_details") or "").replace("\n", " - ").split(" - ") if x.strip()]
    cur, rate = inv["currency"], inv["vat_rate"]
    m = lambda v: f"{v:,.2f}"  # noqa: E731
    d = lambda iso: datetime.date.fromisoformat(iso).strftime("%d %b %Y")  # noqa: E731
    rows = "".join(
        f"<tr><td>{_e(x['desc'])}</td><td>{x['qty']:g}</td><td>{m(x['unit'])}</td><td>{rate * 100:g}%</td>"
        f"<td>{m(round(x['total'] * rate, 2))}</td><td>{m(x['total'])}</td></tr>" for x in inv["lines"])
    bill = [inv["billed_to"]] + list(inv.get("billed_address") or [])
    doc = f"""<html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 18mm 17mm 22mm; }}
body {{ font-family: Inter, Arial, sans-serif; font-size: 9.5pt; color: #1A1A1A; line-height: 1.45; }}
.top {{ display: flex; justify-content: space-between; align-items: flex-start; }}
.logo {{ background: #0A0A0A; padding: 12px 18px; display: inline-block; }}
.logo img {{ height: 30px; display: block; }}
.us {{ text-align: right; }} .us b {{ font-weight: 600; }}
h1 {{ font-family: Poppins, sans-serif; font-weight: 700; font-size: 19pt; letter-spacing: 2px; text-align: center;
     margin: 18px 0 20px; }}
.meta {{ display: flex; justify-content: space-between; gap: 18px; }}
.lab {{ color: #0A7C8C; font-size: 9pt; margin-bottom: 2px; }}
.meta .blk {{ margin-bottom: 9px; }}
.due {{ text-align: right; }}
.due .big {{ font-family: Poppins, sans-serif; font-size: 20pt; font-weight: 500; line-height: 1.1; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
th {{ color: #0A7C8C; font-weight: 400; font-size: 9pt; text-align: right; padding: 9px 0 9px 8px;
     border-top: 2.5px solid #0E2A30; }}
th:first-child, td:first-child {{ text-align: left; padding-left: 0; }}
td {{ text-align: right; padding: 7px 0 7px 8px; border-bottom: 1px solid #D5DADC; vertical-align: top; }}
.tot {{ width: 52%; margin-left: 48%; margin-top: 14px; }}
.tot div {{ display: flex; justify-content: space-between; padding: 3px 0; }}
.tot span:first-child {{ flex: 1; text-align: right; padding-right: 50px; }}
.tot .line {{ border-top: 1px solid #D5DADC; margin-top: 7px; padding-top: 9px; }}
.tot .final {{ border-top: 3px double #D5DADC; margin-top: 7px; padding-top: 9px; font-weight: 600; }}
.tot .final span:first-child {{ color: #0A7C8C; }}
.bank {{ margin-top: 18px; font-size: 9pt; }}
.ref {{ margin-top: 14px; font-size: 9pt; color: #3C474B; }}
.foot {{ position: fixed; bottom: -10mm; left: 0; right: 0; font-size: 8pt; color: #5F6B70; text-align: center; }}
</style></head><body>
<div class="top">
  <div class="logo">{f'<img src="{logo}">' if logo else _e(company.get("name"))}</div>
  <div class="us"><b>{_e(legal)}</b><br>{("TRN " + _e(trn) + "<br>") if trn else ""}{"<br>".join(_e(a) for a in addr_lines)}</div>
</div>
<h1>TAX INVOICE</h1>
<div class="meta">
  <div><div class="lab">Billed To</div>{"<br>".join(_e(x) for x in bill if x)}
       <div style="margin-top:4px">{("TRN " + _e(inv["billed_trn"])) if inv.get("billed_trn") else ""}</div></div>
  <div><div class="blk"><div class="lab">Invoice Number</div>{_e(inv["no"])}</div>
       <div class="blk"><div class="lab">Date of Issue</div>{d(inv["date"])}</div>
       {f'<div class="blk"><div class="lab">Date of Supply</div>{d(inv["supply_date"])}</div>' if inv.get("supply_date") else ""}</div>
  <div><div class="blk"><div class="lab">Quotation</div>{_e(inv["quotation"])}</div>
       <div class="blk"><div class="lab">Due Date</div>{d(inv["due"])}</div></div>
  <div class="due"><div class="lab">Amount Due ({cur})</div><div class="big">{m(inv["total"])}</div></div>
</div>
<table>
  <tr><th>Description</th><th style="width:7%">Qty</th><th style="width:14%">Unit price</th><th style="width:8%">VAT</th>
      <th style="width:12%">VAT amount</th><th style="width:15%">Amount (excl. VAT)</th></tr>
  {rows}
</table>
<div class="tot">
  <div><span>Total excluding VAT</span><span>{m(inv["net"])}</span></div>
  <div><span>VAT ({rate * 100:g}%)</span><span>{m(inv["vat"])}</span></div>
  <div class="line"><span>Total including VAT</span><span>{m(inv["total"])}</span></div>
  {f'<div><span>Paid</span><span>{m(inv["paid"])}</span></div>' if inv.get("paid") else ""}
  <div class="final"><span>Amount Due ({cur})</span><span>{m(inv["due_amount"])}</span></div>
</div>
<div class="ref">{_e(inv.get("note") or "")}</div>
<div class="bank"><div class="lab">Bank Details</div>{" &middot; ".join(_e(b) for b in bank[:2])}<br>{" &middot; ".join(_e(b) for b in bank[2:])}</div>
<div class="foot">{_e(legal)} · TRN {_e(trn)}</div>
</body></html>"""
    return deck.to_pdf(doc, path)


def build(company: dict, number: str, *, version: int | None = None, billed_to: str = "", billed_address=None,
          billed_trn: str = "", invoice_no: str | None = None, paid: float = 0.0, due_days: int = 0,
          supply_date: str = "", out_path: str | None = None, sample: bool = False) -> dict:
    """A tax invoice for the whole supply on a quotation version. `sample` renders without claiming a number."""
    data = (db.one("select data from company_profiles where company_id=%s", (company["id"],)) or {}).get("data") or {}
    reg = db.setting_get(f"quote_versions:{number}") or []
    entry = next((e for e in reg if int(e.get("v") or 0) == int(version)), None) if version else (reg[-1] if reg else None)
    if not entry:
        raise ValueError(f"no stored version of quotation {number}")
    lines = lines_of(entry)
    net = round(sum(x["total"] for x in lines), 2)
    if abs(net - float(proforma.net_of(entry))) > 0.05:
        raise ValueError(f"the lines of {number} v{entry.get('v')} do not add up to its total; nothing issued")
    rate = proforma.vat_rate(data)
    vat = round(net * rate, 2)
    entity = (proforma.plan(company) or {}).get("seq") or company.get("slug") or ""
    if not invoice_no:
        invoice_no = "To be assigned" if sample else next_number(entity, claim=True)
        if not invoice_no:
            raise ValueError("The tax invoice number run is not set yet. Give Cortex the prefix and the next number "
                             f"(setting tax_invoice_seq:{entity}) so it never collides with the accountant's invoices.")
    today = datetime.date.today()
    inv = {"no": invoice_no, "date": today.isoformat(), "due": (today + datetime.timedelta(days=due_days)).isoformat(),
           "supply_date": supply_date, "quotation": f"{number} v{entry.get('v')}", "lines": lines, "net": net,
           "vat": vat, "total": round(net + vat, 2), "paid": round(paid, 2),
           "due_amount": round(net + vat - paid, 2), "vat_rate": rate,
           "currency": (data.get("currency") or "AED").upper(),
           "billed_to": billed_to or (entry.get("spec") or {}).get("customer") or "",
           "billed_address": billed_address or [], "billed_trn": billed_trn,
           "note": f"Supply as agreed under quotation {number} v{entry.get('v')}."}
    path = out_path or os.path.join(OUT, f"tax-invoice-{company.get('slug')}-{invoice_no}.pdf".replace(" ", "-"))
    render(company, data, inv, path)
    return {**inv, "path": path}
