"""Self-check for store.py. Run: python3 test_store.py

Covers the paths where a bug costs something: one-time codes, expiry,
rate limiting, claim dedup, and double-resolving a review.
"""
import tempfile, os
from store import Store


def fresh() -> Store:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return Store(path)


def test_codes():
    s = fresh()
    s.create_code(1, "KD8AAA", "A@Ex.com", "123456", ttl_min=15)

    assert s.consume_code(1, "999999") is None, "wrong code accepted"
    assert s.consume_code(2, "123456") is None, "another user's code accepted"

    assert s.consume_code(1, "123456") == ("KD8AAA", "a@ex.com"), "email not lowercased"
    assert s.consume_code(1, "123456") is None, "code reusable — not one-time"


def test_expiry():
    s = fresh()
    s.create_code(1, "KD8AAA", "a@ex.com", "123456", ttl_min=-1)  # already expired
    assert s.consume_code(1, "123456") is None, "expired code accepted"


def test_invalidate():
    s = fresh()
    s.create_code(1, "KD8AAA", "a@ex.com", "111111", ttl_min=15)
    s.create_code(1, "KD8AAA", "a@ex.com", "222222", ttl_min=15)
    s.invalidate_active_codes(1)
    assert s.consume_code(1, "222222") is None, "invalidate left a code live"
    assert s.consume_code(1, "111111") is None, "invalidate left a code live"


def test_rate_limit():
    s = fresh()
    assert s.recent_code_count(1) == 0
    for _ in range(3):
        s.create_code(1, "KD8AAA", "a@ex.com", "123456", ttl_min=15)
    assert s.recent_code_count(1) == 3
    assert s.recent_code_count(2) == 0, "rate limit leaks across users"


def test_claims():
    s = fresh()
    s.add_claim("KD8AAA", 1, "A@Ex.com")
    assert s.claim_by_callsign("KD8AAA")["discord_user_id"] == 1
    assert s.claim_by_email("a@ex.com") is not None, "email lookup not case-folded"
    assert s.claim_by_email("A@EX.COM") is not None, "email lookup not case-folded"
    assert s.claim_by_callsign("KD8BBB") is None
    s.release_claim("KD8AAA")
    assert s.claim_by_callsign("KD8AAA") is None
    assert s.claimed_count() == 0


def test_review():
    s = fresh()
    qid = s.open_review(1, "KD8AAA", "a@ex.com", "officer_grant")
    assert s.open_review_count() == 1
    assert s.open_review_for_user(1)["id"] == qid
    assert s.resolve_review(qid, 99, "approve") is True
    # double-click on the Approve button must not resolve twice
    assert s.resolve_review(qid, 98, "deny") is False, "review resolved twice"
    assert s.open_review_count() == 0
    assert s.open_review_for_user(1) is None


def test_roster():
    s = fresh()
    s.replace_roster([("KD8AAA", "Jim", "a@ex.com", True),
                      ("KD8BBB", "Ann", "b@ex.com", False)])
    assert s.roster_count() == 2
    assert s.roster_officer_count() == 1
    assert s.roster_lookup("KD8AAA")["is_officer"] == 1
    assert s.roster_lookup("NOPE") is None
    s.replace_roster([("KD8BBB", "Ann", "b@ex.com", False)])
    assert s.roster_count() == 1, "replace_roster did not clear old rows"
    assert s.roster_lookup("KD8AAA") is None, "dropped member still on roster"


def test_welcome_lines_render():
    # Read the literal out of bot.py rather than importing it — bot.py pulls in
    # discord.py, which this test doesn't need.
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(__file__).with_name("bot.py").read_text())
    lines = [
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and getattr(n.targets[0], "id", None) == "WELCOME_LINES"
    ]
    assert lines, "WELCOME_LINES not found in bot.py"

    for t in lines[0]:
        out = t.format(name="James Flattery", call="KO4CFG", mention="<@1>")
        assert "{" not in out, f"unsubstituted placeholder in: {t}"
        for word in (" he ", " she ", " his ", " her ", " him "):
            assert word not in f" {out.lower()} ", f"gendered pronoun in: {t}"


def test_code_attempts():
    s = fresh()
    s.create_code(1, "KD8AAA", "a@ex.com", "123456", ttl_min=15)
    for _ in range(4):
        assert s.consume_code(1, "000000") is None
    assert s.consume_code(1, "123456") == ("KD8AAA", "a@ex.com"), "right code refused after 4 wrong tries"
    s.create_code(1, "KD8AAA", "a@ex.com", "654321", ttl_min=15)
    for _ in range(5):
        s.consume_code(1, "000000")
    assert s.consume_code(1, "654321") is None, "code still usable after 5 wrong tries"


def test_settings():
    s = fresh()
    assert s.get_setting("welcome_message_id") is None
    s.set_setting("welcome_message_id", 1234567890123)
    s.set_setting("welcome_message_id", 987)
    assert s.get_setting("welcome_message_id") == 987


def test_parse_roster():
    from roster import parse_roster
    rows = parse_roster([{"callsign": "kd8aaa", "name": "Amy Able", "email": "Amy@Example.org", "officer": True},
                         {"callsign": None, "name": "No Call", "email": "x@example.org", "officer": False},
                         {"callsign": "KD8AAA", "name": "Dup", "email": "d@example.org", "officer": False},
                         {"callsign": "KD8BBB", "name": "Bob", "email": "", "officer": "true"}])
    assert rows == [("KD8AAA", "Amy Able", "amy@example.org", True)], rows


def test_transfer_claim():
    s = fresh()
    s.add_claim("KD8AAA", 1, "a@ex.com")
    assert s.transfer_claim("KD8AAA", "W8AMY") == "moved"
    assert s.claim_by_callsign("W8AMY")["discord_user_id"] == 1 and s.claim_by_callsign("KD8AAA") is None
    assert s.transfer_claim("KD8AAA", "W8AMY") == "none"  # the next sync: nothing left to move
    s.add_claim("KD8BBB", 2, "b@ex.com")
    s.add_claim("W8BOB", 3, "c@ex.com")
    assert s.transfer_claim("KD8BBB", "W8BOB") == "conflict" and s.claim_by_callsign("KD8BBB")["discord_user_id"] == 2
    s.add_claim("KD8CCC", 4, "d@ex.com")
    s.add_claim("W8CAL", 4, "d2@ex.com")  # re-verified with the new call already
    assert s.transfer_claim("KD8CCC", "W8CAL") == "same user" and s.claim_by_callsign("KD8CCC") is None


def test_callsign_changes():
    from roster import callsign_changes
    assert callsign_changes([{"callsign": "w8amy", "former": ["kd8aaa", "W8AMY"]}, {"callsign": "KD8BBB"}]) == {"KD8AAA": "W8AMY"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name} OK")
    print("all passed")
