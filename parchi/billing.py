"""Bill maths, the WhatsApp reply and the UPI payment QR. All plain code, no AI."""

import io
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import quote, urlencode

import qrcode
import qrcode.image.svg

PAISE = Decimal("0.01")
RUPEE = Decimal("1")
UNIT_LABEL = {"kg": "kg", "l": "L", "pc": "pc", "pack": "pkt"}


class BillError(ValueError):
    pass


def money(value):
    """Indian-style rupees: ₹1,23,456 or ₹45.50."""
    value = Decimal(value)
    sign = "−" if value < 0 else ""
    value = abs(value).quantize(PAISE, ROUND_HALF_UP)
    whole, frac = f"{value:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}" + ("" if frac == "00" else f".{frac}")


def qty_text(qty, unit):
    return f"{format(qty.normalize(), 'f')} {UNIT_LABEL.get(unit, unit)}"


def build_bill(lines, sku_map, delivery="0", discount="0"):
    """lines: [{sku, qty, raw?}]. Prices always come from the price list, never from the request."""
    rows, skipped = [], []
    for ln in lines:
        sku = sku_map.get(ln.get("sku") or "")
        if sku is None:
            skipped.append(ln.get("raw") or "item")
            continue
        try:
            qty = Decimal(str(ln.get("qty")))
        except (InvalidOperation, TypeError):
            raise BillError(f"Quantity for {sku.name} is not a number") from None
        if not qty.is_finite():
            raise BillError(f"Quantity for {sku.name} is not a number")
        if qty <= 0 or qty > 1000:
            raise BillError(f"Quantity for {sku.name} must be between 0 and 1000")
        if not sku.in_stock:
            skipped.append(sku.name)
            continue
        ref = ln.get("ref")
        price = Decimal(sku.price)
        rows.append({"sku": sku.sku, "name": sku.name, "qty": format(qty.normalize(), "f"),
                     "qty_text": qty_text(qty, sku.unit), "unit": sku.unit, "price": str(price),
                     "total": str((qty * price).quantize(PAISE, ROUND_HALF_UP)), "raw": ln.get("raw", ""),
                     "ref": ref})
    try:
        delivery_d = Decimal(str(delivery or "0"))
        discount_d = Decimal(str(discount or "0"))
    except InvalidOperation:
        raise BillError("Delivery and discount must be numbers") from None
    if not (delivery_d.is_finite() and discount_d.is_finite()):
        raise BillError("Delivery and discount must be numbers")
    if delivery_d < 0 or discount_d < 0:
        raise BillError("Delivery and discount cannot be negative")
    subtotal = sum((Decimal(r["total"]) for r in rows), Decimal("0"))
    before_round = subtotal + delivery_d - discount_d
    if before_round < 0:
        raise BillError("Discount is bigger than the bill")
    total = before_round.quantize(RUPEE, ROUND_HALF_UP)
    return {"rows": rows, "skipped": skipped, "subtotal": str(subtotal), "delivery": str(delivery_d),
            "discount": str(discount_d), "round_off": str(total - before_round), "total": str(total)}


def upi_uri(upi_id, payee, amount, note):
    params = {"pa": upi_id.strip(), "pn": payee.strip() or "Shop", "am": f"{Decimal(amount):.2f}",
              "cu": "INR", "tn": note}
    return "upi://pay?" + urlencode(params, quote_via=quote)


def qr_svg(data):
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


def reply_text(shop, customer, bill, order_no, unavailable):
    greet = f"Namaste {customer.strip()} ji!" if customer.strip() else "Namaste!"
    lines = [greet, f"Your order #{order_no} from {shop.get('name') or 'our shop'}:"]
    for r in bill["rows"]:
        lines.append(f"• {r['name']} – {r['qty_text']} – {money(Decimal(r['total']))}")
    if Decimal(bill["delivery"]):
        lines.append(f"• Delivery – {money(Decimal(bill['delivery']))}")
    if Decimal(bill["discount"]):
        lines.append(f"• Discount – {money(-Decimal(bill['discount']))}")
    if Decimal(bill["round_off"]):
        lines.append(f"• Round off – {money(Decimal(bill['round_off']))}")
    lines.append(f"*Total: {money(Decimal(bill['total']))}*")
    if unavailable:
        lines.append("Sorry, not available right now: " + ", ".join(unavailable) + ".")
    if shop.get("upi_id"):
        lines.append(f"Pay by UPI: {shop['upi_id']}")
    if shop.get("delivery_note"):
        lines.append(shop["delivery_note"])
    lines.append("Dhanyavaad 🙏")
    return "\n".join(lines)


def wa_link(phone, text):
    digits = "".join(ch for ch in phone or "" if ch.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    base = f"https://wa.me/{digits}" if len(digits) >= 11 else "https://wa.me/"
    return base + "?text=" + quote(text)
