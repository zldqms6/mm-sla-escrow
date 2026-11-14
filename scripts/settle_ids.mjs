// Settle agreements left over from an aborted live run (ids given on the command line).
//   node scripts/settle_ids.mjs <contractAddress> <id> [<id> ...]
// Appends the result to settle_ids_result.json.
import { createClient, createAccount, generatePrivateKey } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";
import fs from "node:fs";

const [address, ...ids] = process.argv.slice(2);
const GEN = 10n ** 18n;

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

const OUTF = "settle_ids_result.json";
const res = fs.existsSync(OUTF) ? JSON.parse(fs.readFileSync(OUTF, "utf8")) : { contract: address, settled: [] };
for (const id of ids.map(Number)) {
  const before = await read("get_agreement", [id]);
  const tx = await write(project, "settle", [id]);
  const after = await read("get_agreement", [id]);
  console.log(`  #${id}: passed ${after.passed}/${after.n_slots}, paid_mm ${after.paid_mm}, settled ${after.settled}`);
  res.settled.push({ id, before, tx, after });
  fs.writeFileSync(OUTF, JSON.stringify(res, null, 2));
}
