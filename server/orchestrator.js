import fs from "node:fs";
import path from "node:path";
import { config, stageDefs, getAgent, getFormat, getChannel } from "./config.js";
import { bus } from "./bus.js";
import { addProject, allProjects, getProject, touch, appendLog, newId, mediaDir, removeProject } from "./store.js";
import { call, providers } from "./providers/registry.js";
import { runAgent, brainIsSimulated } from "./agents/runtime.js";
import { contextFor, spender, budgetFor } from "./context.js";
import { STAGES } from "./stages/index.js";
import { learn, collectMetrics } from "./learning/retro.js";
import { Triage } from "./schemas.js";

/**
 * The production line. Each project walks the stage graph in pipeline.yaml:
 * a stage starts as soon as everything it needs is done, independent stages
 * run side by side, and the line stops only at the two approval gates.
 */

const running = new Set(); // "projectId:stageId"
const key = (p, s) => `${p.id}:${s}`;

const GATE_LABEL = { plan: "기획", upload: "업로드" };

// ── Projects ─────────────────────────────────────────────────────────────

export function createProject({ topic = "", format, channel, style = "auto", notes = "", targets, auto = false }) {
  const ch = getChannel(channel);
  const fmt = getFormat(format || ch.default_format || "shorts");
  const project = {
    id: newId("p"),
    title: topic.trim() || "주제 선정 중…",
    brief: {
      topic: topic.trim(),
      format: fmt.id,
      channel: ch.id,
      style: style || ch.default_style || "auto",
      notes: notes.trim(),
      targets: targets?.length ? targets : [...fmt.platforms],
      auto,
    },
    status: "active",
    styleId: null,
    stages: {},
    gates: {},
    artifacts: {},
    cost: 0,
    createdAt: Date.now(),
    updatedAt: Date.now(),
  };
  project.brief.id = project.id;
  addProject(project);
  bus.log({ kind: "project", projectId: project.id, text: `새 영상 접수 — ${fmt.name} · ${project.brief.topic || "AI가 주제 선정"}` });
  kickAll();
  return project;
}

/** Stage ids that depend, directly or transitively, on any of `ids` (including themselves). */
function withDependents(ids) {
  const out = new Set(ids);
  let grew = true;
  while (grew) {
    grew = false;
    for (const def of stageDefs()) {
      if (!out.has(def.id) && def.needs.some((n) => out.has(n))) {
        out.add(def.id);
        grew = true;
      }
    }
  }
  return out;
}

/** Send work back: the named stages get revision notes, and they plus everything downstream run again. */
function resetStages(project, targets, by) {
  const valid = targets.filter((t) => stageDefs().some((d) => d.id === t.stage && !d.gate));
  if (!valid.length) return [];
  for (const id of withDependents(valid.map((t) => t.stage))) {
    const st = (project.stages[id] ||= {});
    Object.assign(st, { status: "pending", error: null, progress: 0, label: "", attempts: 0 });
  }
  for (const t of valid) project.stages[t.stage].revision = { notes: t.notes, sceneIds: t.sceneIds || [], by, at: Date.now() };
  return valid.map((t) => t.stage);
}

function stageTitle(id) {
  return stageDefs().find((d) => d.id === id)?.title || id;
}

// ── Stage execution ──────────────────────────────────────────────────────

function stageContext(project, def, st) {
  const base = contextFor(project);
  const spend = spender(project);
  const root = mediaDir(project.id);
  let lastTouch = 0;
  const sc = {
    ...base,
    id: def.id,
    def,
    simulated: brainIsSimulated(),
    art: project.artifacts,
    revision: st.revision || null,
    set(k, v) {
      project.artifacts[k] = v;
      touch(project);
    },
    dir(name) {
      const d = path.join(root, name);
      fs.mkdirSync(d, { recursive: true });
      return d;
    },
    file: (dir, name) => path.join(dir ? sc.dir(dir) : root, name),
    rel: (abs) => path.relative(root, abs).split(path.sep).join("/"),
    abs: (rel) => (rel ? path.join(root, rel) : ""),
    routeCtx: () => ({ projectId: project.id, styleId: base.style?.id, formatId: base.format.id }),
    agentCtx: () => ({ ...base, spend }),
    log(text, level = "info") {
      bus.log({ kind: "stage", level, projectId: project.id, stageId: def.id, agentId: def.agent, text });
      appendLog(project.id, { stage: def.id, level, text });
    },
    progress(fraction, label = "") {
      st.progress = Math.max(0, Math.min(1, fraction));
      st.label = label;
      if (Date.now() - lastTouch > 400) {
        lastTouch = Date.now();
        touch(project);
      }
    },
    async agent(agentId, task, opts) {
      const r = await runAgent(agentId, task, { ...opts, ctx: sc.agentCtx() });
      if (r.simulated) st.simulated = true;
      st.model = r.use;
      return r.data;
    },
    async call(capability, args) {
      const r = await call(capability, args, sc.routeCtx());
      spend(r.costUsd || 0, r.use);
      (st.used ||= {})[capability] = r.use;
      return r;
    },
  };
  return sc;
}

