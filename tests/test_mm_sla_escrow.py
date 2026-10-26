import json
from pathlib import Path

CONTRACT = "contracts/mm_sla_escrow.py"
FIX = Path(__file__).parent / "fixtures"
GEN = 10**18

# Real order books saved from the public Upbit and Bithumb APIs on 2026-10-26.
# "_a" was fetched at 00:55:40 UTC and "_b" 20 seconds later.
BOOK = {p.stem: p.read_text(encoding="utf-8") for p in FIX.glob("*.json")}

START = 1792972800                       # 2026-10-26T00:00:00Z
HOUR = 3600
AT_A = "2026-10-26T00:55:41+00:00"       # slot 0, right after snapshot _a
SLOT1 = "2026-10-26T01:10:00+00:00"
AFTER_4H = "2026-10-26T04:00:00+00:00"

# a thin altcoin on Upbit: 50 bps spread, 3M won each side within 1% of mid
SOPH = {"max_spread_bps": 50, "band_bps": 100, "min_depth_krw": 3_000_000, "min_uptime_pct": 75}
# a major on Upbit: 20 bps, 300M won each side within 1%
XRP = {"max_spread_bps": 20, "band_bps": 100, "min_depth_krw": 300_000_000, "min_uptime_pct": 75}
RETAINER = 40 * GEN


def hexaddr(a):
    return "0x" + bytes(a).hex()


def serve(vm, venue, market, snap=None, status=200, body=None):
    host = {"upbit": r"api\.upbit\.com", "bithumb": r"api\.bithumb\.com"}[venue]
    if body is None:
        body = BOOK[f"{venue}_{market}_{snap}"]
    vm.mock_web(rf"{host}/v1/orderbook\?markets={market}$", {"status": status, "body": body})


def setup(vm, deploy, project, mm, venue="upbit", market="KRW-SOPH", terms=SOPH, n_slots=4, c=None):
    """A new agreement; on contract c if given (a test can only deploy once)."""
    vm.warp("2026-10-25T23:30:00+00:00")
    c = c or deploy(CONTRACT)
    vm.sender = project
    vm.value = RETAINER
    aid = c.create_agreement(hexaddr(mm), venue, market, json.dumps(terms), START, HOUR, n_slots)
    vm.value = 0
    return c, aid


def check(vm, c, aid, who, venue, market, snap, at=AT_A, **kw):
    vm.warp(at)
    vm.clear_mocks()
    serve(vm, venue, market, snap, **kw)
    vm.sender = who
    return c.check(aid)


def test_create_agreement_validation(direct_vm, direct_deploy, direct_alice, direct_bob):
    direct_vm.warp("2026-10-25T23:30:00+00:00")
    c = direct_deploy(CONTRACT)
    direct_vm.sender = direct_alice
    mm, terms = hexaddr(direct_bob), json.dumps(SOPH)
    with direct_vm.expect_revert("send the retainer"):
        c.create_agreement(mm, "upbit", "KRW-SOPH", terms, START, HOUR, 4)
    direct_vm.value = RETAINER
    for args, msg in [
        ((mm, "binance", "KRW-SOPH", terms, START, HOUR, 4), "venue must be one of"),
        ((mm, "upbit", "SOPH/KRW", terms, START, HOUR, 4), "market looks like"),
        ((hexaddr(direct_alice), "upbit", "KRW-SOPH", terms, START, HOUR, 4), "must differ"),
        ((mm, "upbit", "KRW-SOPH", terms, START + 31 * 86400, HOUR, 4), "within 30 days"),
        ((mm, "upbit", "KRW-SOPH", terms, START, 60, 4), "interval_s"),
        ((mm, "upbit", "KRW-SOPH", terms, START, HOUR, 745), "n_slots"),
        ((mm, "upbit", "KRW-SOPH", json.dumps(dict(SOPH, band_bps=5)), START, HOUR, 4), "band_bps"),
        ((mm, "upbit", "KRW-SOPH", json.dumps({"max_spread_bps": 50}), START, HOUR, 4), "terms need"),
    ]:
        with direct_vm.expect_revert(msg):
            c.create_agreement(*args)
    assert c.create_agreement(mm, "upbit", "KRW-SOPH", terms, START, HOUR, 4) == 0
    a = c.get_agreement(0)
    assert a["retainer"] == RETAINER and a["terms"] == SOPH and a["passed"] == 0


