"""THE COMPARISON DECK: its own format, not the retainer deck (owner, 28 Oct 2026).

"Literally a split: left-hand side is AI production, right-hand side is traditional production ... cost
comparison, benefits comparison, the process, timeline, speed, quality, everything ... a completely
different format for this comparison type deck than the retainer deck. Don't copy the retainer format."

Every page is one page cut down a centre SPINE. The left side always speaks for `sides.left` in its colour,
the right side for `sides.right` in its own, and the two labels sit at the top of every page, so a reader
who opens the deck anywhere knows which side is which. Generic by design: AI vs shoot is the first pair,
but any two ways of doing a job (in-house vs agency, one-off vs retainer) render the same way.

Like deck.render_spec there is NO model in here. The words and the numbers arrive written (the numbers
priced by code from the rate card by the caller); this file only lays them out. Shared plumbing (fonts,
logo, icons, thumbnails, the film library, PDF) comes from deck.py so the two formats stay one brand.
"""
from __future__ import annotations

import os

from . import store
from .deck import MONO, SITE, _b64, _esc, _hl, _logo, film_caption, films_by_ids, thumbnail, to_pdf

W, H, MID = 1280, 720, 640
LX, RX, HALF = 72, 680, 528            # left panel x, right panel x, panel width (72..600 | 680..1208)
TOP = 188                              # where the body starts under the centred heading

_CSS = """
@page { size: 1280px 720px; margin: 0; }
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: Inter, sans-serif; background:#0A0A0A; color:#EDEDF2; }
.pg { width:1280px; height:720px; position:relative; page-break-after:always; background:#0A0A0A; overflow:hidden; }
.abs { position:absolute; }
h1,h2 { font-family:Poppins, sans-serif; color:#F4F4F5; }
h1 { font-size:56px; font-weight:800; letter-spacing:-1.6px; line-height:1.04; }
h2 { font-size:40px; font-weight:800; letter-spacing:-1.2px; line-height:1.08; }
p { font-size:14px; line-height:1.55; color:#C4C4CC; font-weight:300; }
.foot { position:absolute; bottom:22px; left:72px; right:72px; font-size:10.5px; color:#5A5A62; letter-spacing:1.5px; }
.foot img { height:19px; }
.big { font-family:Poppins, sans-serif; font-weight:700; letter-spacing:-.8px; line-height:1; }
.li { position:relative; padding-left:22px; margin-bottom:9px; font-size:13.5px; line-height:1.45; color:#D6D6DC; font-weight:300; }
.li .mk { position:absolute; left:0; top:0; font-weight:600; }
.lb { font-size:16.5px; line-height:1.4; margin-bottom:15px; }
.cap { font-size:12px; line-height:1.4; color:#B4B4BC; margin-top:7px; }
.cap b { color:#EDEDF2; font-weight:600; }
a { color:inherit; text-decoration:none; }
"""