async function runStage(project, def) {
  const st = project.stages[def.id];
  const k = key(project, def.id);
  running.add(k);
  Object.assign(st, { status: "running", startedAt: Date.now(), finishedAt: null, error: null, progress: 0, label: "", simulated: false, used: {} });
  st.attempts = (st.attempts || 0) + 1;
  touch(project);

  const agentName = def.agent ? getAgent(def.agent).name : "";
  const sc = stageContext(project, def, st);
  bus.send("agent", { agentId: def.agent, projectId: project.id, state: "working", stageId: def.id });
  sc.log(`${def.title} 시작${st.revision ? " (재작업)" : ""}`);

  try {
    const impl = STAGES[def.run || def.id];
    if (!impl) throw Object.assign(new Error(`'${def.id}' 단계의 구현이 없습니다 — pipeline.yaml 의 id 를 확인하세요`), { fatal: true });
    const result = await impl(sc);
    const summary = typeof result === "string" ? result : result?.summary || "";
    Object.assign(st, { status: "done", finishedAt: Date.now(), progress: 1, label: "", summary });
    if (st.revision) {
      st.lastRevision = st.revision;
      delete st.revision;
    }
    sc.log(`${def.title} 완료 — ${summary}`);

    if (result?.revise?.length) {
      const stages = resetStages(project, result.revise, "qa");
      bus.log({ kind: "revision", level: "warn", projectId: project.id, agentId: def.agent, text: `${agentName}: ${stages.map(stageTitle).join(" · ")} 재작업 지시` });
    }
  } catch (err) {
    const message = String(err?.message || err);
    if (project.status === "canceled") {
      st.status = "pending";
    } else if (err?.budget) {
      st.status = "pending";
      project.paused = { reason: "budget", message, at: Date.now() };
      bus.log({ kind: "budget", level: "attention", projectId: project.id, text: `「${project.title}」 ${message} — 예산을 늘리거나 중단을 선택하세요` });
    } else if (!err?.fatal && err?.status !== 401 && err?.status !== 403 && st.attempts < 3) {
      // Transient failures (rate limits, timeouts, a provider hiccup) get two automatic retries.
      st.status = "pending";
      st.error = message;
      st.retryAt = Date.now() + 4000 * st.attempts ** 2;
      sc.log(`${def.title} 오류 — ${message.slice(0, 200)} · ${st.attempts}/3회, 곧 다시 시도합니다`, "warn");
      setTimeout(kickAll, st.retryAt - Date.now() + 50).unref?.();
    } else {
      Object.assign(st, { status: "failed", finishedAt: Date.now(), error: message });
      sc.log(`${def.title} 실패 — ${message.slice(0, 300)}`, "error");
    }
  } finally {
    running.delete(k);
    bus.send("agent", { agentId: def.agent, projectId: project.id, state: "idle", stageId: def.id });
    refreshStatus(project);
    touch(project);
    kickAll();
  }
}

function refreshStatus(project) {
  if (project.status === "canceled") return;
  const states = stageDefs().map((d) => project.stages[d.id]?.status || "pending");
  if (project.paused) project.status = "paused";
  else if (states.includes("failed")) project.status = "failed";
  else if (states.every((s) => s === "done")) project.status = "done";
  else if (states.includes("waiting") && !states.includes("running")) project.status = "waiting";
  else project.status = "active";
}

function advance(project) {
  if (project.status === "canceled" || project.paused) return;
  const maxStages = (config.studio.concurrency?.projects ?? 2) * 4;
  for (const def of stageDefs()) {
    const st = (project.stages[def.id] ||= { status: "pending", attempts: 0 });
    if (st.status !== "pending") continue;
    if (!def.needs.every((n) => project.stages[n]?.status === "done")) continue;
    if (st.retryAt && st.retryAt > Date.now()) continue;

    if (def.gate) {
      st.status = "waiting";
      st.requestedAt = Date.now();
      const gate = (project.gates[def.gate] ||= { history: [] });
      Object.assign(gate, { status: "pending", requestedAt: Date.now() });
      bus.log({ kind: "gate", level: "attention", projectId: project.id, text: `${GATE_LABEL[def.gate] || def.title} 결재 요청 — 「${project.title}」` });
      bus.send("gate", { projectId: project.id, gate: def.gate });
      touch(project);
    } else if (!running.has(key(project, def.id)) && running.size < maxStages) {
      runStage(project, def); // not awaited: independent stages run side by side
    }
  }
  refreshStatus(project);
}

