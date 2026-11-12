import { config } from "../config.js";
import { readJson, writeJson, hashOf, newId } from "../store.js";
import { bus } from "../bus.js";

/**
 * The playbook is the team's memory: short, actionable lessons that are
 * injected into each AI team member's instructions on every job.
 *
 * Lessons come from four places — the built-in seed list, the owner's
 * feedback at approval gates, QA findings, and real performance data.
 * Lessons learned while the brain is simulated are kept apart and never
 * reach a real production.
 */

const FILE = "playbook/lessons.json";

function load() {
  const doc = readJson(FILE, null) || { lessons: [], retiredSeeds: [], history: [] };
  doc.lessons ||= [];
  doc.retiredSeeds ||= [];
  doc.history ||= [];
  return doc;
}

function save(doc) {
  if (doc.history.length > 500) doc.history = doc.history.slice(-500);
  writeJson(FILE, doc);
  bus.send("playbook", {});
}

function seedLessons(doc) {
  return (config.seedLessons || []).map((l) => {
    const id = `seed_${hashOf(l.text)}`;
    return {
      id,
      agent: l.agent || "",
      format: l.format || "",
      style: l.style || "",
      text: l.text,
      evidence: "기본 제작 원칙",
      confidence: 0.7,
      source: "seed",
      origin: "real",
      status: doc.retiredSeeds.includes(id) ? "retired" : "active",
      createdAt: 0,
    };
  });
}

export function allLessons() {
  const doc = load();
  return [...doc.lessons, ...seedLessons(doc)].sort((a, b) => b.createdAt - a.createdAt);
}

export const playbookHistory = () => load().history.slice(-80).reverse();

const SOURCE_RANK = { manual: 4, feedback: 3, analytics: 2, qa: 1, seed: 0 };

/** Lessons that apply to this agent on this job, strongest first. */
export function relevantLessons({ agentId, formatId, styleId, simulated }) {
  const limit = config.studio.learning?.max_lessons_in_prompt ?? 12;
  if (config.studio.learning?.enabled === false) return [];
  return allLessons()
    .filter((l) => l.status === "active")
    .filter((l) => simulated || l.origin !== "sim")
    .filter((l) => (!l.agent || l.agent === agentId) && (!l.format || l.format === formatId) && (!l.style || l.style === styleId))
    .sort((a, b) => (SOURCE_RANK[b.source] ?? 0) - (SOURCE_RANK[a.source] ?? 0) || b.confidence - a.confidence || b.createdAt - a.createdAt)
    .slice(0, limit);
}

const normalise = (t) => t.replace(/[\s.,!?'"“”‘’()\-–—·:;]/g, "").toLowerCase();

function bigrams(t) {
  const s = normalise(t);
  const out = new Set();
  for (let i = 0; i < s.length - 1; i++) out.add(s.slice(i, i + 2));
  return out;
}

function similar(a, b) {
  const x = bigrams(a);
  const y = bigrams(b);
  if (!x.size || !y.size) return normalise(a) === normalise(b);
  let both = 0;
  for (const g of x) if (y.has(g)) both++;
  return both / (x.size + y.size - both) > 0.72;
}

/** Add lessons, skipping near-duplicates of what the team already knows. Returns the ones actually added. */
export function addLessons(list, { source, origin = "real", projectId = null } = {}) {
  const doc = load();
  const known = [...doc.lessons, ...seedLessons(doc)].filter((l) => l.status === "active");
  const added = [];
  for (const l of list) {
    const text = String(l.text || "").trim();
    if (text.length < 6) continue;
    const dupe = known.find((k) => similar(k.text, text));
    if (dupe) {
      // Seeing the same lesson again is evidence for it.
      const stored = doc.lessons.find((x) => x.id === dupe.id);
      if (stored) stored.confidence = Math.min(1, stored.confidence + 0.1);
      continue;
    }
    const lesson = {
      id: newId("ls"),
      agent: config.agents.has(l.agent) ? l.agent : "",
      format: config.formats.has(l.format) ? l.format : "",
      style: config.styles.has(l.style) ? l.style : "",
      text,
      evidence: String(l.evidence || "").slice(0, 400),
      confidence: Math.min(1, Math.max(0.1, Number(l.confidence) || 0.5)),
      source,
      origin,
      status: "active",
      projectId,
      createdAt: Date.now(),
    };
    doc.lessons.push(lesson);
    known.push(lesson);
    added.push(lesson);
    doc.history.push({ at: Date.now(), action: "added", id: lesson.id, text, source });
  }
  if (added.length || list.length) save(doc);
  return added;
}

export function setLessonStatus(id, status, reason = "") {
  const doc = load();
  if (id.startsWith("seed_")) {
    doc.retiredSeeds = doc.retiredSeeds.filter((x) => x !== id);
    if (status === "retired") doc.retiredSeeds.push(id);
  } else {
    const l = doc.lessons.find((x) => x.id === id);
    if (!l) throw Object.assign(new Error("학습 노트를 찾을 수 없습니다"), { status: 404 });
    l.status = status;
  }
  doc.history.push({ at: Date.now(), action: status === "retired" ? "retired" : "restored", id, text: reason });
  save(doc);
}

export function deleteLesson(id) {
  const doc = load();
  doc.lessons = doc.lessons.filter((x) => x.id !== id);
  doc.history.push({ at: Date.now(), action: "deleted", id });
  save(doc);
}

export function playbookStats() {
  const all = allLessons();
  const active = all.filter((l) => l.status === "active");
  const bySource = {};
  for (const l of active) bySource[l.source] = (bySource[l.source] || 0) + 1;
  return { total: all.length, active: active.length, bySource, learned: active.filter((l) => l.source !== "seed").length };
}