def test_deep_book_passes_and_validators_agree_across_the_20s_gap(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob, market="KRW-XRP", terms=XRP)
    out = check(direct_vm, c, aid, direct_bob, "upbit", "KRW-XRP", "a")
    assert out["pass"] is True and out["slot"] == 0 and out["slot_status"] == "pass"
    assert out["best_bid"] == "1728.0" and out["best_ask"] == "1729.0" and out["spread_bps"] == 6
    assert out["bid_depth"] > 2_000_000_000 and out["ask_depth"] > 1_000_000_000
    assert direct_vm.run_validator() is True
    # a validator that fetched 20 seconds later sees a different book, same verdict
    direct_vm.clear_mocks()
    serve(direct_vm, "upbit", "KRW-XRP", "b")
    assert direct_vm.run_validator() is True
    assert c.get_agreement(aid)["passed"] == 1


def test_thin_book_fails_the_slot_for_good(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob)
    # the project checks while the ask side has 0.4M won within 1%
    out = check(direct_vm, c, aid, direct_alice, "upbit", "KRW-SOPH", "a")
    assert out["pass"] is False and out["slot_status"] == "fail"
    assert out["spread_bps"] == 29 and out["ask_depth"] < 500_000 and out["bid_depth"] > 4_000_000
    assert direct_vm.run_validator() is True
    # the MM checking again with a better book can't undo it
    with direct_vm.expect_revert("slot 0 has already failed"):
        check(direct_vm, c, aid, direct_bob, "upbit", "KRW-SOPH", "b")
    a = c.get_agreement(aid)
    assert a["passed"] == 0 and a["failed"] == 1
    assert c.get_slot(aid, 0)["by"].lower() == hexaddr(direct_alice)


def test_a_later_failing_check_overturns_a_passing_slot(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob)
    out = check(direct_vm, c, aid, direct_bob, "upbit", "KRW-SOPH", "b")
    assert out["pass"] is True and out["checks"] == 1
    assert c.get_agreement(aid)["passed"] == 1
    out = check(direct_vm, c, aid, direct_charlie, "upbit", "KRW-SOPH", "a")   # anyone can check
    assert out["slot_status"] == "fail" and out["checks"] == 2
    a = c.get_agreement(aid)
    assert a["passed"] == 0 and a["failed"] == 1


def test_checks_per_slot_are_capped(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob)
    for _ in range(3):
        check(direct_vm, c, aid, direct_bob, "upbit", "KRW-SOPH", "b")
    with direct_vm.expect_revert("already has 3 checks"):
        check(direct_vm, c, aid, direct_alice, "upbit", "KRW-SOPH", "a")
    # the next slot is a fresh one
    out = check(direct_vm, c, aid, direct_alice, "upbit", "KRW-SOPH", "a", at=SLOT1)
    assert out["slot"] == 1 and out["slot_status"] == "fail"


