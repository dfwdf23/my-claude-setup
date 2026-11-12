import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import chokidar from "chokidar";
import YAML from "yaml";
import { STUDIO_DIR, safeJoin } from "./paths.js";
import { bus } from "./bus.js";

/**
 * Hot-reloadable studio configuration.
 *
 * Everything under studio/ is plain YAML / Markdown. It is re-read the moment a
 * file changes; a file that fails to parse keeps its last good version in
 * memory, so a typo never takes a running studio down.
 */
export const config = {
  studio: {},
  pipeline: { phases: [], stages: [] },
  routing: { capabilities: {}, pricing: {} },
  rubric: { criteria: [] },
  seedLessons: [],
  agents: new Map(),
  styles: new Map(),
  formats: new Map(),
  channels: new Map(),
  errors: [], // [{ file, message }]
  version: 0,
  loadedAt: 0,
};

const readText = (rel) => fs.readFileSync(path.join(STUDIO_DIR, rel), "utf8");

function parseFrontmatter(text, file) {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/.exec(text);
  if (!m) throw new Error(`${file}: 맨 위에 --- 로 감싼 설정 블록이 필요합니다`);
  return { meta: YAML.parse(m[1]) || {}, body: m[2].trim() };
}

function listFiles(dir, ext) {
  const abs = path.join(STUDIO_DIR, dir);
  if (!fs.existsSync(abs)) return [];
  return fs
    .readdirSync(abs)
    .filter((f) => f.endsWith(ext) && !f.startsWith("."))
    .sort()
    .map((f) => path.posix.join(dir, f));
}

function validatePipeline(p) {
  if (!Array.isArray(p?.stages) || !p.stages.length) throw new Error("stages 목록이 비어 있습니다");
  const ids = new Set();
  for (const s of p.stages) {
    if (!s.id) throw new Error("id가 없는 단계가 있습니다");
    if (ids.has(s.id)) throw new Error(`단계 id가 중복됩니다: ${s.id}`);
    ids.add(s.id);
    s.needs = s.needs || [];
  }
  for (const s of p.stages) {
    for (const n of s.needs) if (!ids.has(n)) throw new Error(`${s.id}.needs 에 없는 단계가 있습니다: ${n}`);
  }
  // Reject cycles: every stage must become runnable eventually.
  const done = new Set();
  let progressed = true;
  while (progressed) {
    progressed = false;
    for (const s of p.stages) {
      if (!done.has(s.id) && s.needs.every((n) => done.has(n))) {
        done.add(s.id);
        progressed = true;
      }
    }
  }
  if (done.size !== p.stages.length) throw new Error("단계의 needs가 서로 물려 있어 끝나지 않습니다 (순환)");
  return p;
}

/** Load one unit; on failure keep the previous value and record the error. */
function attempt(errors, file, fn) {
  try {
    fn();
  } catch (err) {
    errors.push({ file, message: String(err.message || err) });
  }
}

export function loadAll() {
  const errors = [];

  attempt(errors, "studio.yaml", () => {
    config.studio = YAML.parse(readText("studio.yaml")) || {};
  });
  attempt(errors, "pipeline.yaml", () => {
    config.pipeline = validatePipeline(YAML.parse(readText("pipeline.yaml")));
  });
  attempt(errors, "routing.yaml", () => {
    const r = YAML.parse(readText("routing.yaml")) || {};
    if (!r.capabilities || typeof r.capabilities !== "object") throw new Error("capabilities 가 없습니다");
    config.routing = { capabilities: r.capabilities, pricing: r.pricing || {} };
  });
  attempt(errors, "rubric.yaml", () => {
    const r = YAML.parse(readText("rubric.yaml")) || {};
    if (!Array.isArray(r.criteria) || !r.criteria.length) throw new Error("criteria 가 비어 있습니다");
    config.rubric = r;
  });
  attempt(errors, "playbook-seed.yaml", () => {
    config.seedLessons = (YAML.parse(readText("playbook-seed.yaml")) || {}).lessons || [];
  });

  const loadCollection = (dir, ext, target, parse) => {
    const next = new Map();
    for (const file of listFiles(dir, ext)) {
      const fallbackId = path.basename(file, ext);
      try {
        const item = parse(readText(file), file);
        item.id = item.id || fallbackId;
        item.file = file;
        next.set(item.id, item);
      } catch (err) {
        errors.push({ file, message: String(err.message || err) });
        // keep the last good copy of this one file
        const prev = [...target.values()].find((v) => v.file === file);
        if (prev) next.set(prev.id, prev);
      }
    }
    target.clear();
    for (const [k, v] of next) target.set(k, v);
  };

  loadCollection("agents", ".md", config.agents, (text, file) => {
    const { meta, body } = parseFrontmatter(text, file);
    return { uses: [], ...meta, prompt: body };
  });
  loadCollection("styles", ".yaml", config.styles, (text) => YAML.parse(text) || {});
  loadCollection("formats", ".yaml", config.formats, (text) => {
    const f = YAML.parse(text) || {};
    if (!f.canvas?.width || !f.canvas?.height) throw new Error("canvas.width / canvas.height 가 필요합니다");
    return f;
  });
  loadCollection("channels", ".yaml", config.channels, (text) => YAML.parse(text) || {});

  config.errors = errors;
  config.version += 1;
  config.loadedAt = Date.now();
  return config;
}

