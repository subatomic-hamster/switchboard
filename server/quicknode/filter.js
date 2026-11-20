// QuickNode Streams filter. Network: Arc Testnet. Dataset: Logs (payload.data is nested arrays of logs).
// Keeps only logs emitted by the Vault. Returning null skips the delivery.
function main(payload) {
  const VAULT = "0xf04a1a72848cd816a0e22d3af62fc5d4288d4524";
  const logs = payload.data.flat(Infinity).filter((l) => l && (l.address || "").toLowerCase() === VAULT);
  return logs.length ? { logs } : null;
}
