/* MeshSettle demo UI behaviour.
 *
 * Two deliberate choices:
 *
 * 1. The page mints a packet first (POST /api/packets) and submits it
 *    separately (POST /api/relay). That is what makes the duplicate
 *    demonstration honest: "send again" re-sends the *same bytes*, which is the
 *    only thing that actually exercises deduplication. Creating a fresh packet
 *    each time would just be two different payments.
 *
 * 2. Rejections are shown exactly as the backend reports them, including the
 *    error code. A demo that hid a DUPLICATE_PACKET response would be hiding
 *    the property the whole project exists to prove.
 *
 * No framework, no build step. It is a few hundred lines of DOM updates.
 */

"use strict";

const SETTLE_POLL_INTERVAL_MS = 400;
const SETTLE_POLL_TIMEOUT_MS = 20000;
const METRICS_REFRESH_MS = 3000;

const el = (id) => document.getElementById(id);

const state = {
  /** The packet JSON we minted, replayed verbatim on "send again". */
  packet: null,
  idempotencyKey: null,
  attempts: 0,
  settledAmount: null,
};

// --- Small helpers ----------------------------------------------------------

function setStatus(kind, label, detail) {
  const dot = el("status-dot");
  dot.className = "dot";
  if (kind) {
    dot.classList.add(`is-${kind}`);
  }
  el("status-label").textContent = label;
  el("status-detail").textContent = detail ? `— ${detail}` : "";
}

function resetStepper() {
  for (const step of document.querySelectorAll(".step")) {
    step.classList.remove("is-done", "is-active", "is-failed");
  }
}

function markStep(name, cls) {
  const step = document.querySelector(`.step[data-step="${name}"]`);
  if (step) {
    step.classList.remove("is-done", "is-active", "is-failed");
    step.classList.add(cls);
  }
}

function formatPaise(minor) {
  // Integer minor units throughout; never parsed as a float.
  const value = Number(minor);
  if (!Number.isFinite(value)) return String(minor);
  const rupees = Math.floor(value / 100);
  const paise = String(value % 100).padStart(2, "0");
  return `${value} paise (₹${rupees}.${paise})`;
}

function addAttemptRow(outcome, kind, detail) {
  el("attempts-card").hidden = false;
  const row = document.createElement("tr");

  const index = document.createElement("td");
  index.className = "mono";
  index.textContent = String(state.attempts);

  const result = document.createElement("td");
  const wrap = document.createElement("span");
  wrap.className = "outcome";
  const dot = document.createElement("span");
  dot.className = `dot is-${kind}`;
  wrap.append(dot, document.createTextNode(outcome));
  result.append(wrap);

  const why = document.createElement("td");
  why.className = "muted";
  why.textContent = detail || "";

  row.append(index, result, why);
  el("attempts-body").append(row);
}

/** Read a JSON body defensively: upstream may return an error shape or nothing. */
async function readJson(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

// --- Backend calls ----------------------------------------------------------

async function mintPacket(payer, payee, amountMinor) {
  const response = await fetch("/api/packets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      payer_id: payer,
      payee_id: payee,
      amount_minor: amountMinor,
      currency: "INR",
    }),
  });
  const body = await readJson(response);
  if (!response.ok) {
    const detail = body && body.detail ? body.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return body;
}

async function submitToMesh(packet) {
  const response = await fetch("/api/relay", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(packet),
  });
  return { response, body: await readJson(response) };
}

async function fetchSettlement(key) {
  const response = await fetch(`/api/settlements/${encodeURIComponent(key)}`);
  if (response.status === 404) return null;
  if (!response.ok) return null;
  return readJson(response);
}

/** Poll until the settlement row appears, or give up. */
async function waitForSettlement(key) {
  const deadline = Date.now() + SETTLE_POLL_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const settlement = await fetchSettlement(key);
    if (settlement) return settlement;
    await new Promise((resolve) => setTimeout(resolve, SETTLE_POLL_INTERVAL_MS));
  }
  return null;
}

// --- Flows ------------------------------------------------------------------