export function kickAll() {
  // Oldest first, so a project already in production finishes before a newer one grabs the slots.
  for (const project of allProjects().reverse()) {
    if (["done", "canceled"].includes(project.status)) continue;
    try {
      advance(project);
    } catch (err) {
      console.error(`[orchestrator] ${project.id}:`, err);
    }
  }
}

// ── Owner actions ────────────────────────────────────────────────────────

const conflict = (message) => Object.assign(new Error(message), { status: 409 });

/** The owner's decision at a gate: approve | revise | reject. */
export async function decideGate(projectId, gateId, { decision, comment = "", options = {} }) {
  const project = getProject(projectId);
  const def = stageDefs().find((d) => d.gate === gateId);
  const st = def && project.stages[def.id];
  if (!def || st?.status !== "waiting") throw conflict("지금은 이 결재를 처리할 수 없습니다 (이미 처리되었거나 아직 올라오지 않았습니다)");
  if (!["approve", "revise", "reject"].includes(decision)) throw Object.assign(new Error("decision 값이 올바르지 않습니다"), { status: 400 });
  comment = String(comment).trim();
  if (decision === "revise" && !comment) throw Object.assign(new Error("수정 요청에는 무엇을 바꿀지 적어 주세요"), { status: 400 });

  const gate = project.gates[gateId];
  if (gate.status !== "pending") throw conflict("이 결재는 이미 처리 중입니다");
  gate.history.push({ decision, comment, options, at: Date.now() });
  const label = GATE_LABEL[gateId] || def.title;

  if (decision === "approve") {
    Object.assign(gate, { status: "approved", options, feedback: "", decidedAt: Date.now() });
    // The owner may overrule the art style the producer proposed.
    if (gateId === "plan" && options.style && config.styles.has(options.style)) {
      project.styleId = options.style;
      if (project.artifacts.plan) project.artifacts.plan.style_id = options.style;
    }
    Object.assign(st, { status: "done", finishedAt: Date.now(), summary: comment ? `승인 — ${comment}` : "승인" });
    bus.log({ kind: "gate", projectId, text: `${label} 결재 승인 — 「${project.title}」` });
  } else if (decision === "reject") {
    Object.assign(gate, { status: "rejected", feedback: comment, decidedAt: Date.now() });
    st.status = "done";
    st.summary = "반려";
    project.status = "canceled";
    project.endedReason = `${label} 결재에서 반려${comment ? ` — ${comment}` : ""}`;
    bus.log({ kind: "gate", level: "warn", projectId, text: `${label} 결재 반려 — 「${project.title}」` });
  } else {
    // The gate stays put (but no longer shows as pending) until we know which stages redo their work;
    // resetting those stages also resets this gate, so it comes back once the rework is done.
    Object.assign(gate, { status: "revising", feedback: comment, decidedAt: Date.now() });
    touch(project);
    let targets;
    if (gateId === "plan") targets = [{ stage: "plan", notes: comment }];
    else targets = await triage(project, comment);
    const stages = resetStages(project, targets, "owner");
    if (!stages.length) {
      gate.status = "pending";
      touch(project);
      throw conflict("다시 작업할 단계를 찾지 못했습니다 — pipeline.yaml 을 확인하세요");
    }
    bus.log({ kind: "gate", level: "warn", projectId, text: `${label} 결재 수정 요청 → ${stages.map(stageTitle).join(" · ")} 재작업` });
  }
  touch(project);

  // The owner's words are the strongest teaching signal the team gets.
  if (comment) learn(project, "feedback", { feedback: { gate: gateId, decision, comment } }).catch((err) => console.error("[learn]", err.message));
  kickAll();
  return project;
}

/** Turn free-form feedback on the finished video into "which stages redo what". */
async function triage(project, comment) {
  const redoable = stageDefs().filter((d) => !d.gate && ["script", "storyboard", "voice", "sound", "package"].includes(d.id)).map((d) => d.id);
  try {
    const { data } = await runAgent("producer", "triage", {
      instruction: "결재자가 완성본에 수정 요청을 남겼습니다. 다시 작업해야 하는 단계만 고르고, 각 담당자에게 줄 구체적 지시로 바꾸세요.",
      input: { comment, stages: redoable, stage_roles: Object.fromEntries(stageDefs().filter((d) => redoable.includes(d.id)).map((d) => [d.id, d.title])) },
      schema: Triage,
      ctx: { ...contextFor(project), spend: spender(project) },
    });
    const targets = data.targets.filter((t) => redoable.includes(t.stage)).map((t) => ({ stage: t.stage, notes: `결재자 요청: ${comment}\n지시: ${t.instruction}` }));
    if (targets.length) return targets;
  } catch (err) {
    bus.log({ kind: "gate", level: "warn", projectId: project.id, text: `수정 요청 배분 실패 — 대본부터 다시 작업합니다 (${String(err.message).slice(0, 100)})` });
  }
  return [{ stage: "script", notes: `결재자 요청: ${comment}` }];
}

