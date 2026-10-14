"""Quick manual check: run a few sample orders through the real model and the matcher."""
import sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from parchi import reader, matcher
from parchi.store import Store

st = Store(":memory:")
for msg in ["bhaiya 2 kilo atta, 1 amul butter chhota, 6 ande, ek maggi",
            "दादा अर्धा किलो साखर, एक लिटर दूध आणि 1 पाव पाठवा",
            "Hi, pls send 500 gm poha, 2 kg kanda, 1 dozen eggs and tata tea",
            "1 packet parle g, dahi, paneer aur thoda dhaniya bhi",
            "ok thank you bhaiya 🙏"]:
    t = time.time(); r = reader.read_order(msg)
    print(f"--- {msg}  ({time.time()-t:.1f}s) is_order={r['is_order']}")
    for ln in matcher.match_order(r["items"], st.skus(), {}):
        print("  ", ln["status"], "|", ln["raw"], "->", ln["sku"], ln["qty"], "|", ln["note"])