let watcher;
export function watchConfig() {
  let timer;
  const changed = new Set();
  watcher = chokidar.watch(STUDIO_DIR, { ignoreInitial: true, awaitWriteFinish: { stabilityThreshold: 120, pollInterval: 40 } });
  watcher.on("all", (_evt, file) => {
    changed.add(path.relative(STUDIO_DIR, file).split(path.sep).join("/"));
    clearTimeout(timer);
    timer = setTimeout(() => {
      const files = [...changed];
      changed.clear();
      loadAll();
      bus.log({
        kind: "config",
        level: config.errors.length ? "warn" : "info",
        text: config.errors.length
          ? `설정 오류: ${config.errors.map((e) => e.file).join(", ")} — 이전 내용으로 계속 동작합니다`
          : `설정 반영됨: ${files.join(", ")}`,
      });
      bus.send("config", { files, errors: config.errors, version: config.version });
    }, 150);
  });
  return watcher;
}

export async function stopWatching() {
  await watcher?.close();
}

// ── Editing from the dashboard ────────────────────────────────────────────

const EDITABLE = /^(studio|pipeline|routing|rubric|playbook-seed)\.yaml$|^(agents\/[\w-]+\.md|(styles|formats|channels)\/[\w-]+\.yaml)$/;

export function listConfigFiles() {
  const top = ["studio.yaml", "pipeline.yaml", "routing.yaml", "rubric.yaml", "playbook-seed.yaml"];
  return [
    ...top,
    ...listFiles("agents", ".md"),
    ...listFiles("styles", ".yaml"),
    ...listFiles("formats", ".yaml"),
    ...listFiles("channels", ".yaml"),
  ];
}

export async function readConfigFile(rel) {
  if (!EDITABLE.test(rel)) throw Object.assign(new Error("편집할 수 없는 파일입니다"), { status: 400 });
  return fsp.readFile(safeJoin(STUDIO_DIR, rel), "utf8");
}

/** Validate then write. The watcher picks the change up and reloads. */
export async function writeConfigFile(rel, content) {
  if (!EDITABLE.test(rel)) throw Object.assign(new Error("편집할 수 없는 파일입니다"), { status: 400 });
  try {
    if (rel.endsWith(".md")) parseFrontmatter(content, rel);
    else {
      const parsed = YAML.parse(content);
      if (rel === "pipeline.yaml") validatePipeline(parsed);
    }
  } catch (err) {
    throw Object.assign(new Error(`저장하지 않았습니다 — 형식 오류: ${err.message}`), { status: 422 });
  }
  const abs = safeJoin(STUDIO_DIR, rel);
  await fsp.mkdir(path.dirname(abs), { recursive: true });
  await fsp.writeFile(abs, content.endsWith("\n") ? content : content + "\n", "utf8");
}

export async function deleteConfigFile(rel) {
  if (!/^(styles|formats|channels)\/[\w-]+\.yaml$/.test(rel)) {
    throw Object.assign(new Error("이 파일은 삭제할 수 없습니다"), { status: 400 });
  }
  await fsp.rm(safeJoin(STUDIO_DIR, rel));
}

/** Change a single value in a YAML file while keeping its comments intact. */
export async function patchYaml(rel, keyPath, value) {
  const text = await readConfigFile(rel);
  const doc = YAML.parseDocument(text);
  doc.setIn(keyPath, value);
  await writeConfigFile(rel, doc.toString());
}

// ── Lookups used across the server ───────────────────────────────────────

export const stageDefs = () => config.pipeline.stages;
export const stageDef = (id) => config.pipeline.stages.find((s) => s.id === id);

export function getFormat(id) {
  const f = config.formats.get(id);
  if (!f) throw new Error(`포맷을 찾을 수 없습니다: ${id}`);
  return f;
}
export function getStyle(id) {
  return config.styles.get(id) || [...config.styles.values()][0];
}
export function getChannel(id) {
  return config.channels.get(id) || [...config.channels.values()][0];
}
export function getAgent(id) {
  const a = config.agents.get(id);
  if (!a) throw new Error(`AI 팀원 정의가 없습니다: studio/agents/${id}.md`);
  return a;
}
