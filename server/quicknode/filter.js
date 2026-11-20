// QuickNode Streams filter. Network: Arc Testnet. Dataset: Logs (payload.data is nested arrays of logs).
// Keeps only logs emitted by the watched contracts (Vault, Ledger, Escrow). Edit WATCHED when the
// addresses are known. Returning null skips the delivery, except every 60th batch: that one
// returns {heartbeat: n} so the server can tell a quiet chain from a dead Stream.
const WATCHED = [
  "0xf04a1a72848cd816a0e22d3af62fc5d4288d4524", // Vault
  // "0x...", // Ledger
  // "0x...", // Escrow
];

function main(payload) {
  const watched = new Set(WATCHED.map((a) => a.toLowerCase()));
  const logs = payload.data.flat(Infinity).filter((l) => l && watched.has((l.address || "").toLowerCase()));
  if (logs.length) return { logs };
  const n = Number(payload.metadata.batch_start_range);
  return n % 60 === 0 ? { heartbeat: n } : null;
}
