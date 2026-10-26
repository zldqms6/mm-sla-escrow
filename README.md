# MarketMakerSLA

Escrow for a market maker's retainer, paid slot by slot only when the exchange's own order book met the agreed spread and depth. A token project funds the retainer and fixes the terms. Anyone can trigger a check at any time. GenLayer validators each pull the public Upbit or Bithumb order book and measure it in code. At the end, the market maker gets the share of slots it passed, and the project gets the rest back.

## Why I built this

I trade Korean stocks and crypto, and I spend a lot of time staring at Korean won order books. Every new listing on Upbit or Bithumb comes with the same quiet arrangement: the project hires a market maker, pays a monthly retainer, and the MM promises "tight spread, X won of depth on each side". Then the month ends and the arguments start.

- The project says the book was empty every night, and shows a screenshot from 3 a.m.
- The MM shows its own dashboard, built from its own logs, where uptime was 98%.
- Nobody wrote down what "depth" meant. Within 1% of mid? 2%? Measured at the best price, or at mid?
- The retainer was paid up front anyway, so the project has no leverage. Or it was paid at the end, so the MM has none.

I've watched both sides of this from the trading desk. The thing is, none of it is a matter of opinion. The terms are numbers, and the exchange publishes the order book to everyone for free. What's missing is a party both sides accept to look at the book at random times and write the result down where neither can edit it.

That's what this contract is:
- The terms are fixed in the contract when the retainer is deposited.
- Every check is an independent look at the live book by several validators, not one party's screenshot.
- The verdict per time slot is recorded on-chain, and the payout is plain arithmetic over those slots.

There's no LLM in this one. Everything in the agreement can be computed from the book, so it is. What GenLayer adds is that each validator fetches the book itself and has to agree with the leader, which turned out to need more care than I expected (see below).

## How it works

```
create_agreement(mm, venue, market, terms_json, start, interval_s, n_slots)   payable: the retainer
  venue  = "upbit" | "bithumb"     market = "KRW-XRP"
  terms  = {max_spread_bps, band_bps, min_depth_krw, min_uptime_pct}
  term   = n_slots slots of interval_s seconds from start (e.g. 720 hourly slots = 30 days)

check(agreement)                   anyone, any time inside the term
  ├─ slot = (now - start) // interval_s
  │    slot already failed → revert (one failure fails the slot for good)
  │    slot already has 3 checks → revert
  ├─ fetch https://api.<venue>.com/v1/orderbook?markets=<market>    (public, no key)
  │    404 / Bithumb's 200-with-error → [EXTERNAL] no such market, nothing recorded
  │    other non-200 → [TRANSIENT], nothing recorded, try again
  ├─ code: drop levels with size 0 (Bithumb lists them)
  │        mid = (best bid + best ask) / 2
  │        spread_bps = (ask - bid) / mid, rounded up
  │        bid depth  = Σ price × size of bids within band_bps below mid (KRW), rounded down
  │        ask depth  = Σ price × size of asks within band_bps above mid
  │        pass = spread ≤ max_spread_bps and both depths ≥ min_depth_krw
  └─ first passing check → slot "pass";  any failing check → slot "fail"

settle(agreement)                  anyone, after the term
  passed / n_slots × retainer → MM   (0 if passed / n_slots < min_uptime_pct)
  the rest → project
```

**Who checks.** A slot nobody checked counts as failed, so the MM has to prove each slot itself (a keeper bot calling `check` once per slot is enough). The project can call `check` too, whenever it thinks the book is thin. One failing check fails the slot even if it passed earlier. So the MM can't just check at good moments; it has to keep the book good the whole time, which is what the retainer is for.

### Why validators don't compare exact numbers

My first design had validators recompute the metrics and require them to match the leader's within a tolerance. Then I saved two real snapshots 20 seconds apart (they are the test fixtures):

| Upbit KRW-SOPH, ±1% of mid | 00:55:40 UTC | 20 s later |
|---|---|---|
| spread | 29 bps | 28 bps |
| bid depth | 4.4M won | 12.3M won |
| ask depth | **0.4M won** | **9.8M won** |

A thin book changes by 20x on one side in 20 seconds. Validators never fetch at the same instant, so no tolerance on raw numbers works. Validators run this instead (`_leader_ok`):

