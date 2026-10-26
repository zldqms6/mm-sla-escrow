// Live run on Studionet against the real Upbit and Bithumb order books.
//   node scripts/live_demo.mjs <contractAddress> [resume]
// Funds four short agreements (3 slots x 10 minutes each), checks every slot while the
// term runs, then settles. The "project" and "market maker" are demo keys; no real MM
// is involved, the books are whatever the exchanges show at that moment.
// Results go to demo_result.json; "resume" continues a run from it after an RPC failure.
import { createClient, createAccount, generatePrivateKey } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";
import fs from "node:fs";

const [address, mode] = process.argv.slice(2);
const GEN = 10n ** 18n;
const OUT = "demo_result.json";
const SLOT_S = 600;
const N_SLOTS = 3;

function key(name) {
  const env = fs.existsSync(".env") ? fs.readFileSync(".env", "utf8") : "";
  const m = env.match(new RegExp(`^${name}=(0x[0-9a-f]+)`, "m"));
  if (m) return m[1];
  const pk = generatePrivateKey();
  fs.appendFileSync(".env", `${name}=${pk}\n`);
  return pk;
}
async function fund(addr) {
  await fetch(studionet.rpcUrls.default.http[0], {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "sim_fundAccount", params: [addr, Number(1000n * GEN)] }),
  });
}

const project = createAccount(key("DEMO_PROJECT_PK"));
const mm = createAccount(key("DEMO_MM_PK"));
const gl = createClient({ chain: studionet });

const plain = (v) => JSON.parse(JSON.stringify(v, (_, x) => (typeof x === "bigint" ? x.toString() : x instanceof Map ? Object.fromEntries(x) : x)));
const read = async (functionName, args) =>
  plain(await gl.readContract({ address, functionName, args, transactionHashVariant: "latest-nonfinal" }));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Math.floor(Date.now() / 1000);

// A write can come back ACCEPTED while the leader errored (validators agreed on the error),
// so log the leader's execution result and the votes, not just the status.
async function write(account, functionName, args, value = 0n) {
  for (let attempt = 1; attempt <= 3; attempt++) {
    const hash = await gl.writeContract({ account, address, functionName, args, value });
    let r;
    for (let i = 0; !r; i++) {
      try {
        r = await gl.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, retries: 200, interval: 5000 });
      } catch (e) {
        // Studionet's RPC sometimes answers with an HTML error page; the transaction is still there
        if (i >= 5) throw e;
        console.log(`  rpc error while waiting for ${hash}, retrying: ${e.shortMessage ?? e.message}`);
        await sleep(15_000);
      }
    }
    const leader = r?.consensus_data?.leader_receipt?.[0];
    const exec = leader?.execution_result;
    const err = leader?.genvm_result?.error_code;
    const votes = r?.last_round?.validator_votes_name ?? null;
    const msg = typeof leader?.result?.payload === "string" ? leader.result.payload : "";
    console.log(`${functionName} -> ${hash} ${r?.status_name ?? ""} ${r?.result_name ?? ""} ${exec ?? ""} ${err ?? ""} ${votes ? votes.join(",") : ""}`);
    if (exec !== "ERROR" || err !== "LLM_RATE_LIMITED") {
      return { hash, status: r?.status_name, result: r?.result_name, exec, error: err, votes,
               rotations_left: r?.last_round?.rotations_left ?? null, message: String(msg).slice(0, 300) };
    }
    await sleep(60_000 * attempt);
  }
  throw new Error(`${functionName} kept failing`);
}

// [label, venue, market, terms, who checks]
const AGREEMENTS = [
  ["xrp-upbit", "upbit", "KRW-XRP", { max_spread_bps: 20, band_bps: 100, min_depth_krw: 300_000_000, min_uptime_pct: 75 }, "mm"],
  ["soph-upbit", "upbit", "KRW-SOPH", { max_spread_bps: 50, band_bps: 100, min_depth_krw: 3_000_000, min_uptime_pct: 75 }, "project"],
  ["soph-bithumb", "bithumb", "KRW-SOPH", { max_spread_bps: 50, band_bps: 100, min_depth_krw: 3_000_000, min_uptime_pct: 75 }, "mm"],
  ["nope-bithumb", "bithumb", "KRW-NOPE", { max_spread_bps: 50, band_bps: 100, min_depth_krw: 3_000_000, min_uptime_pct: 75 }, "mm"],
];
const RETAINER = 30n * GEN;

const resume = mode === "resume";
if (!resume) for (const a of [project, mm]) await fund(a.address);
const out = resume ? JSON.parse(fs.readFileSync(OUT, "utf8"))
  : { contract: address, network: "studionet", slot_s: SLOT_S, n_slots: N_SLOTS, agreements: [] };
const save = () => fs.writeFileSync(OUT, JSON.stringify(out, null, 2));

const start = resume ? out.start : now() + 240;
out.start = start;
for (const [label, venue, market, terms, checker] of resume ? [] : AGREEMENTS) {
  const id = await read("get_count", []);
  const tx = await write(project, "create_agreement",
    [mm.address, venue, market, JSON.stringify(terms), start, SLOT_S, N_SLOTS], RETAINER);
  out.agreements.push({ label, id: Number(id), venue, market, terms, checker, create: tx, checks: [] });
  save();
}

for (let slot = 0; slot < N_SLOTS; slot++) {
  const at = start + slot * SLOT_S + 60;
  if (now() < at) await sleep((at - now()) * 1000);
  for (const ag of out.agreements) {
    if (ag.label === "nope-bithumb" && slot > 0) continue;   // once is enough to show the error path
    if (ag.checks.some((c) => c.slot === slot)) continue;    // done before a resume
    const who = ag.checker === "mm" ? mm : project;
    const tx = await write(who, "check", [ag.id]);
    const s = await read("get_slot", [ag.id, slot]);
    console.log(`  ${ag.label} slot ${slot}: ${s.status}`, s.obs ? JSON.stringify(s.obs) : tx.message);
    ag.checks.push({ slot, by: ag.checker, at: now(), tx, slot_state: s });
    save();
  }
}

const end = start + N_SLOTS * SLOT_S;
if (now() < end + 15) await sleep((end + 15 - now()) * 1000);
for (const ag of out.agreements) {
  if (ag.settle) continue;
  ag.settle = await write(project, "settle", [ag.id]);
  ag.final = await read("get_agreement", [ag.id]);
  console.log(`  ${ag.label}: passed ${ag.final.passed}/${N_SLOTS}, paid_mm ${ag.final.paid_mm}`);
  save();
}
