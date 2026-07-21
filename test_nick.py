"""Self-check for the nickname format. Run: python3 test_nick.py

Needs discord.py installed (bot.py imports it), so run it inside the venv:
    ./venv/bin/python test_nick.py
"""
from bot import NICK_MAX, agreed, club_nick

CASES = [
    ("John Doe", "N0CALL", "John D - N0CALL"),
    ("john doe", "N0CALL", "john D - N0CALL"),      # initial is upper, first name isn't touched
    ("John Doe Jr", "N0CALL", "John D - N0CALL"),   # suffix dropped, not used as the initial
    ("John Doe Jr.", "N0CALL", "John D - N0CALL"),
    ("John Quincy Doe", "N0CALL", "John D - N0CALL"),
    ("John", "N0CALL", "John - N0CALL"),            # single name: no initial
    ("", "N0CALL", "N0CALL"),                       # roster has no name
    ("   ", "N0CALL", "N0CALL"),
    ("Bartholomewlongname Doe", "KD8AAAXY", "Bartholomewlongname D - KD8AAAXY"),  # exactly 32
    ("Bartholomewextralongname Doe", "KD8AAAXY", "Bartholomewextralon D - KD8AAAXY"),  # truncated
]

for full_name, call, want in CASES:
    got = club_nick(full_name, call)
    assert got == want, f"{full_name!r} + {call} -> {got!r}, wanted {want!r}"
    assert len(got) <= NICK_MAX, f"{got!r} is {len(got)} chars, over Discord's limit"

assert agreed("I AGREE") and agreed(" i Agree. ") and agreed("I agree!")
assert not agreed("agree") and not agreed("") and not agreed("I do not agree")

print("ok")