def test_validator_rejects_a_false_leader(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob)
    honest = check(direct_vm, c, aid, direct_alice, "upbit", "KRW-SOPH", "a")
    honest = {k: honest[k] for k in honest if k not in ("slot", "slot_status", "checks")}
    serve(direct_vm, "upbit", "KRW-SOPH", "a")
    assert direct_vm.run_validator(leader_result=honest) is True
    # says pass, numbers don't support it
    assert direct_vm.run_validator(leader_result=dict(honest, **{"pass": True})) is False
    # inflates the ask side so its own numbers pass; this validator's book clearly fails
    assert direct_vm.run_validator(leader_result=dict(honest, ask_depth=9_000_000, **{"pass": True})) is False
    # spread that doesn't follow from its own best bid/ask
    assert direct_vm.run_validator(leader_result=dict(honest, spread_bps=12)) is False
    # an old book: ten minutes before this validator's
    assert direct_vm.run_validator(leader_result=dict(honest, ts=honest["ts"] - 600_000)) is False
    # another market's book (XRP) passed off as SOPH
    xrp = json.loads(BOOK["upbit_KRW-XRP_a"])[0]
    xrp["market"] = "KRW-SOPH"
    direct_vm.clear_mocks()
    serve(direct_vm, "upbit", "KRW-SOPH", body=json.dumps([xrp]))
    fake = check(direct_vm, c, aid, direct_bob, "upbit", "KRW-SOPH", None, at=SLOT1, body=json.dumps([xrp]))
    fake = {k: fake[k] for k in fake if k not in ("slot", "slot_status", "checks")}
    direct_vm.clear_mocks()
    serve(direct_vm, "upbit", "KRW-SOPH", "b")
    assert direct_vm.run_validator(leader_result=fake) is False


def test_book_moves_between_fetches_only_clear_contradictions_count(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob)
    # leader fetched _a (fails: thin ask side); a validator 20 s later sees _b, which passes,
    # but only with 28 bps against a 50 bps limit: not a clear pass, so it goes along
    check(direct_vm, c, aid, direct_alice, "upbit", "KRW-SOPH", "a")
    direct_vm.clear_mocks()
    serve(direct_vm, "upbit", "KRW-SOPH", "b")
    assert direct_vm.run_validator() is True
    # the other way round: leader saw _b (pass), the validator's _a has 0.4M against 3M,
    # a clear fail, so it disagrees. Real books 20 s apart can split validators.
    c2, aid2 = setup(direct_vm, direct_deploy, direct_alice, direct_bob, c=c)
    check(direct_vm, c2, aid2, direct_bob, "upbit", "KRW-SOPH", "b")
    direct_vm.clear_mocks()
    serve(direct_vm, "upbit", "KRW-SOPH", "a")
    assert direct_vm.run_validator() is False


def test_bithumb_wide_spread_fails(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob, venue="bithumb")
    out = check(direct_vm, c, aid, direct_bob, "bithumb", "KRW-SOPH", "a")
    assert out["pass"] is False and out["spread_bps"] == 146
    assert out["best_bid"] == "2.53" and out["best_ask"] == "2.567"
    direct_vm.clear_mocks()
    serve(direct_vm, "bithumb", "KRW-SOPH", "b")      # 75 bps: still a fail, agrees
    assert direct_vm.run_validator() is True


def test_zero_size_levels_do_not_set_the_spread(direct_vm, direct_deploy, direct_alice, direct_bob):
    # saved at 01:45:29 UTC: Bithumb's top bid level is 2.575 with size 0. Counting it would
    # give 20 bps; the first real bid is 2.561, so the spread is 74 bps.
    book = json.loads(BOOK["bithumb_KRW-SOPH_zero"])[0]["orderbook_units"][0]
    assert book["bid_price"] == 2.575 and book["bid_size"] == 0
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob, venue="bithumb")
    out = check(direct_vm, c, aid, direct_bob, "bithumb", "KRW-SOPH", "zero", at="2026-10-26T01:45:30+00:00")
    assert out["best_bid"] == "2.561" and out["best_ask"] == "2.58" and out["spread_bps"] == 74
    assert out["pass"] is False
    assert direct_vm.run_validator() is True


