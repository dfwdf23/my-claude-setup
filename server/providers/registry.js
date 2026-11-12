import { config } from "../config.js";
import { bus } from "../bus.js";
import anthropic from "./anthropic.js";
import fal from "./fal.js";
import elevenlabs from "./elevenlabs.js";
import openai from "./openai.js";
import youtube from "./youtube.js";
import instagram from "./instagram.js";
import local from "./local.js";

/**
 * Capability routing: "the best available AI for each job, wired automatically".
 *
 * routing.yaml lists candidates per capability, best first. For each call we
 * walk the list and use the first provider that has credentials. If a real
 * provider fails we move on to the next real one. We only drop to the local
 * stand-in engine when *no* real provider is connected for that capability —
 * a connected provider that errors makes the stage fail loudly instead of
 * silently shipping placeholder media.
 */

export const providers = { anthropic, fal, elevenlabs, openai, youtube, instagram, local };

const health = new Map(); // "cap|provider/model" → { ok, fail, lastError, lastAt, avgMs, cooldownUntil }

function parseCandidate(entry) {
  const spec = typeof entry === "string" ? { use: entry } : entry || {};
  const [provider, ...rest] = String(spec.use || "").split("/");
  return { provider, model: rest.join("/"), when: spec.when || null, options: spec.options || {}, use: spec.use };
}

function matches(when, ctx) {
  if (!when) return true;
  if (when.style && !when.style.includes(ctx.styleId)) return false;
  if (when.format && !when.format.includes(ctx.formatId)) return false;
  return true;
}

/** Ordered candidates for a capability in this context (style overrides first). */
export function candidatesFor(capability, ctx = {}) {
  const styleOverride = ctx.styleId ? config.styles.get(ctx.styleId)?.routing?.[capability] : null;
  const list = [...(styleOverride || []), ...(config.routing.capabilities[capability] || [])];
  const seen = new Set();
  return list
    .map(parseCandidate)
    .filter((c) => c.provider && providers[c.provider] && matches(c.when, ctx))
    .filter((c) => (seen.has(c.use) ? false : seen.add(c.use)));
}

const simulationOnly = () => config.studio.mode === "simulation";

function usable(candidate, capability) {
  const p = providers[candidate.provider];
  if (!p?.capabilities?.[capability]) return false;
  if (candidate.provider !== "local" && simulationOnly()) return false;
  return p.available(candidate);
}

/** Which candidate would be used right now — shown on the AI 팀 page. */
export function resolve(capability, ctx = {}) {
  const list = candidatesFor(capability, ctx);
  const pick = list.find((c) => usable(c, capability));
  return pick ? { ...pick, isLocal: pick.provider === "local" } : null;
}

export function isPlaceholder(capability, ctx = {}) {
  const r = resolve(capability, ctx);
  return !r || (r.isLocal && providers.local.placeholder?.[capability] === true);
}

function note(capability, c, ok, ms, error) {
  const key = `${capability}|${c.use}`;
  const h = health.get(key) || { ok: 0, fail: 0, avgMs: 0, lastError: null, lastAt: 0, cooldownUntil: 0 };
  if (ok) {
    h.avgMs = h.ok ? Math.round((h.avgMs * h.ok + ms) / (h.ok + 1)) : ms;
    h.ok += 1;
    h.lastError = null;
    h.cooldownUntil = 0;
  } else {
    h.fail += 1;
    h.lastError = String(error?.message || error).slice(0, 240);
    // A rejected request (bad model id, unsupported option) will not fix itself in a minute; a hiccup might.
    const rejected = error?.status >= 400 && error?.status < 500 && error?.status !== 429;
    h.cooldownUntil = Date.now() + (rejected ? 30 * 60_000 : 60_000);
  }
  h.lastAt = Date.now();
  health.set(key, h);
}

/**
 * Run a capability. `args` is the capability's own contract (see local.js for
 * the reference implementation of each one).
 */
export async function call(capability, args = {}, ctx = {}) {
  const list = candidatesFor(capability, ctx).filter((c) => usable(c, capability));
  if (!list.length) throw new Error(`'${capability}' 분야에 사용할 수 있는 AI가 없습니다 — studio/routing.yaml 을 확인하세요`);

  const real = list.filter((c) => c.provider !== "local");
  const order = real.length ? real : list;
  const now = Date.now();
  // Providers that just failed go to the back of the line for a minute.
  order.sort((a, b) => Number((health.get(`${capability}|${a.use}`)?.cooldownUntil || 0) > now) - Number((health.get(`${capability}|${b.use}`)?.cooldownUntil || 0) > now));

  let lastError;
  for (const c of order) {
    const started = Date.now();
    try {
      const result = await providers[c.provider].capabilities[capability]({ ...args, model: c.model, options: { ...c.options, ...(args.options || {}) } }, ctx);
      note(capability, c, true, Date.now() - started);
      return { ...result, provider: c.provider, model: c.model, use: c.use, local: c.provider === "local" };
    } catch (err) {
      note(capability, c, false, 0, err);
      lastError = err;
      bus.log({ kind: "provider", level: "warn", projectId: ctx.projectId, text: `${c.use} 호출 실패 — ${String(err.message).slice(0, 160)}` });
    }
  }
  throw lastError;
}

/** Everything the dashboard needs to draw the routing table. */
export function routingStatus() {
  const out = {};
  for (const capability of Object.keys(config.routing.capabilities)) {
    const active = resolve(capability);
    out[capability] = candidatesFor(capability).map((c) => {
      const p = providers[c.provider];
      const h = health.get(`${capability}|${c.use}`);
      return {
        use: c.use,
        provider: c.provider,
        providerName: p.name,
        model: c.model,
        when: c.when,
        local: c.provider === "local",
        supported: Boolean(p.capabilities?.[capability]),
        connected: Boolean(p.capabilities?.[capability]) && p.available(c),
        active: active?.use === c.use,
        needs: p.keys || [],
        health: h ? { ok: h.ok, fail: h.fail, avgMs: h.avgMs, lastError: h.lastError, lastAt: h.lastAt } : null,
      };
    });
  }
  return out;
}

export function priceOf(use, unit) {
  const p = config.routing.pricing?.[use];
  return p && typeof p[unit] === "number" ? p[unit] : 0;
}
