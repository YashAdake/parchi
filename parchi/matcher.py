"""Match extracted order items to the shop's price list. No AI here: plain rules.

Each line ends up with a status:
  ok      - one clear product and quantity
  check   - a likely product, but the shopkeeper should confirm (several sizes, unit guessed, ...)
  unknown - nothing in the price list looks like it
  out     - matched, but marked out of stock
"""

import re
import unicodedata
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from rapidfuzz import fuzz

STRONG = 90
WEAK = 70

_NOISE = re.compile(r"[^\w\sऀ-ॿ]", re.UNICODE)
_SPACE = re.compile(r"\s+")
_QTY_WORDS = re.compile(
    r"\b(\d+(\.\d+)?|kilo|kg|kgs|gram|grams|gm|gms|g|litre|liter|ltr|l|ml|packet|packets|pkt|pack|"
    r"piece|pieces|pc|pcs|nag|dozen|darjan|ek|do|teen|char|paanch|aadha|adha|ardha|dedh|dhai|paav|"
    r"chhota|chota|bada|bara|mota|motha|small|big|large|aur|and|ani|bhi|bhejna|bhejo|chahiye|pathva)\b")


def norm(text):
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = _NOISE.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def phrase_key(item):
    """Key used to remember a shopkeeper's correction: the item words without quantities."""
    base = norm(item.get("item") or item.get("raw"))
    stripped = _SPACE.sub(" ", _QTY_WORDS.sub(" ", base)).strip()
    return stripped or base


def _strip_qty(text):
    return _SPACE.sub(" ", _QTY_WORDS.sub(" ", norm(text))).strip()


def _names(sku):
    """Names to compare against, without pack sizes ("Amul Butter 100 g" -> "amul butter")."""
    return [n for n in {_strip_qty(x) for x in [sku.name, sku.group, *sku.aliases] if x} if n]


def _score(queries, sku):
    best = 0
    for q in queries:
        for n in _names(sku):
            if q == n:
                return 100
            best = max(best, fuzz.WRatio(q, n))
    return best


_PACK = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|gm|l|ml|ltr)\b", re.IGNORECASE)
_TO_BASE = {"kg": ("g", 1000), "g": ("g", 1), "gm": ("g", 1), "l": ("ml", 1000), "ltr": ("ml", 1000),
            "ml": ("ml", 1)}


def pack_size(sku):
    """Weight/volume of one pack read from its name, e.g. 'Amul Taaza Milk 1 L' -> (1000, 'ml')."""
    m = _PACK.search(sku.name)
    if sku.unit not in ("pc", "pack") or not m:
        return None
    base, factor = _TO_BASE[m.group(2).lower()]
    return Decimal(m.group(1)) * factor, base


def amount_said(qty, unit):
    if qty is None or unit not in ("kg", "g", "l", "ml"):
        return None
    base, factor = _TO_BASE[unit]
    return Decimal(str(qty)) * factor, base


def convert_qty(qty, unit, sku):
    """Return (quantity in the sku's own unit, note or None, needs_check)."""
    said, pack = amount_said(qty, unit), pack_size(sku)
    if said and pack and said[1] == pack[1]:
        count = said[0] / pack[0]
        if count == count.to_integral_value() and count > 0:
            return count, None, False
        return max(Decimal(1), count.to_integral_value(ROUND_CEILING)), (
            f"Customer asked for {qty:g} {unit}; packs are {format(pack[0].normalize(), 'f')} {pack[1]}"), True
    if qty is None:
        return Decimal(1), "Quantity not given, assumed 1", True
    q = Decimal(str(qty))
    su = sku.unit
    if unit is None:
        if su in ("kg", "l"):
            return q, f"Unit not given, assumed {su}", True
        return q, None, q != q.to_integral_value()
    if unit == su or (unit in ("pc", "pack") and su in ("pc", "pack")):
        return q, None, su in ("pc", "pack") and q != q.to_integral_value()
    if unit == "g" and su == "kg":
        return q / 1000, None, False
    if unit == "ml" and su == "l":
        return q / 1000, None, False
    if unit == "kg" and su == "l" or unit == "l" and su == "kg":
        return q, f"Customer said {unit}, sold by {su}", True
    if unit == "dozen" and su == "pc":
        return q * 12, None, False
    if su in ("pc", "pack"):
        return q, f"Customer asked for {qty:g} {unit}; this is sold per {su}", True
    return q, f"Customer said {unit}, sold by {su}", True


