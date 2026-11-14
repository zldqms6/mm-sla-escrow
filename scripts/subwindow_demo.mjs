// Live check of the sub-window rule on Studionet (real Upbit KRW-XRP book).
//   node scripts/subwindow_demo.mjs <contractAddress>
// One agreement, one 10-minute slot = three sub-windows of 200 s. The MM checks right
// after the slot opens, then tries again 30 s later in the same sub-window (should revert
// with [EXPECTED] ... sub-window 0 already has a check), then the project checks in
// sub-window 1 and sub-window 2 (both recorded). Then settle.
// Results go to subwindow_result.json.
import { createClient, createAccount, generatePrivateKey } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";
import fs from "node:fs";

const [address, mode] = process.argv.slice(2);
const GEN = 10n ** 18n;
const OUT = "subwindow_result.json";
const SLOT_S = 600;
const N_SLOTS = 1;

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

const TERMS = { max_spread_bps: 20, band_bps: 100, min_depth_krw: 300_000_000, min_uptime_pct: 75 };
const RETAINER = 5n * GEN;
for (const a of [project, mm]) await fund(a.address);
const out = { contract: address, network: "studionet", slot_s: SLOT_S, sub_windows: 3, venue: "upbit", market: "KRW-XRP", terms: TERMS, steps: [] };
const save = () => fs.writeFileSync(OUT, JSON.stringify(out, null, 2));

const start = now() + 120;
out.start = start;
out.id = Number(await read("get_count", []));
out.create = await write(project, "create_agreement", [mm.address, "upbit", "KRW-XRP", JSON.stringify(TERMS), start, SLOT_S, N_SLOTS], RETAINER);
save();

// [label, who, seconds after slot start]
const PLAN = [["mm, sub-window 0", mm, 15], ["mm again, sub-window 0", mm, 45],
              ["project, sub-window 1", project, 215], ["project, sub-window 2", project, 415]];
for (const [label, who, offset] of PLAN) {
  const at = start + offset;
  if (now() < at) await sleep((at - now()) * 1000);
  const sent_at = now();
  const tx = await write(who, "check", [out.id]);
  const s = await read("get_slot", [out.id, 0]);
  console.log(`  ${label} (sent at +${sent_at - start}s): ${tx.exec} ${tx.message || ""} -> slot ${s.status}, windows ${JSON.stringify(s.windows ?? [])}`);
  out.steps.push({ label, sent_offset_s: sent_at - start, tx, slot_state: s });
  save();
}

const end = start + SLOT_S;
if (now() < end + 15) await sleep((end + 15 - now()) * 1000);
out.settle = await write(project, "settle", [out.id]);
out.final = await read("get_agreement", [out.id]);
console.log(`  settled: passed ${out.final.passed}/1, paid_mm ${out.final.paid_mm}`);
save();
