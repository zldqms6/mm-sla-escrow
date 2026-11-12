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
4. **Same verdict.** The validator computes pass or fail from its own fetch of the book, and votes for the leader only if it got the same answer. The last line of `_leader_ok` is `return leader["pass"] is mine["pass"]`.

So the numbers may differ between validators (they have to, see the table), but the verdict may not. A check is recorded only when a majority of validators reproduced the leader's pass or fail from their own books. If the book sits so close to a limit that too few of them land on the leader's side, the check doesn't reach consensus, nothing is recorded, and the slot can be checked again. With the two real snapshots above, a leader who saw the good book and a validator who saw the bad one disagree.

### What the first version got wrong (fixed 2026-11-11)

The version I first submitted (`0xC148…F680`) had a softer rule 4: the validator went along with the leader unless its own book *clearly* contradicted it (for a claimed pass: spread at least 2× the limit, or one side at most half the minimum depth; the mirror image for a claimed fail). My reasoning was that a book between those lines could plausibly have looked like the leader's a few seconds earlier.

The portal review pointed out what that allows: around the threshold a validator would approve a pass and would equally approve a fail, so the leader alone picks the verdict, and the verdict changes slot state and the payout. That's right, and the margin is gone. Two tests fail on the old contract and pass on this one:
- `test_leader_cannot_pick_the_verdict_near_the_threshold`: the depth floor is set 1,000 won above the thinnest side of a real book, and a leader that nudges its numbers to claim a pass is rejected.
- `test_book_moves_between_fetches_a_split_records_nothing`: the two real snapshots above, 20 seconds apart, give different verdicts, so the validator votes against.

## Live run on Studionet, current contract (2026-11-12)

