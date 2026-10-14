"""Accuracy check: 20 made-up WhatsApp orders (Hinglish, Marathi, Hindi, English) against the
sample price list. A line counts as correct only if Parchi picks the right product AND quantity
with no human help ("ok" status). Run: python scripts/eval.py"""
import sys, time
from decimal import Decimal
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from parchi import matcher, reader
from parchi.store import Store

CASES = [
    ("bhaiya 2 kilo atta aur 6 ande bhejna", {"atta-loose": "2", "eggs": "6"}),
    ("1 amul butter chhota, 1 litre doodh", {"butter-amul-100g": "1", "milk-amul-1l": "1"}),
    ("दादा अर्धा किलो साखर आणि एक लिटर दूध", {"sugar": "0.5", "milk-amul-1l": "1"}),
    ("500 gm poha, 2 kg kanda", {"poha": "0.5", "onion": "2"}),
    ("1 dozen eggs please", {"eggs": "12"}),
    ("2 packet parle g aur 1 tata tea", {"parle-g-250g": "2", "tea-tata-250g": "1"}),
    ("dedh kilo aloo, aadha kilo tamatar", {"potato": "1.5", "tomato": "0.5"}),
    ("1 kg tur dal 2 kg chawal", {"toor-dal": "1", "rice-kolam": "2"}),
    ("मला 2 किलो बटाटा आणि 1 किलो कांदा हवा आहे", {"potato": "2", "onion": "1"}),
    ("मुझे 1 किलो चीनी और 2 पैकेट मैगी बड़ी चाहिए", {"sugar": "1", "maggi-280g": "2"}),
    ("send 1 fortune oil and 1 tata salt", {"oil-sunflower-1l": "1", "salt-tata-1kg": "1"}),
    ("2 dahi, 1 bread", {"dahi-400g": "2", "bread": "1"}),
    ("ek amul butter bada", {"butter-amul-500g": "1"}),
    ("3 maggi chhoti wali", {"maggi-70g": "3"}),
    ("1 aashirvaad atta 5 kg ka packet", {"atta-aashirvaad-5kg": "1"}),
    ("paav kilo jeera", {"jeera-100g": None}),          # 250 g asked, sold in 100 g packs -> should be flagged
    ("2 litre dudh aur 12 ande", {"milk-amul-1l": "2", "eggs": "12"}),
    ("kothimbir 2 judi, 1 kg tomato", {"coriander": "2", "tomato": "1"}),
    ("1 laadi pav and 1 packet parle", {"pav-6": "1", "parle-g-250g": "1"}),
    ("thank you bhaiya, payment kar diya", {}),
]

st = Store(":memory:")
right = total = flagged_ok = 0
times = []
for msg, want in CASES:
    t = time.perf_counter(); res = reader.read_order(msg); times.append(time.perf_counter() - t)
    lines = matcher.match_order(res["items"], st.skus(), {})
    got = {ln["sku"]: (ln["qty"], ln["status"]) for ln in lines if ln["sku"]}
    marks = []
    for sku, qty in want.items():
        total += 1
        g = got.get(sku)
        if qty is None:                       # we expect the shopkeeper to be asked
            ok = g is not None and g[1] == "check"
            flagged_ok += ok
        else:
            ok = g is not None and g[1] == "ok" and Decimal(g[0]) == Decimal(qty)
        right += ok
        marks.append(("✓" if ok else "✗") + f" {sku}")
    if not want:
        total += 1; ok = not res["is_order"]; right += ok; marks.append(("✓" if ok else "✗") + " not an order")
    print(f"{msg[:55]:55} | {', '.join(marks)}")
    for ln in lines:
        if ln["sku"] not in want or ln["status"] != "ok":
            print(f"{'':55} |   {ln['status']}: {ln['raw']} -> {ln['sku']} {ln['qty']} ({ln['note']})")
print(f"\nCorrect without help: {right}/{total} lines ({100*right/total:.0f}%). "
      f"Median read time {sorted(times)[len(times)//2]:.1f}s, model {reader.MODEL}.")
