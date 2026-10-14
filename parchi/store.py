"""SQLite storage: shop settings, price list, learned aliases and orders."""

import csv
import io
import json
import os
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PRICELIST = ROOT / "data" / "sample_pricelist.csv"
UNITS = {"kg", "l", "pc", "pack"}
CSV_FIELDS = ["sku", "name", "group", "size", "unit", "price", "in_stock", "aliases"]


@dataclass
class Sku:
    sku: str
    name: str
    unit: str
    price: str
    group: str = ""
    size: str = ""
    in_stock: bool = True
    aliases: list = field(default_factory=list)

    def to_dict(self):
        return {
            "sku": self.sku, "name": self.name, "group": self.group, "size": self.size,
            "unit": self.unit, "price": self.price, "in_stock": self.in_stock, "aliases": self.aliases,
        }


def _yes(value):
    return str(value).strip().lower() not in {"no", "n", "false", "0", "out"}


def parse_pricelist_csv(text):
    """Parse a price list CSV. Raises ValueError with a readable message on bad rows."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = {"sku", "name", "unit", "price"} - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"CSV is missing columns: {', '.join(sorted(missing))}")
    skus, seen = [], set()
    for n, row in enumerate(reader, start=2):
        sku = (row.get("sku") or "").strip()
        unit = (row.get("unit") or "").strip().lower()
        price = (row.get("price") or "").strip()
        name = (row.get("name") or "").strip()
        label = f"“{name}”" if name else f"row {n}"
        if not sku:
            continue
        if not name:
            raise ValueError(f"Row {n}: the product needs a name")
        if sku in seen:
            raise ValueError(f"{label}: two products have the same id '{sku}'")
        if unit not in UNITS:
            raise ValueError(f"{label}: 'sold by' must be kg, l, pc or pack, not '{unit}'")
        try:
            value = Decimal(price)
        except InvalidOperation:
            value = None
        if value is None or not value.is_finite() or value < 0 or value > 1_000_000:
            raise ValueError(f"{label}: price must be a number like 45 or 12.50, not '{price}'")
        seen.add(sku)
        aliases = [a.strip() for a in (row.get("aliases") or "").split(";") if a.strip()]
        skus.append(Sku(
            sku=sku, name=name, unit=unit, price=format(value.normalize(), "f"),
            group=(row.get("group") or "").strip().lower(), size=(row.get("size") or "").strip().lower(),
            in_stock=_yes(row.get("in_stock", "yes")), aliases=aliases,
        ))
    if not skus:
        raise ValueError("The price list has no items")
    return skus


def pricelist_to_csv(skus):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for s in skus:
        writer.writerow({
            "sku": s.sku, "name": s.name, "group": s.group, "size": s.size, "unit": s.unit,
            "price": s.price, "in_stock": "yes" if s.in_stock else "no", "aliases": ";".join(s.aliases),
        })
    return out.getvalue()


class Store:
    def __init__(self, path=None):
        self.path = str(path or os.environ.get("PARCHI_DB", ROOT / "data" / "parchi.db"))
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS shop (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS sku (sku TEXT PRIMARY KEY, data TEXT NOT NULL, pos INTEGER);
            CREATE TABLE IF NOT EXISTS learned_alias (phrase TEXT PRIMARY KEY, sku TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, customer TEXT, phone TEXT,
                raw TEXT, lines TEXT, total TEXT, status TEXT DEFAULT 'new');
        """)
        if not self.skus():
            self.replace_skus(parse_pricelist_csv(SAMPLE_PRICELIST.read_text(encoding="utf-8")))

    # shop
    def shop(self):
        data = {"name": "", "address": "", "phone": "", "upi_id": "", "delivery_note": ""}
        data.update({r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM shop")})
        return data

    def save_shop(self, values):
        allowed = {"name", "address", "phone", "upi_id", "delivery_note"}
        with self.db:
            for k, v in values.items():
                if k in allowed:
                    self.db.execute("INSERT OR REPLACE INTO shop VALUES (?, ?)", (k, str(v).strip()))
        return self.shop()

    # price list
    def skus(self):
        rows = self.db.execute("SELECT data FROM sku ORDER BY pos").fetchall()
        return [Sku(**json.loads(r["data"])) for r in rows]

    def sku_map(self):
        return {s.sku: s for s in self.skus()}

    def replace_skus(self, skus):
        with self.db:
            self.db.execute("DELETE FROM sku")
            for pos, s in enumerate(skus):
                self.db.execute("INSERT INTO sku VALUES (?, ?, ?)", (s.sku, json.dumps(s.to_dict()), pos))
            valid = {s.sku for s in skus}
            for row in self.db.execute("SELECT phrase, sku FROM learned_alias").fetchall():
                if row["sku"] not in valid:
                    self.db.execute("DELETE FROM learned_alias WHERE phrase = ?", (row["phrase"],))

    # learned aliases
    def learned(self):
        return {r["phrase"]: r["sku"] for r in self.db.execute("SELECT phrase, sku FROM learned_alias")}

    def learn(self, phrase, sku):
        """Remember a correction. It is also added to the product's visible customer words, so the
        shopkeeper can see it in the price list and delete it there if it was a mistake."""
        skus = self.skus()
        target = next((s for s in skus if s.sku == sku), None)
        phrase = (phrase or "").strip()
        if not phrase or target is None:
            return
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO learned_alias VALUES (?, ?)", (phrase, sku))
        if phrase.lower() not in {a.lower() for a in target.aliases} and phrase.lower() != target.name.lower():
            target.aliases.append(phrase)
            self.replace_skus(skus)

    def forget_missing_aliases(self):
        """Drop learned corrections whose word was removed from the product in the price list."""
        by_id = self.sku_map()
        with self.db:
            for row in self.db.execute("SELECT phrase, sku FROM learned_alias").fetchall():
                sku = by_id.get(row["sku"])
                if sku is None or row["phrase"].lower() not in {a.lower() for a in sku.aliases}:
                    self.db.execute("DELETE FROM learned_alias WHERE phrase = ?", (row["phrase"],))

    # orders
    def add_order(self, customer, phone, raw, lines, total):
        with self.db:
            cur = self.db.execute(
                "INSERT INTO orders (created, customer, phone, raw, lines, total) VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), customer, phone, raw, json.dumps(lines), str(total)))
        return cur.lastrowid

    def orders(self, limit=500):
        rows = self.db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "lines": json.loads(r["lines"])} for r in rows]

    def set_status(self, order_id, status):
        if status not in {"new", "packed", "delivered", "paid"}:
            raise ValueError("status must be new, packed, delivered or paid")
        with self.db:
            cur = self.db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        return cur.rowcount == 1