def match_line(item, skus, learned):
    raw_words = phrase_key({"raw": item.get("raw")})  # the customer's own words, minus quantities
    queries = [q for q in {_strip_qty(item.get("item")), phrase_key(item), raw_words} if q]
    by_id = {s.sku: s for s in skus}
    chosen, status, note, options = None, None, None, []

    learned_sku = learned.get(phrase_key(item)) or learned.get(raw_words) or learned.get(norm(item.get("raw")))
    if learned_sku in by_id:
        chosen, status = by_id[learned_sku], "ok"
    else:
        scored = sorted(((_score(queries, s), s) for s in skus), key=lambda x: -x[0])
        scored = [(sc, s) for sc, s in scored if sc >= WEAK]
        options = [{"sku": s.sku, "name": s.name, "score": round(sc)} for sc, s in scored[:4]]
        if not scored:
            status = "unknown"
            note = "Not in the price list"
        else:
            top_score, top = scored[0]
            if top_score == 100:
                # An exact name/alias hit beats fuzzy look-alikes ("atta" vs "aashirvaad atta").
                close = [s for sc, s in scored if sc == 100]
            else:
                close = [s for sc, s in scored if sc >= top_score - 6]
            group = top.group
            if group and all(s.group == group for s in close) and len(close) > 1:
                sized = [s for s in close if item.get("size") and s.size == item["size"]]
                said = amount_said(item.get("qty"), item.get("unit"))
                if not sized and said:
                    # "1 litre doodh" -> the 1 L pack; "2 litre" -> 1 L packs before 500 ml ones.
                    fits = [s for s in close if (p := pack_size(s)) and p[1] == said[1]
                            and (said[0] / p[0]) == (said[0] / p[0]).to_integral_value()]
                    fits.sort(key=lambda s: -pack_size(s)[0])
                    sized = fits[:1]
                if len(sized) == 1:
                    chosen, status = sized[0], ("ok" if top_score >= STRONG else "check")
                else:
                    chosen, status, note = top, "check", "Several sizes, pick one"
            elif len(close) > 1:
                chosen, status, note = top, "check", "Several products match, pick one"
            else:
                chosen = top
                status = "ok" if top_score >= STRONG else "check"
                if status == "check":
                    note = "Best guess, please confirm"

    line = {"raw": item.get("raw", ""), "item": item.get("item", ""), "qty_said": item.get("qty"),
            "unit_said": item.get("unit"), "size_said": item.get("size"), "phrase": phrase_key(item),
            "status": status, "note": note, "options": options, "sku": None, "qty": None, "choices": []}
    if chosen is None:
        return line
    qty, qty_note, qty_check = convert_qty(item.get("qty"), item.get("unit"), chosen)
    line.update({"sku": chosen.sku, "qty": format(qty.normalize(), "f")})
    if qty_check and line["status"] == "ok":
        line["status"] = "check"
    line["note"] = "; ".join(n for n in (line["note"], qty_note) if n) or None
    line["choices"] = pack_choices(item.get("qty"), item.get("unit"), chosen)
    if not chosen.in_stock:
        line["status"], line["note"] = "out", "Out of stock"
    return line


def pack_choices(qty, unit, sku):
    """When the customer asks for an amount that isn't a whole number of packs (250 g of jeera
    sold in 100 g packs), offer the two nearest whole-pack options for one-tap picking."""
    said, pack = amount_said(qty, unit), pack_size(sku)
    if not (said and pack and said[1] == pack[1]):
        return []
    count = said[0] / pack[0]
    if count == count.to_integral_value():
        return []
    unit_txt = "g" if pack[1] == "g" else "ml"
    out = []
    for n in sorted({max(Decimal(1), count.to_integral_value(ROUND_FLOOR)), count.to_integral_value(ROUND_CEILING)}):
        total = format((n * pack[0]).normalize(), "f")
        out.append({"qty": format(n.normalize(), "f"),
                    "label": f"{format(n.normalize(), 'f')} × {format(pack[0].normalize(), 'f')} {unit_txt} = {total} {unit_txt}"})
    return out


def match_order(items, skus, learned):
    return [match_line(it, skus, learned) for it in items]
