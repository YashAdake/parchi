import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from parchi import billing, matcher, reader, whatsapp
from parchi.app import create_app
from parchi.store import Store, parse_pricelist_csv


@pytest.fixture
def store():
    return Store(":memory:")


def item(raw, name, qty=None, unit=None, size=None):
    return {"raw": raw, "item": name, "qty": qty, "unit": unit, "size": size}


def match(store, it, learned=None):
    return matcher.match_line(it, store.skus(), learned or {})


# --- matcher -------------------------------------------------------------

def test_alias_match_and_kg(store):
    ln = match(store, item("2 kilo atta", "wheat atta", 2, "kg"))
    assert (ln["status"], ln["sku"], ln["qty"]) == ("ok", "atta-loose", "2")


def test_grams_convert_to_kg(store):
    ln = match(store, item("500 gm poha", "poha", 500, "g"))
    assert (ln["sku"], ln["qty"], ln["status"]) == ("poha", "0.5", "ok")


def test_dozen_eggs(store):
    assert match(store, item("1 dozen ande", "eggs", 1, "dozen"))["qty"] == "12"


def test_size_word_picks_pack(store):
    ln = match(store, item("1 amul butter chhota", "amul butter", 1, None, "small"))
    assert (ln["sku"], ln["status"]) == ("butter-amul-100g", "ok")


def test_volume_picks_pack(store):
    ln = match(store, item("एक लिटर दूध", "milk", 1, "l"))
    assert (ln["sku"], ln["qty"], ln["status"]) == ("milk-amul-1l", "1", "ok")


def test_two_litres_is_two_one_litre_packs(store):
    ln = match(store, item("2 litre doodh", "milk", 2, "l"))
    assert (ln["sku"], ln["qty"]) == ("milk-amul-1l", "2")


def test_several_sizes_needs_check(store):
    ln = match(store, item("ek maggi", "maggi", 1, "pack"))
    assert ln["status"] == "check" and {o["sku"] for o in ln["options"]} >= {"maggi-70g", "maggi-280g"}


def test_unknown_item(store):
    ln = match(store, item("1 kg kaju katli", "kaju katli sweet", 1, "kg"))
    assert ln["status"] == "unknown" and ln["sku"] is None


def test_out_of_stock(store):
    assert match(store, item("paneer", "paneer"))["status"] == "out"


def test_missing_qty_is_flagged(store):
    ln = match(store, item("dahi", "dahi"))
    assert ln["status"] == "check" and "assumed 1" in ln["note"]


def test_learned_alias_wins(store):
    it = item("2 packet biskut", "biskut", 2, "pack")
    learned = {matcher.phrase_key(it): "parle-g-250g"}
    ln = match(store, it, learned)
    assert (ln["sku"], ln["status"], ln["qty"]) == ("parle-g-250g", "ok", "2")


# --- billing -------------------------------------------------------------

def test_bill_uses_price_list_not_request(store):
    bill = billing.build_bill([{"sku": "eggs", "qty": "6", "price": "1"}], store.sku_map())
    assert bill["rows"][0]["total"] == "42.00" and bill["total"] == "42"


def test_bill_rounds_to_rupee_and_skips_out_of_stock(store):
    bill = billing.build_bill([{"sku": "sugar", "qty": "0.25"}, {"sku": "paneer-200g", "qty": "1"}],
                              store.sku_map(), delivery="10")
    assert bill["subtotal"] == "11.25" and bill["total"] == "21" and bill["round_off"] == "-0.25"
    assert bill["skipped"] == ["Paneer 200 g"]


@pytest.mark.parametrize("qty", ["0", "-1", "abc", "5000"])
def test_bad_quantity_rejected(store, qty):
    with pytest.raises(billing.BillError):
        billing.build_bill([{"sku": "eggs", "qty": qty}], store.sku_map())


def test_upi_uri_is_encoded():
    uri = billing.upi_uri("shop@okaxis", "Joshi Kirana & Sons", "224", "Parchi order 7")
    assert uri.startswith("upi://pay?pa=shop%40okaxis&pn=Joshi%20Kirana%20%26%20Sons&am=224.00&cu=INR")
    assert billing.qr_svg(uri).lstrip().startswith("<?xml") or "<svg" in billing.qr_svg(uri)


