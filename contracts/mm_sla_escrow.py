# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
MarketMakerSLA: pay a market maker's retainer only for the hours its order book
actually met the agreement, measured on the exchange itself.

When a token lists on a Korean won exchange (Upbit, Bithumb), the project usually
hires a market maker: a monthly retainer for keeping the spread tight and enough
size on both sides. Whether the MM did that is argued afterwards from screenshots
and the MM's own reports. Neither side can check the other's numbers.

Here the terms are fixed when the project funds the retainer:

  create_agreement  the project deposits the retainer and fixes venue, market,
                    max spread, the price band around mid, the minimum won depth
                    each side must have inside that band, the slot length (e.g.
                    1 hour), the number of slots, and the minimum share of slots
                    below which nothing is paid.
  check             anyone, at any time inside the term. Validators fetch the
                    public order book, compute spread and depth in code and
                    record whether the current slot passes. One failing check
                    fails the slot for good; a slot nobody checked counts as
                    failed. So the MM has to prove every slot, and the project
                    can check whenever it suspects the book is thin.
  settle            anyone, after the term. passed / total slots of the retainer
                    goes to the MM (zero below the minimum share), the rest back
                    to the project.

No LLM is involved: everything in the agreement is a number the order book shows.
What GenLayer adds is that each validator fetches the book itself. Validators fetch
seconds apart and a thin book can change a lot in that time (a 20 s gap moved
one-side depth on KRW-SOPH from 0.4M to 9.8M won in the fixtures), so a validator
does not demand the leader's exact numbers. It rejects the leader only when its own
book clearly contradicts the leader's verdict, or when the leader's numbers do not
add up, are stale, or are for a different price level.
"""
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re

BOOK_URL = {
    "upbit": "https://api.upbit.com/v1/orderbook?markets={m}",
    "bithumb": "https://api.bithumb.com/v1/orderbook?markets={m}",
}
MARKET_RE = re.compile(r"^KRW-[A-Z0-9]{1,15}$")

MIN_INTERVAL_S = 600
MAX_INTERVAL_S = 86_400
MAX_SLOTS = 744                 # 31 days of hourly slots
MAX_CHECKS_PER_SLOT = 3         # after that a passing slot stays passed
MAX_START_DELAY_S = 30 * 86_400

# validator tolerances (see README "Why validators don't compare exact numbers")
MAX_SKEW_MS = 180_000           # leader's book timestamp vs this validator's
MID_TOL_BPS = 300               # leader's mid vs this validator's mid
CLEAR_FACTOR = 2                # "clearly" = off by at least 2x from the threshold

ERR_EXPECTED = "[EXPECTED]"
ERR_EXTERNAL = "[EXTERNAL]"
ERR_TRANSIENT = "[TRANSIENT]"

OBS_KEYS = ("ts", "mid", "best_bid", "best_ask", "spread_bps", "bid_depth", "ask_depth",
            "bid_truncated", "ask_truncated", "pass")


@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


@allow_storage
@dataclass
class Agreement:
    project: Address
    mm: Address
    venue: str
    market: str
    terms: str               # JSON {max_spread_bps, band_bps, min_depth_krw, min_uptime_pct}
    retainer: u256
    start: u256
    interval_s: u256
    n_slots: u256
    passed: u256             # slots currently marked pass
    failed: u256             # slots marked fail by a check
    settled: bool
    paid_mm: u256


# ---------- deterministic helpers ----------

def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _parse_terms(terms_json: str) -> dict:
    try:
        t = json.loads(terms_json)
        assert isinstance(t, dict)
        out = {k: int(t[k]) for k in ("max_spread_bps", "band_bps", "min_depth_krw", "min_uptime_pct")}
    except Exception:
        raise gl.vm.UserError(f"{ERR_EXPECTED} terms need max_spread_bps, band_bps, min_depth_krw, min_uptime_pct (integers)")
    if not (1 <= out["max_spread_bps"] <= 2000):
        raise gl.vm.UserError(f"{ERR_EXPECTED} max_spread_bps must be 1-2000")
    if not (10 <= out["band_bps"] <= 1000):
        raise gl.vm.UserError(f"{ERR_EXPECTED} band_bps must be 10-1000")
    if not (1 <= out["min_depth_krw"] <= 10**13):
        raise gl.vm.UserError(f"{ERR_EXPECTED} min_depth_krw must be positive")
    if not (0 <= out["min_uptime_pct"] <= 100):
        raise gl.vm.UserError(f"{ERR_EXPECTED} min_uptime_pct must be 0-100")
    return out


def _passes(obs: dict, terms: dict) -> bool:
    return (obs["spread_bps"] <= terms["max_spread_bps"]
            and obs["bid_depth"] >= terms["min_depth_krw"]
            and obs["ask_depth"] >= terms["min_depth_krw"])


def _measure(book: dict, terms: dict) -> dict:
    """One order book snapshot -> the numbers the agreement is about. Pure function."""
    units = book.get("orderbook_units") or []
    try:
        # Bithumb lists levels with size 0 (seen live at the top of the book): skip them,
        # or an empty price would set the spread
        bids = [(float(u["bid_price"]), float(u["bid_size"])) for u in units]
        asks = [(float(u["ask_price"]), float(u["ask_size"])) for u in units]
        ts = int(book["timestamp"])
    except Exception:
        raise gl.vm.UserError(f"{ERR_TRANSIENT} unexpected order book format")
    bids = [(p, s) for p, s in bids if p > 0 and s > 0]
    asks = [(p, s) for p, s in asks if p > 0 and s > 0]
    if not bids or not asks or asks[0][0] <= bids[0][0]:
        # an empty side or a crossed book: nothing to quote against
        return {"ts": ts, "mid": "0", "best_bid": "0", "best_ask": "0", "spread_bps": 10_000,
                "bid_depth": 0, "ask_depth": 0, "bid_truncated": False, "ask_truncated": False,
                "pass": False}
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2
    lo, hi = mid * (1 - terms["band_bps"] / 10_000), mid * (1 + terms["band_bps"] / 10_000)
    bid_depth = sum(p * s for p, s in bids if p >= lo)
    ask_depth = sum(p * s for p, s in asks if p <= hi)
    obs = {
        "ts": ts,
        "mid": repr(round(mid, 8)),
        "best_bid": repr(bid), "best_ask": repr(ask),
        # rounded against the MM: spread up, depth down
        "spread_bps": math.ceil((ask - bid) / mid * 10_000 - 1e-9),
        "bid_depth": int(bid_depth), "ask_depth": int(ask_depth),
        # the API shows 30 levels; if they all sit inside the band the true depth is larger
        "bid_truncated": bids[-1][0] >= lo, "ask_truncated": asks[-1][0] <= hi,
    }
    obs["pass"] = _passes(obs, terms)
    return obs


def _fetch_book(venue: str, market: str) -> dict:
    resp = gl.nondet.web.get(BOOK_URL[venue].format(m=market))
    if resp.status == 404:
        raise gl.vm.UserError(f"{ERR_EXTERNAL} {venue} has no market {market}")
    if resp.status != 200:
        raise gl.vm.UserError(f"{ERR_TRANSIENT} {venue} returned HTTP {resp.status}")
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except Exception:
        raise gl.vm.UserError(f"{ERR_TRANSIENT} {venue} returned something that isn't JSON")
    if isinstance(data, dict) and "error" in data:
        # Bithumb answers an unknown market with HTTP 200 and an error object
        raise gl.vm.UserError(f"{ERR_EXTERNAL} {venue} has no market {market}")
    if not isinstance(data, list) or len(data) != 1 or data[0].get("market") != market:
        raise gl.vm.UserError(f"{ERR_TRANSIENT} {venue} returned an unexpected order book")
    return data[0]


def _observe(venue: str, market: str, terms: dict) -> dict:
    return _measure(_fetch_book(venue, market), terms)


def _clearly_passes(obs: dict, terms: dict) -> bool:
    return (obs["spread_bps"] * CLEAR_FACTOR <= terms["max_spread_bps"]
            and min(obs["bid_depth"], obs["ask_depth"]) >= terms["min_depth_krw"] * CLEAR_FACTOR)


def _clearly_fails(obs: dict, terms: dict) -> bool:
    return (obs["spread_bps"] >= terms["max_spread_bps"] * CLEAR_FACTOR
            or min(obs["bid_depth"], obs["ask_depth"]) * CLEAR_FACTOR <= terms["min_depth_krw"])


def _leader_ok(leader: dict, mine: dict, terms: dict) -> bool:
    """Does this validator's own snapshot support the leader's verdict?"""
    if not isinstance(leader, dict) or set(leader) != set(OBS_KEYS):
        return False
    try:
        bid, ask, mid = float(leader["best_bid"]), float(leader["best_ask"]), float(leader["mid"])
        ints = [int(leader[k]) for k in ("ts", "spread_bps", "bid_depth", "ask_depth")]
    except Exception:
        return False
    if ints != [leader[k] for k in ("ts", "spread_bps", "bid_depth", "ask_depth")]:
        return False
    # the leader's numbers have to be internally consistent
    if leader["pass"] is not _passes(leader, terms):
        return False
    if bid > 0 and ask > bid:
        if abs(mid - (bid + ask) / 2) > mid * 1e-6:
            return False
        if leader["spread_bps"] != math.ceil((ask - bid) / mid * 10_000 - 1e-9):
            return False
    # same moment, same price level
    if abs(leader["ts"] - mine["ts"]) > MAX_SKEW_MS:
        return False
    my_mid = float(mine["mid"])
    if my_mid > 0 and mid > 0 and abs(mid - my_mid) / my_mid * 10_000 > MID_TOL_BPS:
        return False
    if (my_mid > 0) != (mid > 0):
        return False
    # books move between fetches: only a clear contradiction overturns the leader
    if leader["pass"] and _clearly_fails(mine, terms):
        return False
    if not leader["pass"] and _clearly_passes(mine, terms):
        return False
    return True


def _errors_agree(leader_res, leader_fn) -> bool:
    leader_msg = getattr(leader_res, "message", "")
    try:
        leader_fn()
        return False
    except gl.vm.UserError as e:
        mine = getattr(e, "message", str(e))
        if mine.startswith(ERR_EXPECTED) or mine.startswith(ERR_EXTERNAL):
            return mine == leader_msg
        return mine.startswith(ERR_TRANSIENT) and leader_msg.startswith(ERR_TRANSIENT)
    except Exception:
        return False


class MarketMakerSLA(gl.Contract):
    agreements: TreeMap[u256, Agreement]
    agreement_count: u256
    slots: TreeMap[str, str]         # "<agreement>:<slot>" -> JSON {status, checks, obs, by}

    def __init__(self):
        self.agreement_count = u256(0)

    # ---------- project ----------

    @gl.public.write.payable
    def create_agreement(self, mm: str, venue: str, market: str, terms_json: str,
                         start: int, interval_s: int, n_slots: int) -> int:
        retainer = int(gl.message.value)
        if retainer == 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} send the retainer with the call")
        if venue not in BOOK_URL:
            raise gl.vm.UserError(f"{ERR_EXPECTED} venue must be one of: " + ", ".join(sorted(BOOK_URL)))
        if not MARKET_RE.match(market):
            raise gl.vm.UserError(f"{ERR_EXPECTED} market looks like KRW-XRP")
        mm_addr = Address(mm)
        if mm_addr == gl.message.sender_address:
            raise gl.vm.UserError(f"{ERR_EXPECTED} project and market maker must differ")
        now = _now()
        if not (now - MIN_INTERVAL_S <= start <= now + MAX_START_DELAY_S):
            raise gl.vm.UserError(f"{ERR_EXPECTED} start must be now or within 30 days")
        if not (MIN_INTERVAL_S <= interval_s <= MAX_INTERVAL_S):
            raise gl.vm.UserError(f"{ERR_EXPECTED} interval_s must be {MIN_INTERVAL_S}-{MAX_INTERVAL_S}")
        if not (1 <= n_slots <= MAX_SLOTS):
            raise gl.vm.UserError(f"{ERR_EXPECTED} n_slots must be 1-{MAX_SLOTS}")
        terms = _parse_terms(terms_json)
        aid = int(self.agreement_count)
        self.agreements[u256(aid)] = Agreement(
            project=gl.message.sender_address, mm=mm_addr, venue=venue, market=market,
            terms=json.dumps(terms, sort_keys=True), retainer=u256(retainer), start=u256(start),
            interval_s=u256(interval_s), n_slots=u256(n_slots), passed=u256(0), failed=u256(0),
            settled=False, paid_mm=u256(0),
        )
        self.agreement_count = u256(aid + 1)
        return aid

    # ---------- anyone ----------

    @gl.public.write
    def check(self, agreement_id: int) -> dict:
        a = self._agreement(agreement_id)
        now, start, interval = _now(), int(a.start), int(a.interval_s)
        if not (start <= now < start + interval * int(a.n_slots)):
            raise gl.vm.UserError(f"{ERR_EXPECTED} outside the agreement term")
        slot = (now - start) // interval
        key = f"{agreement_id}:{slot}"
        state = json.loads(self.slots.get(key, '{"status": "unchecked", "checks": 0}'))
        if state["status"] == "fail":
            raise gl.vm.UserError(f"{ERR_EXPECTED} slot {slot} has already failed")
        if state["checks"] >= MAX_CHECKS_PER_SLOT:
            raise gl.vm.UserError(f"{ERR_EXPECTED} slot {slot} already has {MAX_CHECKS_PER_SLOT} checks")
        venue, market, terms = a.venue, a.market, json.loads(a.terms)

        def leader_fn() -> dict:
            return _observe(venue, market, terms)

        def validator_fn(leader_res) -> bool:
            if not isinstance(leader_res, gl.vm.Return):
                return _errors_agree(leader_res, leader_fn)
            return _leader_ok(leader_res.calldata, _observe(venue, market, terms), terms)

        obs = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        prev = state["status"]
        state["checks"] += 1
        state["obs"] = obs
        state["by"] = gl.message.sender_address.as_hex
        if obs["pass"]:
            if prev == "unchecked":
                state["status"] = "pass"
                a.passed = u256(int(a.passed) + 1)
        else:
            state["status"] = "fail"
            if prev == "pass":
                a.passed = u256(int(a.passed) - 1)
            a.failed = u256(int(a.failed) + 1)
        self.slots[key] = json.dumps(state, sort_keys=True)
        return dict(obs, slot=slot, slot_status=state["status"], checks=state["checks"])

    @gl.public.write
    def settle(self, agreement_id: int) -> dict:
        a = self._agreement(agreement_id)
        end = int(a.start) + int(a.interval_s) * int(a.n_slots)
        if _now() < end:
            raise gl.vm.UserError(f"{ERR_EXPECTED} agreement term has not ended")
        if a.settled:
            raise gl.vm.UserError(f"{ERR_EXPECTED} already settled")
        terms, n, passed, retainer = json.loads(a.terms), int(a.n_slots), int(a.passed), int(a.retainer)
        met_minimum = passed * 100 >= terms["min_uptime_pct"] * n
        to_mm = retainer * passed // n if met_minimum else 0
        a.settled = True
        a.paid_mm = u256(to_mm)
        if to_mm:
            _Recipient(a.mm).emit_transfer(value=u256(to_mm))
        if retainer - to_mm:
            _Recipient(a.project).emit_transfer(value=u256(retainer - to_mm))
        return {"passed": passed, "slots": n, "met_minimum": met_minimum, "to_mm": to_mm,
                "to_project": retainer - to_mm}

    # ---------- views ----------

    @gl.public.view
    def get_agreement(self, agreement_id: int) -> dict:
        a = self._agreement(agreement_id)
        return {"project": a.project.as_hex, "mm": a.mm.as_hex, "venue": a.venue, "market": a.market,
                "terms": json.loads(a.terms), "retainer": int(a.retainer), "start": int(a.start),
                "interval_s": int(a.interval_s), "n_slots": int(a.n_slots), "passed": int(a.passed),
                "failed": int(a.failed), "settled": a.settled, "paid_mm": int(a.paid_mm)}

    @gl.public.view
    def get_slot(self, agreement_id: int, slot: int) -> dict:
        self._agreement(agreement_id)
        return json.loads(self.slots.get(f"{agreement_id}:{slot}", '{"status": "unchecked", "checks": 0}'))

    @gl.public.view
    def current_slot(self, agreement_id: int) -> int:
        """-1 before the term starts, n_slots after it ends."""
        a = self._agreement(agreement_id)
        now, start = _now(), int(a.start)
        if now < start:
            return -1
        return min((now - start) // int(a.interval_s), int(a.n_slots))

    @gl.public.view
    def get_count(self) -> int:
        return int(self.agreement_count)

    def _agreement(self, aid: int) -> Agreement:
        if not (0 <= aid < int(self.agreement_count)):
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown agreement")
        return self.agreements[u256(aid)]