def test_exchange_errors_record_nothing(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob, venue="bithumb", market="KRW-NOPE")
    # Bithumb answers an unknown market with HTTP 200 and an error object
    with direct_vm.expect_revert("[EXTERNAL] bithumb has no market KRW-NOPE"):
        check(direct_vm, c, aid, direct_bob, "bithumb", "KRW-NOPE", None,
              body='{"error":{"name":404,"message":"Code not found"}}')
    c2, aid2 = setup(direct_vm, direct_deploy, direct_alice, direct_bob, market="KRW-NOPE", c=c)
    with direct_vm.expect_revert("[EXTERNAL] upbit has no market KRW-NOPE"):
        check(direct_vm, c2, aid2, direct_bob, "upbit", "KRW-NOPE", None, status=404,
              body='{"error":{"name":404,"message":"Code not found"}}')
    with direct_vm.expect_revert("[TRANSIENT] upbit returned HTTP 429"):
        check(direct_vm, c2, aid2, direct_bob, "upbit", "KRW-NOPE", None, status=429, body="Too Many Requests")
    assert c2.get_slot(aid2, 0) == {"status": "unchecked", "checks": 0}


def test_empty_side_and_truncated_depth(direct_vm, direct_deploy, direct_alice, direct_bob):
    # every bid pulled: nothing to measure a spread against
    book = json.loads(BOOK["upbit_KRW-SOPH_a"])[0]
    for u in book["orderbook_units"]:
        u["bid_price"], u["bid_size"] = 0, 0
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob)
    out = check(direct_vm, c, aid, direct_alice, "upbit", "KRW-SOPH", None, body=json.dumps([book]))
    assert out["pass"] is False and out["spread_bps"] == 10_000 and out["bid_depth"] == 0
    # XRP with a 2% band: all 30 visible levels are inside it, so depth is a lower bound
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob, market="KRW-XRP", terms=dict(XRP, band_bps=200), c=c)
    out = check(direct_vm, c, aid, direct_bob, "upbit", "KRW-XRP", "a")
    assert out["bid_truncated"] is True and out["ask_truncated"] is True and out["pass"] is True


def test_outside_the_term(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob)
    assert c.current_slot(aid) == -1
    with direct_vm.expect_revert("outside the agreement term"):
        check(direct_vm, c, aid, direct_bob, "upbit", "KRW-SOPH", "b", at="2026-10-25T23:59:00+00:00")
    with direct_vm.expect_revert("outside the agreement term"):
        check(direct_vm, c, aid, direct_bob, "upbit", "KRW-SOPH", "b", at=AFTER_4H)


def test_settle_pays_pro_rata_and_unchecked_slots_count_as_failed(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob, market="KRW-XRP",
                   terms=dict(XRP, min_uptime_pct=50))
    for h in range(3):                                # slot 3 is never checked
        check(direct_vm, c, aid, direct_bob, "upbit", "KRW-XRP", "a", at=f"2026-10-26T0{h}:30:00+00:00")
    direct_vm.sender = direct_charlie
    direct_vm.warp("2026-10-26T03:59:59+00:00")
    with direct_vm.expect_revert("has not ended"):
        c.settle(aid)
    direct_vm.warp(AFTER_4H)
    out = c.settle(aid)
    assert out == {"passed": 3, "slots": 4, "met_minimum": True, "to_mm": 30 * GEN, "to_project": 10 * GEN}
    assert c.get_agreement(aid)["paid_mm"] == 30 * GEN
    with direct_vm.expect_revert("already settled"):
        c.settle(aid)


def test_below_minimum_uptime_pays_nothing(direct_vm, direct_deploy, direct_alice, direct_bob):
    c, aid = setup(direct_vm, direct_deploy, direct_alice, direct_bob)       # needs 75%
    check(direct_vm, c, aid, direct_bob, "upbit", "KRW-SOPH", "b")           # slot 0 pass
    check(direct_vm, c, aid, direct_bob, "upbit", "KRW-SOPH", "b", at=SLOT1)  # slot 1 pass
    check(direct_vm, c, aid, direct_alice, "upbit", "KRW-SOPH", "a", at="2026-10-26T02:10:00+00:00")
    direct_vm.warp(AFTER_4H)
    out = c.settle(aid)
    assert out == {"passed": 2, "slots": 4, "met_minimum": False, "to_mm": 0, "to_project": RETAINER}