class _Versus:
    def __init__(self, company: dict, sides: dict, label: str):
        self.co, self.logo, self.label, self.pages = company, _logo(company), label, []
        lt, rt = (sides or {}).get("left") or {}, (sides or {}).get("right") or {}
        self.L = {"label": lt.get("label") or "Option A", "c": lt.get("colour") or SITE["accent"]}
        self.R = {"label": rt.get("label") or "Option B", "c": rt.get("colour") or SITE["accent2"]}

    # ------------------------------------------------------------------ the frame every page shares
    def _frame(self, body: str, section: str, spine: bool = True, tint: bool = True) -> None:
        n = len(self.pages) + 1
        L, R = self.L, self.R
        # each half carries a faint wash of its own colour, so the split reads before a word is read
        wash = (f'<div class="abs" style="top:0;left:0;width:{MID}px;height:{H}px;background:{L["c"]};opacity:.035"></div>'
                f'<div class="abs" style="top:0;left:{MID}px;width:{MID}px;height:{H}px;background:{R["c"]};opacity:.035"></div>'
                ) if tint else ""
        rule = (f'<div class="abs" style="top:0;left:0;width:{MID}px;height:4px;background:{L["c"]}"></div>'
                f'<div class="abs" style="top:0;left:{MID}px;width:{MID}px;height:4px;background:{R["c"]}"></div>')
        tags = (f'<div class="abs" style="top:26px;left:{LX}px">{_tag(L["label"], L["c"])}</div>'
                f'<div class="abs" style="top:26px;right:72px;text-align:right">{_tag(R["label"], R["c"])}</div>')
        line = (f'<div class="abs" style="top:{TOP - 14}px;left:{MID}px;width:1px;height:{H - TOP - 46}px;'
                f'background:#2A2A31"></div>') if spine else ""
        lg = f'<img src="{self.logo}">' if self.logo else _esc(self.co.get("name"))
        foot = (f'<div class="foot"><div class="abs" style="left:0;top:0">{lg}</div>'
                f'<div style="text-align:center">{_esc(section)}</div>'
                f'<div class="abs" style="right:0;top:0">{_esc(self.label)} &middot; {n:02d}</div></div>')
        self.pages.append(f'<div class="pg">{wash}{rule}{tags}{line}{body}{foot}</div>')

    def _head(self, kicker: str, heading: str, intro: str = "") -> str:
        """Centred over the spine: the heading belongs to both sides."""
        return (f'<div class="abs" style="top:64px;left:120px;right:120px;text-align:center">'
                + _mono_c(kicker, SITE["muted"]) +
                f'<h2 style="margin-top:10px">{_hl(heading, "#FFFFFF")}</h2>'
                + (f'<p style="margin:10px auto 0;max-width:820px">{_esc(intro)}</p>' if intro else "")
                + '</div>')

    # ------------------------------------------------------------------ page types
    def cover(self, title: str, title2: str, standfirst: str, meta: list, left_image: str | None,
              right_image: str | None, section: str = "Comparison"):
        """Two pictures, one per side, meeting at the spine; the title runs across both."""
        L, R = self.L, self.R
        halves = ""
        for x, img, c in ((0, left_image, L["c"]), (MID, right_image, R["c"])):
            if img and os.path.isfile(img):
                halves += (f'<img class="abs" style="top:0;left:{x}px;width:{MID}px;height:{H}px;object-fit:cover" '
                           f'src="{_b64(img)}">')
            halves += f'<div class="abs" style="top:0;left:{x}px;width:{MID}px;height:{H}px;background:{c};opacity:.06"></div>'
        shade = (f'<div class="abs" style="top:0;left:0;width:{W}px;height:{H}px;background:linear-gradient('
                 'to top, rgba(10,10,10,.97) 30%, rgba(10,10,10,.55) 60%, rgba(10,10,10,.25))"></div>'
                 f'<div class="abs" style="top:0;left:{MID - 1}px;width:2px;height:{H}px;background:#0A0A0A"></div>')
        badge = _vs_badge(L["c"], R["c"], top=250)
        m = "".join(f'<div style="display:inline-block;margin:0 22px;text-align:center">'
                    + _mono_c(x.get("k"), SITE["muted"], 9) +
                    f'<div style="font-size:13.5px;color:#EDEDF2;margin-top:5px">{_esc(x.get("v"))}</div></div>'
                    for x in (meta or [])[:4])
        body = (halves + shade + badge +
                f'<div class="abs" style="top:396px;left:90px;right:90px;text-align:center">'
                f'<h1>{_esc(title)}</h1>'
                + (f'<h1><span style="color:{L["c"]}">{_esc(title2)}</span></h1>' if title2 else "")
                + f'<p style="margin:16px auto 0;max-width:780px;font-size:15px">{_esc(standfirst)}</p>'
                f'<div style="margin-top:22px">{m}</div></div>')
        self._frame(body, section, spine=False, tint=False)

    def spotlight(self, side: str, kicker: str, title: str, caption: str, image: str | None, href: str = "",
                  play: bool = True, section: str = "Watch"):
        """One film full frame, owned by one side: its colour edges the page and its label sits on it."""
        s = self.R if side == "right" else self.L
        img = (f'<img class="abs" style="top:0;left:0;width:{W}px;height:{H}px;object-fit:cover" src="{_b64(image)}">'
               if image and os.path.isfile(image) else "")
        shade = (f'<div class="abs" style="top:0;left:0;width:{W}px;height:{H}px;background:linear-gradient('
                 'to top, rgba(10,10,10,.94) 12%, rgba(10,10,10,.30) 46%, rgba(10,10,10,.05) 70%)"></div>')
        edge = f'<div class="abs" style="top:0;left:0;width:8px;height:{H}px;background:{s["c"]}"></div>'
        btn = ('' if not (play and img) else
               '<div class="abs" style="top:288px;left:568px;width:144px;height:144px;border-radius:72px;'
               'background:rgba(10,10,10,.55);border:3px solid rgba(255,255,255,.85)"></div>'
               '<div class="abs" style="top:318px;left:626px;width:0;height:0;border-top:42px solid transparent;'
               'border-bottom:42px solid transparent;border-left:66px solid rgba(255,255,255,.92)"></div>')
        text = (f'<div class="abs" style="bottom:70px;left:72px;right:200px">'
                + _tag(s["label"], s["c"]) + '<div style="height:12px"></div>' + _mono_c(kicker, "#B4B4BC", align="left") +
                f'<h2 style="margin-top:8px">{_esc(title)}</h2>'
                f'<p style="margin-top:10px;max-width:760px">{_esc(caption)}</p></div>')
        inner = img + shade + edge + btn + text
        if href and img:
            inner = f'<a href="{_esc(href)}">{inner}</a>'
        self._frame(inner, section, spine=False, tint=not img)

    def scorecard(self, kicker: str, heading: str, rows: list, note: str = "", section: str = "At a glance"):
        """Dimension by dimension. The dimension sits ON the spine, each side's answer faces it, and the side
        that wins a row carries a dot in its colour. A row can be won by either side: an honest scorecard
        is the only kind a client's management will believe."""
        L, R = self.L, self.R
        rs = (rows or [])[:9]
        step = min(52, int((H - TOP - 104) / max(1, len(rs))))
        out = []
        for i, r in enumerate(rs):
            y = TOP + 6 + i * step
            edge = (r.get("edge") or "").lower()
            ld = _dot(L["c"]) if edge == "left" else ""
            rd = _dot(R["c"]) if edge == "right" else ""
            lw = "#F4F4F5" if edge == "left" else "#B4B4BC"
            rw = "#F4F4F5" if edge == "right" else "#B4B4BC"
            out.append(
                f'<div class="abs" style="top:{y + step - 1}px;left:{LX}px;width:{W - 2 * LX}px;height:1px;background:#1C1C22"></div>'
                f'<div class="abs" style="top:{y + 8}px;left:{LX}px;width:410px;text-align:right;font-size:14px;'
                f'color:{lw};line-height:1.35">{ld}{_esc(r.get("left"))}</div>'
                f'<div class="abs" style="top:{y + 4}px;left:{MID - 108}px;width:216px;text-align:center">'
                f'<span style="display:inline-block;background:#141418;border:1px solid #2A2A31;border-radius:14px;'
                f'padding:5px 14px;font-family:{MONO};font-size:9.5px;letter-spacing:1.6px;text-transform:uppercase;'
                f'color:#EDEDF2">{_esc(r.get("label"))}</span></div>'
                f'<div class="abs" style="top:{y + 8}px;left:{RX + 118}px;width:410px;font-size:14px;'
                f'color:{rw};line-height:1.35">{_esc(r.get("right"))}{rd}</div>')
        legend = (f'<div class="abs" style="bottom:56px;left:{LX}px;right:72px;text-align:center;font-size:11.5px;'
                  f'color:#7A7A84">{_dot("#9A9AA0")}marks the side that comes out ahead on that line'
                  + (f'. {_esc(note)}' if note else "") + '</div>')
        self._frame(self._head(kicker, heading) + "".join(out) + legend, section, spine=False)

    def cost(self, kicker: str, heading: str, rows: list, totals: dict | None = None, note: str = "",
             currency: str = "AED", section: str = "Cost"):
        """Line by line as mirrored bars growing OUT from the spine: the longer side is the dearer side,
        visible before a figure is read. Amounts arrive priced (by ratecard.price_lines); a zero prints
        'Not needed' rather than a bar."""
        L, R = self.L, self.R
        rs = (rows or [])[:10]
        top = max([float(r.get("left") or 0) for r in rs] + [float(r.get("right") or 0) for r in rs] + [1])
        span, lab = 380, 210
        step = min(36, int((H - TOP - 196) / max(1, len(rs))))
        out = []
        for i, r in enumerate(rs):
            y = TOP + i * step
            out.append(f'<div class="abs" style="top:{y + 5}px;left:{MID - lab // 2}px;width:{lab}px;text-align:center;'
                       f'font-size:12px;color:#D6D6DC;line-height:1.25">{_esc(r.get("label"))}</div>')
            for side, c, val in (("l", L["c"], float(r.get("left") or 0)), ("r", R["c"], float(r.get("right") or 0))):
                w = int(span * val / top) if val else 0
                amt = _money(val, currency) if val else "Not needed"
                col = "#EDEDF2" if val else "#5A5A62"
                if side == "l":
                    x0 = MID - lab // 2 - 10
                    out.append(f'<div class="abs" style="top:{y + 6}px;left:{x0 - w}px;width:{max(w, 2)}px;height:{step - 14}px;'
                               f'background:{c if val else "#2A2A31"};opacity:{.9 if val else 1};border-radius:3px 0 0 3px"></div>'
                               f'<div class="abs" style="top:{y + 6}px;left:{x0 - w - 130}px;width:122px;text-align:right;'
                               f'font-size:12.5px;color:{col};font-weight:500">{amt}</div>')
                else:
                    x0 = MID + lab // 2 + 10
                    out.append(f'<div class="abs" style="top:{y + 6}px;left:{x0}px;width:{max(w, 2)}px;height:{step - 14}px;'
                               f'background:{c if val else "#2A2A31"};opacity:{.9 if val else 1};border-radius:0 3px 3px 0"></div>'
                               f'<div class="abs" style="top:{y + 6}px;left:{x0 + w + 8}px;width:122px;'
                               f'font-size:12.5px;color:{col};font-weight:500">{amt}</div>')
        t = totals or {}
        lt = sum(float(r.get("left") or 0) for r in rs)
        rt = sum(float(r.get("right") or 0) for r in rs)
        yb = TOP + len(rs) * step + 18
        diff = abs(rt - lt)
        cheaper = L if lt < rt else R
        pct = round(diff / max(lt, rt) * 100) if max(lt, rt) else 0
        tot = (f'<div class="abs" style="top:{yb}px;left:{LX}px;width:{W - 2 * LX}px;height:1px;background:#2A2A31"></div>'
               f'<div class="abs" style="top:{yb + 16}px;left:{LX}px;width:400px;text-align:right">'
               + _mono_c(t.get("left_label") or "Total", L["c"], 9.5, align="right") +
               f'<div class="big" style="font-size:44px;color:{L["c"]};margin-top:6px">{_money(lt, currency)}</div></div>'
               f'<div class="abs" style="top:{yb + 16}px;left:{RX + 128}px;width:400px">'
               + _mono_c(t.get("right_label") or "Total", R["c"], 9.5, align="left") +
               f'<div class="big" style="font-size:44px;color:{R["c"]};margin-top:6px">{_money(rt, currency)}</div></div>'
               f'<div class="abs" style="top:{yb + 22}px;left:{MID - 104}px;width:208px;text-align:center;'
               f'border:1px solid {cheaper["c"]};border-radius:10px;padding:10px 6px;background:#0E0E11">'
               + _mono_c(t.get("diff_label") or f"Saving, {pct}%", "#9A9AA0", 8.5) +
               f'<div class="big" style="font-size:22px;color:#F4F4F5;margin-top:5px">{_money(diff, currency)}</div></div>')
        nt = (f'<div class="abs" style="bottom:52px;left:{LX}px;right:72px;text-align:center;font-size:11px;'
              f'color:#7A7A84;line-height:1.45">{_esc(note)}</div>') if note else ""
        self._frame(self._head(kicker, heading) + "".join(out) + tot + nt, section, spine=False)

    def process(self, kicker: str, heading: str, left_steps: list, right_steps: list, left_foot: str = "",
                right_foot: str = "", section: str = "Process"):
        """Two lanes, one per side, numbered in order. A step marked `wait` is a stretch that depends on
        someone else (a permit, an approval, the weather) and is drawn as one, because that is where a
        timeline is really lost."""
        lanes = []
        for x, steps, s, foot, align in ((LX, left_steps, self.L, left_foot, "right"),
                                         (RX, right_steps, self.R, right_foot, "left")):
            st = (steps or [])[:10]
            step = min(38, int((H - TOP - 150) / max(1, len(st))))
            rows = []
            for i, p in enumerate(st):
                y = TOP + i * step
                wait = bool(p.get("wait"))
                num = (f'<span style="display:inline-block;width:24px;height:24px;border-radius:12px;text-align:center;'
                       f'line-height:24px;font-size:11px;font-weight:600;'
                       + (f'border:1px dashed {s["c"]};color:{s["c"]}' if wait else f'background:{s["c"]};color:#0A0A0A')
                       + f'">{i + 1}</span>')
                txt = (f'<b style="color:#F4F4F5;font-weight:600;font-size:13.5px">{_esc(p.get("title"))}</b>'
                       + (f' <span style="color:#8A8A92;font-size:12px">{_esc(p.get("body"))}</span>' if p.get("body") else ""))
                if align == "right":
                    rows.append(f'<div class="abs" style="top:{y}px;left:{x}px;width:{HALF - 40}px;text-align:right;'
                                f'line-height:24px">{txt}</div>'
                                f'<div class="abs" style="top:{y}px;left:{x + HALF - 24}px">{num}</div>')
                else:
                    rows.append(f'<div class="abs" style="top:{y}px;left:{x}px">{num}</div>'
                                f'<div class="abs" style="top:{y}px;left:{x + 40}px;width:{HALF - 40}px;line-height:24px">{txt}</div>')
            yf = TOP + len(st) * step + 12
            if foot:
                rows.append(f'<div class="abs" style="top:{yf}px;left:{x}px;width:{HALF}px;text-align:{align};'
                            f'border-top:1px solid {s["c"]};padding-top:10px;font-size:13px;color:#EDEDF2">{_esc(foot)}</div>')
            lanes.append("".join(rows))
        legend = (f'<div class="abs" style="bottom:56px;left:{LX}px;right:72px;text-align:center;font-size:11.5px;color:#7A7A84">'
                  'A dashed number is a wait on someone else: a permit, a clearance or a booking.</div>')
        self._frame(self._head(kicker, heading) + "".join(lanes) + legend, section)

    def proscons(self, kicker: str, heading: str, left: dict, right: dict, section: str = "Pros and cons"):
        """Each side's own advantages and disadvantages, side by side, in the same order, the same size.
        `left`/`right` = {pros: [...], cons: [...]}."""
        cols = []
        for x, d, s, align in ((LX, left or {}, self.L, "right"), (RX + 40, right or {}, self.R, "left")):
            w = HALF - 40
            blk = ""
            for title, items, mk, col in (("Advantages", d.get("pros"), "+", s["c"]),
                                          ("Disadvantages", d.get("cons"), "&minus;", "#7A7A84")):
                lis = "".join(f'<div class="li lb" style="text-align:{align};'
                              + ('padding-left:0;padding-right:22px' if align == "right" else "")
                              + f'"><span class="mk" style="color:{col};'
                              + ('left:auto;right:0' if align == "right" else "")
                              + f'">{mk}</span>{_esc(t)}</div>' for t in (items or [])[:5])
                blk += (f'<div style="margin-bottom:30px;text-align:{align}">'
                        + _mono_c(title, col, 9.5, align=align) + f'<div style="height:10px"></div>{lis}</div>')
            cols.append(f'<div class="abs" style="top:{TOP}px;left:{x}px;width:{w}px">{blk}</div>')
        self._frame(self._head(kicker, heading) + "".join(cols), section)

    def films(self, kicker: str, heading: str, intro: str, left: list, right: list, section: str = "Our work"):
        """Our own films, each side's under its label, two to four a side. `left`/`right` are film dicts
        already resolved from the library by id (render() does that); a side with none is left empty."""
        cols = []
        for x, fl, s in ((LX, left, self.L), (RX, right, self.R)):
            fl = list(fl or [])[:4]
            if not fl:
                continue
            n = len(fl)
            w = HALF if n == 1 else 256
            h = int(w * 9 / 16)
            cells = []
            for i, f in enumerate(fl):
                cx = x + (i % 2) * (w + 16) if n > 1 else x
                cy = TOP + 6 + (i // 2) * (h + 74)
                th = f.get("thumb")
                im = (f'<img style="width:{w}px;height:{h}px;display:block;border-radius:6px;object-fit:cover;'
                      f'border-bottom:3px solid {s["c"]}" src="{_b64(th)}">') if th and os.path.isfile(th) else \
                     f'<div style="width:{w}px;height:{h}px;background:#141418;border-radius:6px"></div>'
                url = f'https://www.youtube.com/watch?v={f["youtube_video_id"]}'
                cells.append(f'<a href="{url}"><div class="abs" style="top:{cy}px;left:{cx}px;width:{w}px">{im}'
                             f'<div class="cap"><b>{_esc(f.get("title"))}</b><br>{_esc(f.get("caption"))}. Click to watch.</div>'
                             f'</div></a>')
            cols.append("".join(cells))
        self._frame(self._head(kicker, heading, intro) + "".join(cols), section)

    def scale(self, kicker: str, heading: str, left: list, right: list, note: str = "", section: str = "At scale"):
        """What the Nth film costs on each side. Rows = {k, v, sub}; the figures arrive computed."""
        cols = []
        for x, rows, s, align in ((LX, left, self.L, "right"), (RX + 40, right, self.R, "left")):
            out = []
            for i, r in enumerate((rows or [])[:4]):
                out.append(f'<div style="margin-bottom:24px;text-align:{align}">'
                           + _mono_c(r.get("k"), "#9A9AA0", 9.5, align=align) +
                           f'<div class="big" style="font-size:{40 if i == 0 else 32}px;color:{s["c"]};margin-top:6px">'
                           f'{_esc(r.get("v"))}</div>'
                           f'<div style="font-size:12.5px;color:#B4B4BC;margin-top:5px">{_esc(r.get("sub"))}</div></div>')
            cols.append(f'<div class="abs" style="top:{TOP + 4}px;left:{x}px;width:{HALF - 40}px">{"".join(out)}</div>')
        nt = (f'<div class="abs" style="bottom:52px;left:{LX}px;right:72px;text-align:center;font-size:11px;'
              f'color:#7A7A84">{_esc(note)}</div>') if note else ""
        self._frame(self._head(kicker, heading) + "".join(cols) + nt, section)

    def verdict(self, kicker: str, heading: str, left: list, right: list, band: dict | None = None,
                left_title: str = "", right_title: str = "", section: str = "Verdict"):
        """When to choose each side, then the recommendation across the spine: the one block allowed to
        cross it, because it is the answer that uses both."""
        cols = []
        for x, items, s, t, align in ((LX, left, self.L, left_title, "right"), (RX + 40, right, self.R, right_title, "left")):
            lis = "".join(f'<div class="li lb" style="text-align:{align};'
                          + ('padding-left:0;padding-right:22px' if align == "right" else "")
                          + f'"><span class="mk" style="color:{s["c"]};'
                          + ('left:auto;right:0' if align == "right" else "")
                          + f'">&#10003;</span>{_esc(i)}</div>' for i in (items or [])[:5])
            cols.append(f'<div class="abs" style="top:{TOP}px;left:{x}px;width:{HALF - 40}px;text-align:{align}">'
                        + _mono_c(t or f"Choose {s['label']} when", s["c"], 9.5, align=align)
                        + f'<div style="height:12px"></div>{lis}</div>')
        b = band or {}
        bd = ""
        if b:
            bd = (f'<div class="abs" style="top:468px;left:{LX}px;width:{W - 2 * LX}px;height:150px;border-radius:12px;'
                  f'background:linear-gradient(to right, {self.L["c"]}, {self.R["c"]});padding:2px">'
                  f'<div style="background:#0E0E11;border-radius:10px;height:146px;padding:22px 30px;text-align:center">'
                  + _mono_c(b.get("label") or "Our recommendation", "#9A9AA0", 9.5) +
                  f'<div style="font-family:Poppins,sans-serif;font-size:26px;font-weight:700;color:#F4F4F5;margin-top:8px">'
                  f'{_hl(b.get("title"), self.L["c"])}</div>'
                  f'<p style="margin:8px auto 0;max-width:900px">{_esc(b.get("body"))}</p></div></div>')
        self._frame(self._head(kicker, heading) + "".join(cols) + bd, section, spine=not b)

    def close(self, kicker: str, heading: str, sub: str, contact: dict | None = None, section: str = "Next"):
        c = contact or {}
        body = (_vs_badge(self.L["c"], self.R["c"], top=150) +
                f'<div class="abs" style="top:290px;left:120px;right:120px;text-align:center">'
                + _mono_c(kicker, SITE["muted"]) +
                f'<h1 style="margin-top:12px">{_hl(heading, self.L["c"])}</h1>'
                f'<p style="margin:14px auto 0;max-width:760px;font-size:15px">{_esc(sub)}</p>'
                + (f'<div style="margin-top:30px">' + _mono_c(c.get("label"), "#9A9AA0", 9)
                   + f'<div style="font-family:Poppins,sans-serif;font-size:24px;font-weight:600;color:#F4F4F5;margin-top:6px">'
                   f'{_esc(c.get("line"))}</div><div style="font-size:13px;color:#9A9AA0;margin-top:4px">'
                   f'{_esc(c.get("sub"))}</div></div>' if c else "")
                + '</div>')
        self._frame(body, section, spine=False)

    def html(self) -> str:
        return ('<html><head><meta charset="utf-8"><style>' + _CSS + "</style></head><body>"
                + "".join(self.pages) + "</body></html>")


# ---------------------------------------------------------------------- small parts

def _tag(text: str, colour: str) -> str:
    return (f'<span style="display:inline-block;border:1px solid {colour};border-radius:3px;padding:4px 10px;'
            f'font-family:{MONO};font-size:9.5px;letter-spacing:2px;text-transform:uppercase;color:{colour};'
            f'font-weight:500">{_esc(text)}</span>')


def _mono_c(text, colour: str, size: float = 9.5, align: str = "center") -> str:
    if not text:
        return ""
    return (f'<div style="font-family:{MONO};font-size:{size}px;letter-spacing:2px;text-transform:uppercase;'
            f'color:{colour};font-weight:500;text-align:{align}">{_esc(text)}</div>')


def _dot(colour: str) -> str:
    return (f'<span style="display:inline-block;width:8px;height:8px;border-radius:4px;background:{colour};'
            f'margin:0 9px 1px 9px;vertical-align:middle"></span>')


def _vs_badge(lc: str, rc: str, top: int) -> str:
    return (f'<div class="abs" style="top:{top}px;left:{MID - 46}px;width:92px;height:92px;border-radius:46px;'
            f'background:linear-gradient(to right, {lc} 50%, {rc} 50%);padding:3px">'
            f'<div style="width:86px;height:86px;border-radius:43px;background:#0A0A0A;text-align:center;'
            f'line-height:86px;font-family:Poppins,sans-serif;font-size:26px;font-weight:800;color:#F4F4F5">VS</div></div>')


def _money(v: float, currency: str) -> str:
    return f"{currency} {v:,.0f}"


# ---------------------------------------------------------------------- entry point

PAGE_TYPES = ("cover", "spotlight", "scorecard", "cost", "process", "proscons", "films", "scale", "verdict", "close")


def render(company_slug: str, spec: dict, *, out_dir: str = "/tmp", filename: str = "comparison.pdf") -> dict:
    """Lay out a comparison deck whose content is already written. spec = {sides: {left: {label, colour},
    right: {...}}, label, pages: [{type, ...}]}; page types are PAGE_TYPES. Films are named by youtube id
    per side (`left_ids`, `right_ids`) and resolved from the library BY CODE; an id we do not hold drops."""
    co = store.get_company_by_slug(company_slug)
    if not co:
        raise ValueError(f"unknown company {company_slug}")
    v = _Versus(co, spec.get("sides") or {}, spec.get("label") or "")
    for pg in spec.get("pages") or []:
        t = (pg.get("type") or "").strip().lower()
        if t not in PAGE_TYPES:
            raise ValueError(f"unknown comparison page type '{t}'")
        sec = pg.get("section")
        if t == "cover":
            v.cover(pg.get("title", ""), pg.get("title2", ""), pg.get("standfirst", ""), pg.get("meta") or [],
                    pg.get("left_image"), pg.get("right_image"), sec or "Comparison")
        elif t == "spotlight":
            v.spotlight(pg.get("side", "left"), pg.get("kicker", ""), pg.get("title", ""), pg.get("caption", ""),
                        pg.get("image"), pg.get("href", ""), bool(pg.get("play", True)), sec or "Watch")
        elif t == "scorecard":
            v.scorecard(pg.get("kicker", ""), pg.get("heading", ""), pg.get("rows") or [], pg.get("note", ""),
                        sec or "At a glance")
        elif t == "cost":
            v.cost(pg.get("kicker", ""), pg.get("heading", ""), pg.get("rows") or [], pg.get("totals"),
                   pg.get("note", ""), pg.get("currency") or "AED", sec or "Cost")
        elif t == "process":
            v.process(pg.get("kicker", ""), pg.get("heading", ""), pg.get("left") or [], pg.get("right") or [],
                      pg.get("left_foot", ""), pg.get("right_foot", ""), sec or "Process")
        elif t == "proscons":
            v.proscons(pg.get("kicker", ""), pg.get("heading", ""), pg.get("left") or {}, pg.get("right") or {},
                       sec or "Pros and cons")
        elif t == "films":
            def _side(ids):
                return [{**f, "thumb": thumbnail(f["youtube_video_id"], out_dir), "caption": film_caption(f)}
                        for f in films_by_ids(co["id"], ids or [])]
            v.films(pg.get("kicker", ""), pg.get("heading", ""), pg.get("intro", ""),
                    _side(pg.get("left_ids")), _side(pg.get("right_ids")), sec or "Our work")
        elif t == "scale":
            v.scale(pg.get("kicker", ""), pg.get("heading", ""), pg.get("left") or [], pg.get("right") or [],
                    pg.get("note", ""), sec or "At scale")
        elif t == "verdict":
            v.verdict(pg.get("kicker", ""), pg.get("heading", ""), pg.get("left") or [], pg.get("right") or [],
                      pg.get("band"), pg.get("left_title", ""), pg.get("right_title", ""), sec or "Verdict")
        elif t == "close":
            v.close(pg.get("kicker", ""), pg.get("heading", ""), pg.get("sub", ""), pg.get("contact"), sec or "Next")
    res = {"path": to_pdf(v.html(), os.path.join(out_dir, filename)), "pages": len(v.pages)}
    try:
        import pypdf
        real = len(pypdf.PdfReader(res["path"]).pages)
        res["rendered"] = real
        if real != res["pages"]:
            res["overflow"] = real - res["pages"]
            print(f"[versus] {res['pages']} pages laid out, {real} rendered - a page has overflowed", flush=True)
    except Exception:  # noqa: BLE001
        pass
    return res
