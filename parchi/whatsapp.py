"""Parse a WhatsApp "Export chat (without media)" .txt file into messages."""

import re

# Android: "15/10/26, 9:41 am - Mrs Joshi: message"   iPhone: "[15/10/26, 9:41:02 AM] Mrs Joshi: message"
_LINE = re.compile(
    r"^‎?\[?(?P<date>\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}),?\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?"
    r"(?:\s?[apAP]\.?\s?[mM]\.?)?)\]?\s*(?:-\s*)?(?P<sender>[^:]{1,60}):\s?(?P<text>.*)$")
# A line that starts with a date/time but has no "Name:" is a system notice (encryption, joins, ...).
_SYSTEM = re.compile(r"^\u200e?\[?\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4},?\s+\d{1,2}:\d{2}")
_MEDIA = re.compile(r"\u200e?(<attached:.*>|<?[a-z ]*omitted>?)")
_SKIP = ("<media omitted>", "this message was deleted", "messages and calls are end-to-end encrypted",
         "you deleted this message", "null")


def parse_chat(text):
    messages = []
    for raw_line in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.replace(" ", " ").strip()
        if not line:
            continue
        m = _LINE.match(line)
        if m:
            messages.append({"date": m["date"], "time": m["time"].strip(),
                             "sender": m["sender"].strip().lstrip("‎~ "), "text": m["text"].strip()})
        elif _SYSTEM.match(line):
            continue
        elif messages:
            messages[-1]["text"] += "\n" + line
    return [m for m in messages if m["text"] and m["text"].strip().lower() not in _SKIP
            and not _MEDIA.fullmatch(m["text"].strip().lower())]