async function sendNewPayment(event) {
  event.preventDefault();

  const payer = el("payer").value.trim();
  const payee = el("payee").value.trim();
  const amountMinor = Number.parseInt(el("amount").value, 10);

  if (payer === payee) {
    setStatus("rejected", "Rejected", "payer and payee must differ");
    el("journey-card").hidden = false;
    return;
  }

  // Reset for a new packet.
  state.packet = null;
  state.idempotencyKey = null;
  state.attempts = 0;
  state.settledAmount = null;
  el("attempts-body").replaceChildren();
  el("attempts-card").hidden = true;
  el("journey-card").hidden = false;
  el("replay-btn").disabled = true;
  el("send-btn").disabled = true;
  resetStepper();
  el("final-label").textContent = "Settled";
  el("fact-hops").textContent = "—";
  el("fact-amount").textContent = "—";
  setStatus("pending", "Creating", "signing and sealing the packet");

  try {
    const created = await mintPacket(payer, payee, amountMinor);
    state.packet = created.packet;
    state.idempotencyKey = created.idempotency_key;

    el("fact-packet-id").textContent = created.packet.packet_id;
    el("fact-idempotency-key").textContent = created.idempotency_key;
    markStep("created", "is-done");

    await submitAndTrack();
  } catch (error) {
    markStep("created", "is-failed");
    setStatus("rejected", "Failed", error.message);
  } finally {
    el("send-btn").disabled = false;
  }
}

async function submitAndTrack() {
  state.attempts += 1;
  const attempt = state.attempts;

  markStep("relayed", "is-active");
  setStatus("pending", "Relaying", "carrying the packet hop by hop");

  const { response, body } = await submitToMesh(state.packet);

  if (!response.ok) {
    // The mesh refused it outright (bad signature, hop limit, unreachable).
    const code = body && body.code ? body.code : `HTTP ${response.status}`;
    const detail = body && body.detail ? body.detail : "";
    markStep("relayed", "is-failed");
    el("final-label").textContent = "Rejected";
    setStatus("rejected", "Rejected by the mesh", `${code}${detail ? `: ${detail}` : ""}`);
    addAttemptRow("Refused by mesh", "rejected", `${code}${detail ? ` — ${detail}` : ""}`);
    el("replay-btn").disabled = state.packet === null;
    return;
  }

  markStep("relayed", "is-done");
  markStep("bridged", "is-done");
  el("fact-hops").textContent = body && body.hop_count != null ? body.hop_count : "—";

  markStep("final", "is-active");
  setStatus("pending", "Queued", "waiting for the settlement service");

  // Whether this attempt settles or is refused as a duplicate, the row either
  // appears (first time) or already exists (every later time). Distinguish the
  // two by whether the settled amount changed.
  const before = state.settledAmount;
  const settlement = await waitForSettlement(state.idempotencyKey);

  if (!settlement) {
    markStep("final", "is-failed");
    el("final-label").textContent = "Not settled";
    setStatus("pending", "Still pending", "no settlement recorded within 20s");
    addAttemptRow("No settlement seen", "pending", "timed out waiting");
    el("replay-btn").disabled = false;
    return;
  }

  state.settledAmount = settlement.amount_minor;
  el("fact-amount").textContent = formatPaise(settlement.amount_minor);
  el("fact-hops").textContent = settlement.hop_count;
  el("final-label").textContent = "Settled";
  markStep("final", "is-done");

  if (attempt === 1) {
    setStatus("settled", "Settled", `settlement #${settlement.id}`);
    addAttemptRow("Settled", "settled", `settlement #${settlement.id}`);
  } else {
    // The packet was accepted by the mesh again, but settlement refused it.
    // The proof is that the stored settlement is unchanged.
    const unchanged = before === settlement.amount_minor;
    setStatus(
      "settled",
      "Already settled",
      unchanged
        ? "the duplicate was refused; the settled amount did not change"
        : "the settled amount changed, which should not happen",
    );
    addAttemptRow(
      unchanged ? "Duplicate refused" : "Unexpected change",
      unchanged ? "pending" : "rejected",
      unchanged
        ? `still settlement #${settlement.id}, amount unchanged`
        : "the settled amount changed between attempts",
    );
  }

  el("replay-btn").disabled = false;
  refreshMetrics();
}

async function replayPacket() {
  if (!state.packet) return;
  el("replay-btn").disabled = true;
  try {
    await submitAndTrack();
  } finally {
    el("replay-btn").disabled = state.packet === null;
  }
}

// --- Metrics ----------------------------------------------------------------

async function refreshMetrics() {
  try {
    const response = await fetch("/api/metrics");
    if (!response.ok) return;
    const metrics = await readJson(response);
    if (!metrics) return;
    el("metric-settled").textContent = metrics.settled;
    el("metric-duplicates").textContent = metrics.duplicates;
    el("metric-invalid").textContent = metrics.invalid_signature;
    el("metric-expired").textContent = metrics.expired;
    el("metric-queue").textContent =
      metrics.queue_depth == null ? "n/a" : metrics.queue_depth;
  } catch {
    // The counters are decoration; a failed refresh should not disturb the page.
  }
}

// --- Wiring -----------------------------------------------------------------

el("payment-form").addEventListener("submit", sendNewPayment);
el("replay-btn").addEventListener("click", replayPacket);

refreshMetrics();
setInterval(refreshMetrics, METRICS_REFRESH_MS);