def test_wa_link_adds_india_code():
    assert billing.wa_link("98765 43210", "hi").startswith("https://wa.me/919876543210?text=hi")


# --- reader --------------------------------------------------------------

def test_reader_validates_and_clamps():
    out = json.dumps({"is_order": True, "items": [
        {"raw": "2 kilo atta", "item": "Wheat Atta", "qty": 2, "unit": "kg", "size": None},
        {"raw": "x", "item": "rice", "qty": -3, "unit": "tonne", "size": "huge"}]})
    res = reader.read_order("2 kilo atta", chat=lambda t: out)
    assert res["items"][0]["item"] == "wheat atta"
    assert res["items"][1] == {"raw": "x", "item": "rice", "qty": None, "unit": None, "size": None}


def test_reader_retries_once_then_fails():
    calls = []
    def bad(t):
        calls.append(t)
        return "not json"
    with pytest.raises(reader.ReaderError):
        reader.read_order("2 kilo atta", chat=bad)
    assert len(calls) == 2


def test_reader_not_an_order():
    res = reader.read_order("thank you", chat=lambda t: '{"is_order": false, "items": []}')
    assert res["is_order"] is False and res["items"] == []


# --- whatsapp export -------------------------------------------------------

def test_parse_android_and_iphone_exports():
    text = ("15/10/26, 9:41 am - Mrs Joshi: 2 kilo atta\naur 6 ande\n"
            "15/10/26, 9:42 am - Mrs Joshi: <Media omitted>\n"
            "[15/10/26, 10:02:11 AM] Rahul: 1 litre doodh")
    msgs = whatsapp.parse_chat(text)
    assert [(m["sender"], m["text"]) for m in msgs] == [("Mrs Joshi", "2 kilo atta\naur 6 ande"), ("Rahul", "1 litre doodh")]


# --- price list ------------------------------------------------------------

def test_pricelist_rejects_bad_unit():
    with pytest.raises(ValueError, match="sold by"):
        parse_pricelist_csv("sku,name,unit,price\na,Atta,bag,40\n")


# --- API end to end (fake model) -------------------------------------------

def test_api_read_and_bill(store):
    fake = json.dumps({"is_order": True, "items": [
        {"raw": "6 ande", "item": "eggs", "qty": 6, "unit": "pc", "size": None},
        {"raw": "<script>alert(1)</script> 1 biskut", "item": "biskut", "qty": 1, "unit": "pack", "size": None}]})
    app = create_app(store=store, read=lambda t: reader.read_order(t, chat=lambda _: fake))
    c = TestClient(app)
    c.post("/api/shop", json={"name": "Joshi Kirana", "upi_id": "joshi@okaxis"})
    lines = c.post("/api/read", json={"text": "6 ande, 1 biskut"}).json()["lines"]
    assert lines[0]["status"] == "ok"
    bill = c.post("/api/bill", json={"customer": "Rahul", "lines": [
        {"sku": "eggs", "qty": "6", "raw": "6 ande"},
        {"sku": "parle-g-250g", "qty": "1", "raw": lines[1]["raw"], "phrase": lines[1]["phrase"], "learn": True}]}).json()
    assert bill["total"] == "67" and "Total: ₹67" in bill["reply"] and "am=67.00" in bill["upi_uri"]
    assert store.learned()[lines[1]["phrase"]] == "parle-g-250g"
    assert c.get("/api/orders").json()[0]["total"] == "67"
    assert c.post(f"/api/orders/{bill['order_id']}/status", json={"status": "paid"}).json() == {"ok": True}
    assert c.post("/api/bill", json={"lines": [{"sku": "nope", "qty": "1"}]}).status_code == 400


def test_partial_pack_rounds_up_and_asks(store):
    ln = match(store, item("paav kilo jeera", "jeera", 0.25, "kg"))
    assert (ln["sku"], ln["qty"], ln["status"]) == ("jeera-100g", "3", "check")
    assert [c["qty"] for c in ln["choices"]] == ["2", "3"]
    assert ln["choices"][0]["label"] == "2 × 100 g = 200 g"


# --- regressions found in review -------------------------------------------

