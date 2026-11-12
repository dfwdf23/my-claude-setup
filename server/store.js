import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { DATA_DIR, PROJECTS_DIR, PLAYBOOK_DIR, OUTBOX_DIR } from "./paths.js";
import { bus } from "./bus.js";

/**
 * File-backed persistence. Each project is a folder:
 *   data/projects/<id>/project.json   state + artifacts (JSON)
 *   data/projects/<id>/log.jsonl      append-only activity log
 *   data/projects/<id>/media/...      generated images, clips, audio, final cut
 * Plain files on purpose: easy to inspect, back up, and move between machines.
 */

const projects = new Map();
const dirty = new Set();
let flushTimer;

export function initStore() {
  for (const dir of [DATA_DIR, PROJECTS_DIR, PLAYBOOK_DIR, OUTBOX_DIR]) fs.mkdirSync(dir, { recursive: true });
  for (const id of fs.readdirSync(PROJECTS_DIR)) {
    const file = path.join(PROJECTS_DIR, id, "project.json");
    if (!fs.existsSync(file)) continue;
    try {
      projects.set(id, JSON.parse(fs.readFileSync(file, "utf8")));
    } catch (err) {
      console.error(`[store] ${file} 을(를) 읽지 못했습니다:`, err.message);
    }
  }
}

export const projectDir = (id) => path.join(PROJECTS_DIR, id);
export const mediaDir = (id, ...rest) => path.join(PROJECTS_DIR, id, "media", ...rest);

export function newId(prefix) {
  const d = new Date();
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  return `${prefix}_${stamp}_${crypto.randomBytes(3).toString("hex")}`;
}

export function getProject(id) {
  const p = projects.get(id);
  if (!p) throw Object.assign(new Error("프로젝트를 찾을 수 없습니다"), { status: 404 });
  return p;
}
export const hasProject = (id) => projects.has(id);
export const allProjects = () => [...projects.values()].sort((a, b) => b.createdAt - a.createdAt);

export function addProject(project) {
  projects.set(project.id, project);
  fs.mkdirSync(mediaDir(project.id), { recursive: true });
  touch(project);
  return project;
}

/** Mark changed, schedule a write, and tell dashboards. */
export function touch(project) {
  project.updatedAt = Date.now();
  dirty.add(project.id);
  clearTimeout(flushTimer);
  flushTimer = setTimeout(flush, 200);
  bus.send("project", { id: project.id });
}

export async function flush() {
  clearTimeout(flushTimer);
  const ids = [...dirty];
  dirty.clear();
  await Promise.all(
    ids.map(async (id) => {
      const p = projects.get(id);
      if (!p) return;
      const file = path.join(projectDir(id), "project.json");
      const tmp = file + ".tmp";
      await fsp.mkdir(path.dirname(file), { recursive: true });
      await fsp.writeFile(tmp, JSON.stringify(p, null, 1));
      await fsp.rename(tmp, file);
    }),
  );
}

export async function removeProject(id) {
  projects.delete(id);
  dirty.delete(id);
  await fsp.rm(projectDir(id), { recursive: true, force: true });
  bus.send("project", { id, removed: true });
}

export function appendLog(projectId, entry) {
  const line = JSON.stringify({ at: Date.now(), ...entry }) + "\n";
  fsp.appendFile(path.join(projectDir(projectId), "log.jsonl"), line).catch(() => {});
}

export async function readLog(projectId, limit = 400) {
  try {
    const text = await fsp.readFile(path.join(projectDir(projectId), "log.jsonl"), "utf8");
    return text
      .trim()
      .split("\n")
      .slice(-limit)
      .map((l) => JSON.parse(l));
  } catch {
    return [];
  }
}

// ── Small JSON documents (playbook, secrets, counters) ───────────────────

export function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(path.join(DATA_DIR, file), "utf8"));
  } catch {
    return fallback;
  }
}

export function writeJson(file, value, { secret = false } = {}) {
  const abs = path.join(DATA_DIR, file);
  fs.mkdirSync(path.dirname(abs), { recursive: true });
  const tmp = abs + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(value, null, 1), secret ? { mode: 0o600 } : undefined);
  fs.renameSync(tmp, abs);
}

/** Short stable hash, used to reuse media that was already generated for identical input. */
export function hashOf(...parts) {
  return crypto.createHash("sha1").update(JSON.stringify(parts)).digest("hex").slice(0, 12);
}