Contract [`0xa129B0e59ef2183F6857252b195b78896478128f`](https://explorer-studio.genlayer.com/address/0xa129B0e59ef2183F6857252b195b78896478128f). `deployments.json` lists it, and the `glcheck` workflow fails if the deployed source stops matching `contracts/mm_sla_escrow.py`. `scripts/live_demo.mjs` funded four agreements of 30 GEN each, three 10-minute slots each (from 02:00 UTC), and checked every slot once against the live books. Full output is in `demo_result.json`. Every transaction was `ACCEPTED`, leader `SUCCESS` unless noted. Depths are bid / ask in won.

| Agreement | Terms | Slot 0 | Slot 1 | Slot 2 | `settle()` |
|---|---|---|---|---|---|
| #0 Upbit KRW-XRP | ≤ 20 bps, ≥ 300M won each side within 1% | pass: 8 bps, 3.61B / 941.56M | pass: 8 bps, 2.89B / 1.28B | pass: 8 bps, 1.55B / 1.49B | 3/3 → **30 GEN to the MM** ([`0x7ba9…0d24`](https://explorer-studio.genlayer.com/tx/0x7ba9608c3174dae4313c077d9b1035b4400896ff03fa66c7dd053e532b970d24)) |
| #1 Upbit KRW-SOPH | ≤ 50 bps, ≥ 3M won each side within 1% | fail: 33 bps, 7.01M / 0.86M | fail: 65 bps, 3.40M / 3.16M ([`0x6e49…6d36`](https://explorer-studio.genlayer.com/tx/0x6e4993b1a4f4723bdd7f860d841f0d6b9fe70dc76de8a3e82feb9d8e3fdb6d36)) | fail: 33 bps, 1.75M / 1.71M | 0/3 → **0 to the MM**, 30 back ([`0x9a99…8dfe`](https://explorer-studio.genlayer.com/tx/0x9a992e09dbfa68f72c6cb660e928b6f82fb68c1c723f20f00c8d29586c658dfe)) |
| #2 Bithumb KRW-SOPH | same | fail: 111 bps, 1.63M / 1.64M | fail: 111 bps, 1.08M / 1.64M | fail: 5 bps, 1.19M / 1.64M | 0/3 → 0 to the MM, 30 back ([`0x4de3…cafd`](https://explorer-studio.genlayer.com/tx/0x4de3466a39e6b9b14309b435ecbfe6aa813b7a4911ceb9b6787e5af8cbc8cafd)) |
| #3 Bithumb KRW-NOPE (no such market) | same | leader `ERROR` `[EXTERNAL] bithumb has no market KRW-NOPE` ([`0x2ca5…77ae`](https://explorer-studio.genlayer.com/tx/0x2ca500763cf432a386abc5fcc3d839f768dbb1157d7071dde03b80d27f2477ae)); validators agreed on the error, nothing recorded | - | - | 0/3 → 30 back |

Every check had 3 AGREE votes and 2 IDLE validators that didn't vote in the round. There were no DISAGREE votes.

Upbit SOPH slot 1 is the kind of book the fix is about: a 65 bps spread against a 50 bps limit, with 3.40M / 3.16M won against a 3M floor. Under the old rule a validator would have gone along with a pass or a fail from the leader there, because the spread was under 2× the limit and both sides were over half the floor and under twice it. Now each validator that voted had to get fail from its own fetch.

The same source is also deployed at [`0x34D32F1F3b1181bCEb99F1A7C13F585F6841Fda9`](https://explorer-studio.genlayer.com/address/0x34D32F1F3b1181bCEb99F1A7C13F585F6841Fda9), the address the web app ([mm-sla-app](https://github.com/zldqms6/mm-sla-app)) uses; `deployments.json` lists both. A run of this script there on 2026-11-11 is in `demo_result_34d3.json`. In that run Upbit SOPH passed slot 0 with the bid side at 3.08M won, 2.7% over the floor, and failed slot 1 at 2.69M, 10% under, which settled at 2/3 = 67%, under the 75% minimum, so 0 to the MM.

A check that ends without consensus did not happen in either run. That path is covered by the tests, not by a live transaction.

Before these two addresses there was one more deployment of the fixed contract (`0x7826B076CC17E33d5a6f3F86Ab7Af0cE9492c327`). Its docstring said a check needs *every* validator to match, which overstates it: the rule is a majority. The docstring is part of the deployed source, so I corrected it and redeployed rather than leave a wrong sentence on chain.

## Live run on the first version (2026-10-26)

Kept for history. This run was under the old rule 4, so validators were applying the "clear contradiction" margin. Its output is `demo_result_c148.json`.

Contract [`0xC148A44089a37209585e9Ef57c2cf3585133F680`](https://explorer-studio.genlayer.com/address/0xC148A44089a37209585e9Ef57c2cf3585133F680). `scripts/live_demo.mjs` funded four agreements of 30 GEN each, three 10-minute slots each (01:51-02:21 UTC), and checked every slot once against the live books. All numbers below are what the leader recorded; every transaction is `ACCEPTED`, leader `SUCCESS` unless noted. IDLE means that validator didn't vote in that round.

| Agreement | Terms | Slot 0 | Slot 1 | Slot 2 | Settled |
|---|---|---|---|---|---|
| Upbit KRW-XRP, MM checks | ≤20 bps, ≥300M won each side within 1% | pass: 6 bps, bid 2.78B / ask 1.16B | pass: 12 bps, 2.03B / 1.31B | pass: 6 bps, 1.96B / 1.44B | 3/3 → **30 GEN to MM** |
| Upbit KRW-SOPH, project checks | ≤50 bps, ≥3M won each side within 1% | fail: 30 bps, **bid 2.45M** / ask 25.1M | pass: 31 bps, 6.18M / 3.52M, votes **3 AGREE / 2 DISAGREE** | fail: **91 bps**, 5.61M / 8.16M | 1/3 < 75% → **0 to MM**, 30 back |
| Bithumb KRW-SOPH, MM checks | same as above | fail: 24 bps, bid 92K / **ask 957 won** | fail: **153 bps**, 1.14M / 0.66M | fail: 4 bps, 1.72M / 1.20M | 0/3 → 0 to MM, 30 back |
| Bithumb KRW-NOPE (no such market) | same | leader `ERROR` `[EXTERNAL] bithumb has no market KRW-NOPE`, validators agree, nothing recorded | - | - | 0/3 → 30 back |

What I take from it:
- **The deep book is boring, as it should be.** XRP passed every slot by a wide margin, and validators agreed every time.
- **The thin book split validators once.** SOPH slot 1 on Upbit passed with 3.52M won on the ask side against a 3M minimum. Two validators fetched a book that was clearly failing and voted DISAGREE; the majority went along.
- **Bithumb SOPH is not market-made in any useful sense.** Even in slot 2, with a 4 bps spread, there was barely 1.2M won on the ask side within 1%.

**The first deployment had a bug the live run found.** On the first contract (`0xEd6C789B0526dF27Cb68914762701DA770810625`, same demo, `demo_v1_result.json`), Bithumb SOPH slot 0 came back with best bid 2.526 and bid depth 0. Polling Bithumb showed why: it sometimes lists price levels with size 0, including at the top of the book. The contract treated an empty level as the best bid, so the spread looked tighter than it was. The fix skips zero-size levels. `test_zero_size_levels_do_not_set_the_spread` uses a real Bithumb book where the top bid (2.575) has size 0: the old code reads 20 bps, the fixed code 74 bps. The 2026-10-26 run above is on the redeployed contract (`0xC148…F680`). The Studionet RPC also returned an HTML error page during slot 2, which crashed the script once; `live_demo.mjs resume` picked the run up from `demo_result.json`.

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
npm install
node scripts/deploy.mjs contracts/mm_sla_escrow.py     # Studionet, key in .env (created if missing)
```

Put the address in `deployments.json`; the `glcheck` workflow then compares the deployed source with the repo on every push and once a day.

Live demo (writes `demo_result.json`):

```bash
node scripts/live_demo.mjs <contract address>
```

## Tests

```bash
py -3.12 -m venv .venv && .venv/Scripts/pip install -r requirements-test.txt
.venv/Scripts/python -X utf8 -m pytest tests -q
```

The tests pin genvm v0.2.16; the first run downloads it. The `contract tests` workflow runs them on every push.

16 direct-mode tests run against **real order books** saved in `tests/fixtures/` (Upbit KRW-XRP and KRW-SOPH, Bithumb KRW-SOPH, each fetched twice 20 seconds apart on 2026-10-26, plus one Bithumb book with a zero-size top level). They cover:
- **Measuring:** deep XRP book passes (6 bps, billions of won each side). Thin SOPH ask side fails. Bithumb's 146 bps spread fails. A zero-size top bid on Bithumb is skipped (74 bps, not 20); this test fails on the first deployed version.
- **Slots:** a failure can't be undone by a later passing check. A later failing check overturns a pass. 3 checks per slot max. Unchecked slots count as failed at settlement.
- **Validators:** a validator 20 s later agrees on the deep book. A validator whose numbers differ from the leader's but give the same verdict agrees. One whose own book gives the other verdict votes against, including when the book is right at the threshold. A leader that claims pass over failing numbers, inflates depth, misstates the spread, sends a 10-minute-old book, or passes off the XRP book as SOPH is rejected.
- **Exchange errors:** Bithumb's 200-with-error and Upbit's 404 for an unknown market are `[EXTERNAL]`, a 429 is `[TRANSIENT]`, and none of them record anything.
- **Edges:** a book with no bids, and a 2% band where all 30 visible levels are inside the band (`*_truncated: true`).
- **Settlement:** pro rata (3 of 4 → 30 of 40 GEN) and below the minimum uptime (2 of 4 at 75% → 0, all back to the project).

The two near-threshold tests fail on the first version's contract; I checked by running them against that source.

On Windows, `tests/conftest.py` patches a temp-file unlink in gltest's loader.

## Limits

- **Snapshots, not a time average.** Each check is one look at the book. Real MM contracts often measure time-weighted spread and depth. More checks per slot from both sides get closer to that, but this is sampling.
- **A book at the limit can end with nothing recorded.** When depth or spread sits right at a threshold, validators' own fetches land on both sides and a check may not reach consensus. The slot stays as it was and can be checked again. A slot that never gets a recorded check counts as failed at settlement, so this goes against the MM, not for it.
- **30 visible levels.** Upbit and Bithumb return 30 levels. If they all sit inside the band, the depth is a lower bound (`*_truncated: true`), which only ever counts against the MM. Pick a band the book covers, or a lower `min_depth_krw`.
- **The book shows orders, not who placed them.** The contract measures whether the market was liquid, not whether this MM provided it. That's usually what the project is paying for anyway, but an MM can't be blamed or credited for other participants' orders.
- **Won markets on two exchanges.** Adding a venue with the same response format is one line in `BOOK_URL`; other formats need a parser.
- **Spoofing isn't detected.** Orders that are placed and pulled around checks still count when they are there. Frequent unannounced checks from the project are the defence.