@pytest.mark.parametrize("price", ["nan", "inf", "-5", "abc", ""])
def test_pricelist_rejects_bad_prices(price):
    with pytest.raises(ValueError, match="price"):
        parse_pricelist_csv(f"sku,name,unit,price\na,Atta,kg,{price}\n")


def test_pricelist_error_names_the_product():
    with pytest.raises(ValueError, match="Toor Dal"):
        parse_pricelist_csv("sku,name,unit,price\ndal,Toor Dal,kg,lots\n")


def test_money_uses_indian_grouping():
    assert billing.money(Decimal("123456")) == "₹1,23,456"
    assert billing.money(Decimal("1234567.5")) == "₹12,34,567.50"
    assert billing.money(Decimal("999")) == "₹999"
    assert billing.money(Decimal("-0.25")) == "−₹0.25"


def test_wa_link_handles_leading_zero():
    assert billing.wa_link("09876543210", "x").startswith("https://wa.me/919876543210?")


def test_chat_export_skips_system_and_media_lines():
    text = ("15/10/26, 9:40 am - Messages and calls are end-to-end encrypted. No one outside of this chat can read them.\n"
            "15/10/26, 9:41 am - Mrs Joshi: 2 kilo atta\n"
            "15/10/26, 9:42 am - Mrs Joshi changed their phone number\n"
            "15/10/26, 9:43 am - Mrs Joshi: image omitted\n"
            "15/10/26, 9:44 am - Rahul: 1 litre doodh")
    msgs = whatsapp.parse_chat(text)
    assert [(m["sender"], m["text"]) for m in msgs] == [("Mrs Joshi", "2 kilo atta"), ("Rahul", "1 litre doodh")]


def test_learning_shows_up_as_customer_word_and_can_be_removed(store):
    store.learn("biskut", "parle-g-250g")
    parle = store.sku_map()["parle-g-250g"]
    assert "biskut" in parle.aliases and store.learned()["biskut"] == "parle-g-250g"
    parle.aliases.remove("biskut")
    store.replace_skus([parle if s.sku == parle.sku else s for s in store.skus()])
    store.forget_missing_aliases()
    assert "biskut" not in store.learned()


def test_api_rejects_bad_upi_and_does_not_learn_from_failed_bill(store):
    c = TestClient(create_app(store=store, read=lambda t: {"is_order": False, "items": []}))
    assert c.post("/api/shop", json={"upi_id": "not a upi"}).status_code == 400
    assert c.post("/api/shop", json={"upi_id": "joshi.kirana@okaxis"}).status_code == 200
    bad = c.post("/api/bill", json={"lines": [{"sku": "eggs", "qty": "abc", "phrase": "andaa", "learn": True}]})
    assert bad.status_code == 400 and "andaa" not in store.learned()


def test_quote_mode_saves_nothing(store):
    c = TestClient(create_app(store=store, read=lambda t: {"is_order": False, "items": []}))
    q = c.post("/api/bill", json={"save": False, "lines": [{"ref": "l1", "sku": "eggs", "qty": "6", "phrase": "andaa", "learn": True}]}).json()
    assert q["total"] == "42" and q["rows"][0]["ref"] == "l1"
    assert store.orders() == [] and "andaa" not in store.learned()


def test_index_is_not_cached(store):
    c = TestClient(create_app(store=store))
    assert c.get("/").headers["cache-control"] == "no-store"


def test_learning_keeps_customer_words_too(store):
    c = TestClient(create_app(store=store, read=lambda t: {"is_order": False, "items": []}))
    r = c.post("/api/bill", json={"lines": [{"sku": "coriander", "qty": "2", "raw": "2 nimbu", "phrase": "lemon", "learn": True}]})
    assert r.status_code == 200
    assert {"lemon", "nimbu"} <= set(store.sku_map()["coriander"].aliases)
    ln = matcher.match_line(item("4 nimbu", "lime", 4, None), store.skus(), store.learned())
    assert ln["sku"] == "coriander"


def test_reply_shows_round_off(store):
    bill = billing.build_bill([{"sku": "sugar", "qty": "0.5"}], store.sku_map())
    text = billing.reply_text({"name": "Shop"}, "", bill, 1, [])
    assert "Round off – ₹0.50" in text and "*Total: ₹23*" in text
