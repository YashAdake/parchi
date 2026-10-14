"""Parchi web app: a local page for the shop counter. Run with `parchi` or `python -m parchi`."""

import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import billing, matcher, reader, whatsapp
from .store import Store, parse_pricelist_csv, pricelist_to_csv, Sku

STATIC = Path(__file__).resolve().parent / "static"
UPI_RE = re.compile(r"^[\w.\-]{2,256}@[A-Za-z][A-Za-z0-9]{1,63}$")
NO_CACHE = {"Cache-Control": "no-store"}


class ReadIn(BaseModel):
    text: str = Field(max_length=4000)


class LineIn(BaseModel):
    ref: str | None = Field(None, max_length=40)
    sku: str | None = None
    qty: str | float | None = None
    raw: str = ""
    phrase: str = ""
    learn: bool = False


class BillIn(BaseModel):
    customer: str = Field("", max_length=80)
    phone: str = Field("", max_length=20)
    raw_text: str = Field("", max_length=4000)
    lines: list[LineIn] = Field(max_length=60)
    delivery: str = "0"
    discount: str = "0"
    save: bool = True


class TextIn(BaseModel):
    text: str = Field(max_length=2_000_000)


class StatusIn(BaseModel):
    status: str


def create_app(store=None, read=None):
    app = FastAPI(title="Parchi", docs_url=None, redoc_url=None)
    app.state.store = store or Store()
    app.state.read = read or reader.read_order

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers=NO_CACHE)

    @app.get("/api/health")
    def health():
        return reader.health()

    @app.post("/api/warmup")
    def warm():
        return {"ready": reader.warmup()}

    @app.get("/api/customers")
    def customers():
        seen = {}
        for o in app.state.store.orders(500):
            name = (o.get("customer") or "").strip()
            if name and name.lower() not in seen:
                seen[name.lower()] = {"name": name, "phone": o.get("phone") or ""}
        return list(seen.values())[:100]

    @app.get("/api/shop")
    def get_shop():
        return app.state.store.shop()

    @app.post("/api/shop")
    def save_shop(values: dict):
        clean = {k: v.strip()[:200] for k, v in values.items() if isinstance(v, str)}
        if clean.get("upi_id") and not UPI_RE.match(clean["upi_id"]):
            raise HTTPException(400, "That UPI ID doesn't look right. It should look like name@bank")
        return app.state.store.save_shop(clean)

    @app.get("/api/skus")
    def get_skus():
        return [s.to_dict() for s in app.state.store.skus()]

    @app.post("/api/skus/import")
    def import_skus(body: TextIn):
        try:
            skus = parse_pricelist_csv(body.text)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        app.state.store.replace_skus(skus)
        return {"count": len(skus)}

    @app.post("/api/skus")
    def save_skus(rows: list[dict]):
        try:
            skus = parse_pricelist_csv(pricelist_to_csv([Sku(
                sku=str(r.get("sku", "")).strip(), name=str(r.get("name", "")).strip(),
                unit=str(r.get("unit", "")).strip().lower(), price=str(r.get("price", "")).strip(),
                group=str(r.get("group", "")), size=str(r.get("size", "")),
                in_stock=bool(r.get("in_stock", True)),
                aliases=[a for a in r.get("aliases", []) if isinstance(a, str)]) for r in rows]))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        app.state.store.replace_skus(skus)
        app.state.store.forget_missing_aliases()
        return {"count": len(skus)}

    @app.get("/api/skus/export", response_class=PlainTextResponse)
    def export_skus():
        return pricelist_to_csv(app.state.store.skus())

    @app.post("/api/read")
    def read_message(body: ReadIn):
        try:
            result = app.state.read(body.text)
        except reader.ReaderError as e:
            raise HTTPException(502, str(e)) from None
        store = app.state.store
        lines = matcher.match_order(result["items"], store.skus(), store.learned())
        return {"is_order": result["is_order"], "ms": result.get("ms"), "lines": lines}

    @app.post("/api/bill")
    def make_bill(body: BillIn):
        store = app.state.store
        sku_map = store.sku_map()
        try:
            bill = billing.build_bill([ln.model_dump() for ln in body.lines], sku_map,
                                      body.delivery, body.discount)
        except billing.BillError as e:
            raise HTTPException(400, str(e)) from None
        if not body.save:
            return {**bill, "order_id": 0}
        if not bill["rows"]:
            raise HTTPException(400, "Nothing to bill: choose a product for at least one line")
        for ln in body.lines:  # remember corrections only once the bill is valid
            if ln.learn and ln.sku in sku_map:
                # Remember the customer's own words ("nimbu") as well as the model's English ("lemon").
                for phrase in {ln.phrase, matcher.phrase_key({"raw": ln.raw})}:
                    if phrase and len(phrase) <= 40 and len(phrase.split()) <= 4:
                        store.learn(phrase, ln.sku)
        shop = store.shop()
        order_id = store.add_order(body.customer, body.phone, body.raw_text, bill["rows"], bill["total"])
        unavailable = bill["skipped"]
        text = billing.reply_text(shop, body.customer, bill, order_id or "-", unavailable)
        out = {**bill, "order_id": order_id, "shop": shop, "reply": text,
               "wa_link": billing.wa_link(body.phone, text), "upi_uri": None, "qr_svg": None}
        if shop.get("upi_id"):
            out["upi_uri"] = billing.upi_uri(shop["upi_id"], shop.get("name", ""), bill["total"],
                                             f"Parchi order {order_id or ''}".strip())
            out["qr_svg"] = billing.qr_svg(out["upi_uri"])
        return out

    @app.post("/api/whatsapp/parse")
    def parse_whatsapp(body: TextIn):
        return whatsapp.parse_chat(body.text)[-300:]

    @app.get("/api/orders")
    def list_orders():
        return app.state.store.orders()

    @app.post("/api/orders/{order_id}/status")
    def order_status(order_id: int, body: StatusIn):
        try:
            if not app.state.store.set_status(order_id, body.status):
                raise HTTPException(404, "No such order")
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True}

    return app


def main():
    import uvicorn

    host = os.environ.get("PARCHI_HOST", "127.0.0.1")
    port = int(os.environ.get("PARCHI_PORT", "8765"))
    print(f"Parchi is running: open http://{host}:{port} in your browser (Ctrl+C to stop)")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