export function retryStage(projectId, stageId) {
  const project = getProject(projectId);
  const st = project.stages[stageId];
  if (!st || st.status !== "failed") throw conflict("실패한 단계만 다시 시도할 수 있습니다");
  Object.assign(st, { status: "pending", error: null, attempts: 0, retryAt: 0 });
  refreshStatus(project);
  touch(project);
  kickAll();
}

/** Redo one stage (and everything after it) on request, e.g. after connecting a new AI. */
export function redoStage(projectId, stageId, notes = "") {
  const project = getProject(projectId);
  if (["canceled"].includes(project.status)) throw conflict("중단된 프로젝트입니다");
  if ([...running].some((k) => k.startsWith(project.id + ":"))) throw conflict("작업 중인 단계가 끝난 뒤에 다시 시도하세요");
  const stages = resetStages(project, [{ stage: stageId, notes: notes || "결재자가 이 단계를 다시 만들도록 요청했습니다." }], "owner");
  if (!stages.length) throw Object.assign(new Error("다시 만들 수 없는 단계입니다"), { status: 400 });
  if (project.status === "done") project.status = "active";
  touch(project);
  kickAll();
}

export function resumeProject(projectId, { addBudget = 0 } = {}) {
  const project = getProject(projectId);
  if (addBudget > 0) project.budgetExtra = (project.budgetExtra || 0) + addBudget;
  if (project.paused?.reason === "budget" && (project.cost || 0) > budgetFor(project)) throw conflict("예산을 늘려야 계속할 수 있습니다");
  delete project.paused;
  refreshStatus(project);
  touch(project);
  kickAll();
}

export function cancelProject(projectId) {
  const project = getProject(projectId);
  project.status = "canceled";
  project.endedReason = "직접 중단";
  delete project.paused;
  touch(project);
}

export async function deleteProject(projectId) {
  const project = getProject(projectId);
  if ([...running].some((k) => k.startsWith(project.id + ":"))) throw conflict("작업 중인 프로젝트는 먼저 중단해 주세요");
  await removeProject(projectId);
}

// ── Background duties ────────────────────────────────────────────────────

/** After a restart, anything that was mid-flight simply starts that stage again. */
export function recover() {
  for (const project of allProjects()) {
    let changed = false;
    for (const st of Object.values(project.stages)) {
      if (st.status === "running") {
        st.status = "pending";
        st.progress = 0;
        changed = true;
      }
    }
    if (changed) touch(project);
  }
}

function sameDay(a, b) {
  const x = new Date(a);
  const y = new Date(b);
  return x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
}

/** Autopilot: keep each channel's daily quota of plans flowing into the approval inbox. */
export function autopilot() {
  for (const ch of config.channels.values()) {
    if (!ch.autopilot?.enabled) continue;
    for (const [format, perDay] of Object.entries(ch.autopilot.daily || {})) {
      if (!perDay || !config.formats.has(format)) continue;
      const today = allProjects().filter((p) => p.brief.auto && p.brief.channel === ch.id && p.brief.format === format && sameDay(p.createdAt, Date.now())).length;
      if (today < perDay) createProject({ topic: "", format, channel: ch.id, style: ch.default_style || "auto", notes: ch.autopilot.topics_hint || "", auto: true });
    }
  }
}

const collecting = new Set();

/** Collect performance numbers when a checkpoint comes due. */
function metricsDue() {
  for (const project of allProjects()) {
    const an = project.artifacts.analyze;
    if (!an?.pending?.length || !project.publishedAt || collecting.has(project.id)) continue;
    // Simulated runs compress time (1 "hour" = 2 s) so the learning loop can be watched end to end.
    const unit = an.simulated ? 2000 : 3_600_000;
    const due = an.pending.find((h) => Date.now() - project.publishedAt >= h * unit);
    if (due === undefined) continue;
    collecting.add(project.id);
    collectMetrics(project, due)
      .catch((err) => console.error("[metrics]", err.message))
      .finally(() => collecting.delete(project.id));
  }
}

let timers = [];
export function startScheduler() {
  timers = [setInterval(kickAll, 5000), setInterval(metricsDue, 3000), setInterval(autopilot, 60_000)];
  timers.forEach((t) => t.unref?.());
  autopilot();
  kickAll();
}
export function stopScheduler() {
  timers.forEach(clearInterval);
}

export const runningStages = () => [...running];
export { providers };