1. **The leader's numbers must add up.** Mid, spread and the pass flag are recomputed from the leader's own best bid/ask and depths. A leader saying "pass" over numbers that fail is rejected.
2. **Same moment.** The book timestamp from the exchange must be within 3 minutes of the validator's own. This stops a leader from replaying an old good book.
3. **Same price level.** The leader's mid must be within 3% of the validator's. This stops a leader from passing off another market's book.
4. **Only a clear contradiction overturns the verdict.** If the leader says pass and the validator's own book *clearly* fails (spread at least 2× the limit, or one side at most half the minimum depth), it disagrees. If the leader says fail and the validator's book *clearly* passes (spread at most half the limit, both sides at least 2× the minimum), it disagrees. Anywhere in between, the book could plausibly have looked like the leader's a few seconds earlier, so it goes along.

The result is that a borderline book gets the leader's verdict, and a book that is far on one side of the line gets the right verdict no matter who leads. The tests show the uncomfortable case too: with the two real snapshots above, a leader who saw the good one and a validator who saw the bad one *should* disagree (0.4M against a 3M minimum is a clear fail), and do.

## Live run on Studionet (2026-10-26)

Contract [`0xC148A44089a37209585e9Ef57c2cf3585133F680`](https://explorer-studio.genlayer.com/address/0xC148A44089a37209585e9Ef57c2cf3585133F680). `scripts/live_demo.mjs` funded four agreements of 30 GEN each, three 10-minute slots each (01:51-02:21 UTC), and checked every slot once against the live books. All numbers below are what the leader recorded; every transaction is `ACCEPTED`, leader `SUCCESS` unless noted. IDLE means that validator didn't vote in that round.

| Agreement | Terms | Slot 0 | Slot 1 | Slot 2 | Settled |
|---|---|---|---|---|---|
| Upbit KRW-XRP, MM checks | ≤20 bps, ≥300M won each side within 1% | pass: 6 bps, bid 2.78B / ask 1.16B | pass: 12 bps, 2.03B / 1.31B | pass: 6 bps, 1.96B / 1.44B | 3/3 → **30 GEN to MM** |
| Upbit KRW-SOPH, project checks | ≤50 bps, ≥3M won each side within 1% | fail: 30 bps, **bid 2.45M** / ask 25.1M | pass: 31 bps, 6.18M / 3.52M, votes **3 AGREE / 2 DISAGREE** | fail: **91 bps**, 5.61M / 8.16M | 1/3 < 75% → **0 to MM**, 30 back |
| Bithumb KRW-SOPH, MM checks | same as above | fail: 24 bps, bid 92K / **ask 957 won** | fail: **153 bps**, 1.14M / 0.66M | fail: 4 bps, 1.72M / 1.20M | 0/3 → 0 to MM, 30 back |
| Bithumb KRW-NOPE (no such market) | same | leader `ERROR` `[EXTERNAL] bithumb has no market KRW-NOPE`, validators agree, nothing recorded | - | - | 0/3 → 30 back |

What I take from it:
- **The deep book is boring, as it should be.** XRP passed every slot by a wide margin, and validators agreed every time.
- **The thin book split validators once.** SOPH slot 1 on Upbit passed with 3.52M won on the ask side against a 3M minimum. Two validators fetched a book that was clearly failing and voted DISAGREE; the majority went along. This is the case the "only a clear contradiction" rule is for, and it shows the rule doesn't hide disagreement: when books really differ, validators say so.
- **Bithumb SOPH is not market-made in any useful sense.** Even in slot 2, with a 4 bps spread, there was barely 1.2M won on the ask side within 1%.

**The first deployment had a bug the live run found.** On the first contract (`0xEd6C789B0526dF27Cb68914762701DA770810625`, same demo, `demo_v1_result.json`), Bithumb SOPH slot 0 came back with best bid 2.526 and bid depth 0. Polling Bithumb showed why: it sometimes lists price levels with size 0, including at the top of the book. The contract treated an empty level as the best bid, so the spread looked tighter than it was. The fix skips zero-size levels. `test_zero_size_levels_do_not_set_the_spread` uses a real Bithumb book where the top bid (2.575) has size 0: the old code reads 20 bps, the fixed code 74 bps. The run above is on the redeployed contract. The Studionet RPC also returned an HTML error page during slot 2, which crashed the script once; `live_demo.mjs resume` picked the run up from `demo_result.json`.

## API

| Method | Who | What |
|---|---|---|
| `create_agreement(mm, venue, market, terms_json, start, interval_s, n_slots)` payable | project | locks the retainer, returns the agreement id |
| `check(agreement_id)` | anyone | measures the live book, records the current slot, returns the observation |
| `settle(agreement_id)` | anyone, after the term | pays the MM pro rata, refunds the rest |
| `get_agreement(agreement_id)` | view | terms, retainer, passed / failed slot counts, settled, paid_mm |
| `get_slot(agreement_id, slot)` | view | `{status, checks, obs, by}`; `obs` is the last observation |
| `current_slot(agreement_id)` | view | -1 before the term, `n_slots` after it |
| `get_count()` | view | number of agreements |

`terms_json`:

```json
{"max_spread_bps": 50, "band_bps": 100, "min_depth_krw": 3000000, "min_uptime_pct": 75}
```

- `max_spread_bps` 1-2000, `band_bps` 10-1000, `min_uptime_pct` 0-100.
- `interval_s` 600-86400, `n_slots` up to 744 (31 days hourly), `start` from now up to 30 days ahead.

An observation (`check`'s return value, and `obs` in a slot):

```json
{"ts": 1792976140343, "mid": "1728.5", "best_bid": "1728.0", "best_ask": "1729.0", "spread_bps": 6,
 "bid_depth": 2106709374, "ask_depth": 1010592293, "bid_truncated": false, "ask_truncated": false, "pass": true}
```

Deploy:

```bash
genlayer network set studionet
genlayer deploy --contract contracts/mm_sla_escrow.py
```

Live demo (writes `demo_result.json`):

```bash
npm install
node scripts/live_demo.mjs <contract address>
```

## Tests

```bash
py -3.12 -m venv .venv && .venv/Scripts/pip install genlayer-test
.venv/Scripts/python -X utf8 -m pytest tests -q
```

14 direct-mode tests run against **real order books** saved in `tests/fixtures/` (Upbit KRW-XRP and KRW-SOPH, Bithumb KRW-SOPH, each fetched twice 20 seconds apart on 2026-10-26, plus one Bithumb book with a zero-size top level). They cover:
- **Measuring:** deep XRP book passes (6 bps, billions of won each side). Thin SOPH ask side fails. Bithumb's 146 bps spread fails. A zero-size top bid on Bithumb is skipped (74 bps, not 20); this test fails on the first deployed version.
- **Slots:** a failure can't be undone by a later passing check. A later failing check overturns a pass. 3 checks per slot max. Unchecked slots count as failed at settlement.
- **Validators:** a validator 20 s later agrees on the deep book. On the thin book, it goes along with a borderline difference and disagrees on a clear one. A leader that claims pass over failing numbers, inflates depth, misstates the spread, sends a 10-minute-old book, or passes off the XRP book as SOPH is rejected.
- **Exchange errors:** Bithumb's 200-with-error and Upbit's 404 for an unknown market are `[EXTERNAL]`, a 429 is `[TRANSIENT]`, and none of them record anything.
- **Edges:** a book with no bids, and a 2% band where all 30 visible levels are inside the band (`*_truncated: true`).
- **Settlement:** pro rata (3 of 4 → 30 of 40 GEN) and below the minimum uptime (2 of 4 at 75% → 0, all back to the project).

Removing the "clear contradiction" rule or the timestamp check makes the validator tests fail; I checked both.

On Windows, `tests/conftest.py` patches a temp-file unlink in gltest's loader.

## Limits

- **Snapshots, not a time average.** Each check is one look at the book. Real MM contracts often measure time-weighted spread and depth. More checks per slot from both sides get closer to that, but this is sampling.
- **Borderline books follow the leader.** Inside the "not clearly wrong" zone, the leader's own fetch decides. A biased leader can tilt borderline slots, but can't pass a clearly thin book or fail a clearly healthy one.
- **Thin books can split validators.** When the book swings across both "clear" lines within seconds (the SOPH case above), validators can disagree and the check goes to rotation or ends undetermined. Nothing is recorded then, and the slot can be checked again.
- **30 visible levels.** Upbit and Bithumb return 30 levels. If they all sit inside the band, the depth is a lower bound (`*_truncated: true`), which only ever counts against the MM. Pick a band the book covers, or a lower `min_depth_krw`.
- **The book shows orders, not who placed them.** The contract measures whether the market was liquid, not whether this MM provided it. That's usually what the project is paying for anyway, but an MM can't be blamed or credited for other participants' orders.
- **Won markets on two exchanges.** Adding a venue with the same response format is one line in `BOOK_URL`; other formats need a parser.
- **Spoofing isn't detected.** Orders that are placed and pulled around checks still count when they are there. Frequent unannounced checks from the project are the defence.
