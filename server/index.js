import http from "node:http";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { WEB_DIR, NODE_MODULES, PROJECTS_DIR, safeJoin } from "./paths.js";
import { config, loadAll, watchConfig, listConfigFiles, readConfigFile, writeConfigFile, deleteConfigFile, patchYaml, stageDefs } from "./config.js";
import { bus } from "./bus.js";
import { initStore, allProjects, getProject, readLog, flush, readJson, writeJson } from "./store.js";
import { loadDotEnv, secretStatus, setSecret, hasSecret, KNOWN_KEYS, secret } from "./secrets.js";
import { routingStatus, resolve } from "./providers/registry.js";
import * as youtube from "./providers/youtube.js";
import instagram from "./providers/instagram.js";
import { createProject, decideGate, retryStage, redoStage, resumeProject, cancelProject, deleteProject, recover, startScheduler, runningStages, autopilot } from "./orchestrator.js";
import { allLessons, playbookHistory, playbookStats, addLessons, setLessonStatus, deleteLesson } from "./learning/playbook.js";
import { runAgent, brainIsSimulated } from "./agents/runtime.js";
import { placeholdersOf } from "./stages/post.js";
import { budgetFor } from "./context.js";
import { verifySignature } from "./public-url.js";
import { closeBrowser, FONT_FAMILIES } from "./media/render.js";
import { ScoutReport } from "./schemas.js";

loadDotEnv();

const PORT = Number(process.env.PORT || 4400);
const HOST = process.env.HOST || "127.0.0.1";
const TOKEN = process.env.STUDIO_TOKEN || "";
const LOOPBACK = ["127.0.0.1", "localhost", "::1"].includes(HOST);

// This server can spend money and publish to your channels. Never expose it without a password.
if (!LOOPBACK && !TOKEN && process.env.STUDIO_INSECURE_NO_AUTH !== "1") {
  console.error(`\n  HOST=${HOST} 로 외부에 열려면 STUDIO_TOKEN(접속 비밀번호)을 함께 설정해야 합니다.\n  예)  STUDIO_TOKEN=원하는비밀번호 HOST=0.0.0.0 npm start\n`);
  process.exit(1);
}

// ── Small HTTP helpers ───────────────────────────────────────────────────

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
  ".mp4": "video/mp4", ".mov": "video/quicktime", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".woff2": "font/woff2", ".ico": "image/x-icon",
};

function sendJson(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  res.end(data);
}

async function readBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 2_000_000) throw Object.assign(new Error("요청이 너무 큽니다"), { status: 413 });
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw Object.assign(new Error("JSON 형식이 올바르지 않습니다"), { status: 400 });
  }
}

/** Serve a file with Range support (video scrubbing needs it). */
async function sendFile(req, res, file, { cache = "no-cache" } = {}) {
  let stat;
  try {
    stat = await fsp.stat(file);
    if (!stat.isFile()) throw new Error("not a file");
  } catch {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    return res.end("찾을 수 없습니다");
  }
  const type = MIME[path.extname(file).toLowerCase()] || "application/octet-stream";
  const range = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || "");
  if (range && (range[1] || range[2])) {
    const start = range[1] ? Number(range[1]) : Math.max(0, stat.size - Number(range[2]));
    const end = range[1] && range[2] ? Math.min(Number(range[2]), stat.size - 1) : stat.size - 1;
    if (start > end || start >= stat.size) {
      res.writeHead(416, { "content-range": `bytes */${stat.size}` });
      return res.end();
    }
    res.writeHead(206, { "content-type": type, "content-length": end - start + 1, "content-range": `bytes ${start}-${end}/${stat.size}`, "accept-ranges": "bytes", "cache-control": cache });
    return fs.createReadStream(file, { start, end }).pipe(res);
  }
  res.writeHead(200, { "content-type": type, "content-length": stat.size, "accept-ranges": "bytes", "cache-control": cache });
  if (req.method === "HEAD") return res.end();
  fs.createReadStream(file).pipe(res);
}

function cookies(req) {
  return Object.fromEntries((req.headers.cookie || "").split(";").map((c) => c.trim().split("=")).filter((p) => p[0]));
}

const safeEqual = (a, b) => a.length === b.length && crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));

function authorised(req) {
  if (!TOKEN) return true;
  const given = cookies(req).studio_token || (req.headers.authorization || "").replace(/^Bearer\s+/i, "");
  return Boolean(given) && safeEqual(decodeURIComponent(given), TOKEN);
}

const LOGIN_PAGE = `<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>스튜디오 로그인</title>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#0d0d0d;color:#f0efec;font-family:system-ui,sans-serif">
<form method="post" action="/login" style="display:grid;gap:12px;width:min(320px,86vw)">
<h1 style="font-size:20px;margin:0">스튜디오 접속</h1>
<input name="token" type="password" placeholder="접속 비밀번호" autofocus style="padding:12px;border-radius:10px;border:1px solid #383835;background:#1a1a19;color:inherit;font-size:16px">
<button style="padding:12px;border-radius:10px;border:0;background:#3987e5;color:#fff;font-size:16px;font-weight:700">들어가기</button>
</form></body></html>`;

// ── What the dashboard needs to know ─────────────────────────────────────

function mediaUrl(project, rel, version) {
  return rel ? `/media/${project.id}/${rel}${version ? `?v=${version}` : ""}` : null;
}

function summarise(p) {
  const a = p.artifacts;
  const stages = {};
  for (const def of stageDefs()) {
    const st = p.stages[def.id] || { status: "pending" };
    stages[def.id] = { status: st.status, progress: st.progress || 0, label: st.label || "", error: st.error || null, summary: st.summary || "", simulated: Boolean(st.simulated), revising: Boolean(st.revision), startedAt: st.startedAt || null, finishedAt: st.finishedAt || null, requestedAt: st.requestedAt || null };
  }
  const lastCheckpoint = a.analyze?.checkpoints?.at(-1) || null;
  return {
    id: p.id,
    title: p.title,
    status: p.status,
    brief: p.brief,
    styleId: p.styleId,
    createdAt: p.createdAt,
    updatedAt: p.updatedAt,
    publishedAt: p.publishedAt || null,
    cost: p.cost || 0,
    budget: budgetFor(p),
    paused: p.paused || null,
    endedReason: p.endedReason || null,
    stages,
    gates: Object.fromEntries(Object.entries(p.gates).map(([k, g]) => [k, { status: g.status, requestedAt: g.requestedAt, feedback: g.feedback || "", rounds: g.history?.length || 0 }])),
    predicted: a.plan?.predicted || null,
    qa: a.qa ? { score: a.qa.score, passed: a.qa.passed, belowBar: a.qa.belowBar, rounds: a.qa.rounds.length, passScore: a.qa.passScore } : null,
    duration: a.edit?.duration || a.timeline?.total || null,
    poster: mediaUrl(p, a.edit?.poster, a.edit?.version),
    thumbnail: mediaUrl(p, a.package?.thumbnailFile),
    placeholders: placeholdersOf(p),
    publish: a.publish?.results?.map((r) => ({ platform: r.platform, url: r.url, dryRun: r.dryRun })) || null,
    metrics: lastCheckpoint,
  };
}

function detail(p) {
  const a = p.artifacts;
  const withUrls = {
    ...a,
    voice: a.voice && { ...a.voice, scenes: a.voice.scenes.map((s) => ({ ...s, rawWords: undefined, url: mediaUrl(p, s.file) })) },
    visuals: a.visuals && { ...a.visuals, scenes: a.visuals.scenes.map((s) => ({ ...s, imageUrl: mediaUrl(p, s.image), clipUrl: mediaUrl(p, s.clip) })) },
    sound: a.sound && { ...a.sound, url: mediaUrl(p, a.sound.music) },
    package: a.package && { ...a.package, thumbnailUrl: mediaUrl(p, a.package.thumbnailFile) },
    edit: a.edit && { ...a.edit, url: mediaUrl(p, a.edit.file, a.edit.version), posterUrl: mediaUrl(p, a.edit.poster, a.edit.version) },
    qa: a.qa && { ...a.qa, rounds: a.qa.rounds.map((r) => ({ ...r, frames: r.frames.map((f) => mediaUrl(p, f)) })) },
    prevScript: undefined,
  };
  return { ...summarise(p), gatesFull: p.gates, stagesFull: p.stages, costBy: p.costBy || {}, artifacts: withUrls };
}

function state() {
  const working = {};
  for (const k of runningStages()) {
    const [projectId, stageId] = k.split(":");
    const def = stageDefs().find((d) => d.id === stageId);
    if (def?.agent) (working[def.agent] ||= []).push({ projectId, stageId });
  }
  const routing = routingStatus();
  return {
    studio: {
      name: config.studio.name || "스튜디오",
      mode: config.studio.mode || "auto",
      quality: config.studio.quality || {},
      publish: config.studio.publish || {},
      budget: config.studio.budget || {},
      learning: config.studio.learning || {},
      version: config.version,
      errors: config.errors,
    },
    brainSimulated: brainIsSimulated(),
    pipeline: config.pipeline,
    agents: [...config.agents.values()].map((ag) => ({
      id: ag.id, name: ag.name, icon: ag.icon, summary: ag.summary, uses: ag.uses || [], model: ag.model || null, effort: ag.effort || null, file: ag.file,
      stages: stageDefs().filter((d) => d.agent === ag.id).map((d) => d.title),
      routes: Object.fromEntries((ag.uses || []).filter((u) => routing[u]).map((u) => [u, routing[u].find((c) => c.active) || null])),
    })),
    styles: [...config.styles.values()],
    formats: [...config.formats.values()],
    channels: [...config.channels.values()],
    fonts: FONT_FAMILIES,
    routing,
    secrets: secretStatus(),
    connections: { youtube: youtube.connected(), youtubeReady: hasSecret("YOUTUBE_CLIENT_ID") && hasSecret("YOUTUBE_CLIENT_SECRET"), instagram: instagram.available() },
    projects: allProjects().map(summarise),
    working,
    activity: bus.activity.slice(-80),
    playbook: playbookStats(),
    scout: readJson("scout.json", null),
  };
}

// ── Routes ───────────────────────────────────────────────────────────────

const routes = [];
const route = (method, pattern, handler) => routes.push({ method, pattern, handler });

route("GET", /^\/api\/state$/, () => state());

route("GET", /^\/api\/projects\/([\w-]+)$/, async (_req, [id]) => {
  const p = getProject(id);
  return { ...detail(p), log: await readLog(id, 300) };
});

route("POST", /^\/api\/projects$/, async (req) => {
  const body = await readBody(req);
  if (body.format && !config.formats.has(body.format)) throw Object.assign(new Error("알 수 없는 포맷입니다"), { status: 400 });
  const p = createProject({ topic: String(body.topic || ""), format: body.format, channel: body.channel, style: body.style, notes: String(body.notes || ""), targets: body.targets });
  return { id: p.id };
});

route("POST", /^\/api\/projects\/([\w-]+)\/gates\/(\w+)$/, async (req, [id, gate]) => {
  const body = await readBody(req);
  await decideGate(id, gate, { decision: body.decision, comment: body.comment, options: body.options || {} });
  return { ok: true };
});

route("POST", /^\/api\/projects\/([\w-]+)\/actions$/, async (req, [id]) => {
  const body = await readBody(req);
  if (body.action === "retry") retryStage(id, body.stageId);
  else if (body.action === "redo") redoStage(id, body.stageId, String(body.notes || ""));
  else if (body.action === "resume") resumeProject(id, { addBudget: Number(body.addBudget) || 0 });
  else if (body.action === "cancel") cancelProject(id);
  else if (body.action === "delete") await deleteProject(id);
  else throw Object.assign(new Error("알 수 없는 동작입니다"), { status: 400 });
  return { ok: true };
});

route("GET", /^\/api\/playbook$/, () => ({ lessons: allLessons(), history: playbookHistory(), stats: playbookStats() }));
route("POST", /^\/api\/playbook$/, async (req) => {
  const body = await readBody(req);
  const added = addLessons([{ text: body.text, agent: body.agent || "", format: body.format || "", style: body.style || "", evidence: "직접 입력한 지시", confidence: 1 }], { source: "manual", origin: "real" });
  if (!added.length) throw Object.assign(new Error("이미 같은 내용의 노트가 있거나 내용이 너무 짧습니다"), { status: 409 });
  return { ok: true };
});
route("PATCH", /^\/api\/playbook\/([\w-]+)$/, async (req, [id]) => {
  const body = await readBody(req);
  setLessonStatus(id, body.status === "retired" ? "retired" : "active", "직접 변경");
  return { ok: true };
});
route("DELETE", /^\/api\/playbook\/([\w-]+)$/, (_req, [id]) => {
  deleteLesson(id);
  return { ok: true };
});

route("GET", /^\/api\/config\/files$/, () => ({ files: listConfigFiles() }));
route("GET", /^\/api\/config\/file$/, async (_req, _m, url) => ({ path: url.searchParams.get("path"), content: await readConfigFile(url.searchParams.get("path") || "") }));
route("PUT", /^\/api\/config\/file$/, async (req) => {
  const body = await readBody(req);
  await writeConfigFile(String(body.path || ""), String(body.content ?? ""));
  return { ok: true };
});
route("DELETE", /^\/api\/config\/file$/, async (_req, _m, url) => {
  await deleteConfigFile(url.searchParams.get("path") || "");
  return { ok: true };
});

/** Quick switches from the dashboard; each one edits the YAML in place (comments preserved). */
route("POST", /^\/api\/settings$/, async (req) => {
  const b = await readBody(req);
  if (b.mode !== undefined) {
    if (!["auto", "simulation"].includes(b.mode)) throw Object.assign(new Error("mode 는 auto 또는 simulation"), { status: 400 });
    await patchYaml("studio.yaml", ["mode"], b.mode);
  }
  if (b.publishLive !== undefined) await patchYaml("studio.yaml", ["publish", "live"], Boolean(b.publishLive));
  if (b.passScore !== undefined) await patchYaml("studio.yaml", ["quality", "pass_score"], Math.max(50, Math.min(98, Number(b.passScore) || 80)));
  if (b.autopilot) {
    const ch = config.channels.get(b.autopilot.channel);
    if (!ch) throw Object.assign(new Error("채널을 찾을 수 없습니다"), { status: 404 });
    if (b.autopilot.enabled !== undefined) await patchYaml(ch.file, ["autopilot", "enabled"], Boolean(b.autopilot.enabled));
    if (b.autopilot.daily) for (const [f, n] of Object.entries(b.autopilot.daily)) if (config.formats.has(f)) await patchYaml(ch.file, ["autopilot", "daily", f], Math.max(0, Math.min(10, Number(n) || 0)));
  }
  return { ok: true };
});

route("POST", /^\/api\/secrets$/, async (req) => {
  const body = await readBody(req);
  const allowed = [...KNOWN_KEYS.map((k) => k.key), "INSTAGRAM_LOGIN_TYPE", "PUBLIC_BASE_URL"];
  if (!allowed.includes(body.key)) throw Object.assign(new Error("알 수 없는 키입니다"), { status: 400 });
  setSecret(body.key, String(body.value || "").trim());
  bus.send("config", { files: ["secrets"], errors: config.errors, version: config.version });
  return { ok: true };
});

// YouTube account connection (OAuth). The redirect URI to register is <this address>/api/oauth/youtube/callback
const oauthStates = new Map();
const baseUrlOf = (req) => secret("PUBLIC_BASE_URL") || `http://${req.headers.host}`;

route("GET", /^\/api\/oauth\/youtube\/start$/, (req, _m, _url, res) => {
  if (!hasSecret("YOUTUBE_CLIENT_ID") || !hasSecret("YOUTUBE_CLIENT_SECRET")) throw Object.assign(new Error("먼저 YouTube OAuth 클라이언트 ID와 보안 비밀을 입력하세요"), { status: 400 });
  const stateId = crypto.randomBytes(16).toString("hex");
  oauthStates.set(stateId, Date.now());
  res.writeHead(302, { location: youtube.authUrl(`${baseUrlOf(req)}/api/oauth/youtube/callback`, stateId) });
  res.end();
  return undefined;
});

route("GET", /^\/api\/oauth\/youtube\/callback$/, async (req, _m, url, res) => {
  const stateId = url.searchParams.get("state");
  const page = (msg) => {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    res.end(`<!doctype html><meta charset="utf-8"><body style="font-family:system-ui;padding:40px;background:#0d0d0d;color:#f0efec"><h2>${msg}</h2><p><a href="/#/settings" style="color:#3987e5">스튜디오로 돌아가기</a></p></body>`);
  };
  if (!stateId || !oauthStates.has(stateId) || Date.now() - oauthStates.get(stateId) > 10 * 60_000) return page("연결 요청이 만료되었습니다. 다시 시도해 주세요."), undefined;
  oauthStates.delete(stateId);
  if (url.searchParams.get("error")) return page("Google 계정 연결이 취소되었습니다."), undefined;
  try {
    await youtube.exchangeCode(url.searchParams.get("code"), `${baseUrlOf(req)}/api/oauth/youtube/callback`);
    bus.log({ kind: "config", text: "YouTube 계정이 연결되었습니다" });
    page("YouTube 계정이 연결되었습니다 ✓");
  } catch (err) {
    page(`연결 실패: ${String(err.message).replace(/[<>]/g, "")}`);
  }
  return undefined;
});

/** AI 기술 스카우트: look for newer models than the ones in routing.yaml and propose changes. */
let scouting = false;
route("POST", /^\/api\/scout$/, async () => {
  if (scouting) throw Object.assign(new Error("이미 조사 중입니다"), { status: 409 });
  scouting = true;
  bus.send("agent", { agentId: "tech-scout", state: "working" });
  (async () => {
    try {
      const current = Object.fromEntries(["image", "video", "tts", "music", "llm"].map((c) => [c, (config.routing.capabilities[c] || []).map((e) => (typeof e === "string" ? e : e.use))]));
      let notes = { text: "", sources: [] };
      if (!resolve("search")?.isLocal) {
        const { call } = await import("./providers/registry.js");
        notes = await call("search", {
          llmModel: config.agents.get("tech-scout")?.model,
          system: config.agents.get("tech-scout")?.prompt || "",
          user: `오늘 날짜 기준으로, 아래 분야별 현재 사용 모델보다 나은 최신 생성형 AI 모델이 API로 공개되었는지 조사하세요. 공식 발표와 문서를 우선 확인하고 출처를 남기세요.\n\n${JSON.stringify(current, null, 1)}`,
          maxSearches: 10,
        });
      }
      const { data } = await runAgent("tech-scout", "scout", {
        instruction: "조사 노트를 바탕으로 라우팅에 추가할 후보를 제안하세요. 바꿀 이유가 없으면 제안을 비워 둡니다.",
        input: { current_routing: current, research_notes: notes.text, sources: notes.sources },
        schema: ScoutReport,
        ctx: {},
      });
      writeJson("scout.json", { at: Date.now(), simulated: brainIsSimulated(), ...data });
      bus.log({ kind: "scout", agentId: "tech-scout", text: `AI 기술 스카우트 보고 — 제안 ${data.proposals.length}건` });
    } catch (err) {
      writeJson("scout.json", { at: Date.now(), error: String(err.message).slice(0, 300), summary: "", proposals: [] });
    } finally {
      scouting = false;
      bus.send("agent", { agentId: "tech-scout", state: "idle" });
      bus.send("config", { files: ["scout"], errors: config.errors, version: config.version });
    }
  })();
  return { ok: true };
});

// ── Server ───────────────────────────────────────────────────────────────

const VENDOR = {
  "/vendor/htm.js": path.join(NODE_MODULES, "htm/preact/standalone.module.js"),
  "/vendor/pretendard.woff2": path.join(NODE_MODULES, "pretendard/dist/web/variable/woff2/PretendardVariable.woff2"),
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const p = decodeURIComponent(url.pathname);

  try {
    // Signed public media (only used when a platform must fetch the video from us).
    const pub = /^\/public\/([a-f0-9]+)\/([\w-]+)\/(.+)$/.exec(p);
    if (pub) {
      if (!verifySignature(pub[1], pub[2], pub[3])) return sendJson(res, 403, { error: "서명이 올바르지 않습니다" });
      return sendFile(req, res, safeJoin(path.join(PROJECTS_DIR, pub[2], "media"), pub[3]));
    }

    if (p === "/login") {
      if (req.method === "POST") {
        const form = new URLSearchParams(Buffer.concat(await req.toArray()).toString());
        if (TOKEN && safeEqual(form.get("token") || "", TOKEN)) {
          res.writeHead(302, { location: "/", "set-cookie": `studio_token=${encodeURIComponent(TOKEN)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000` });
          return res.end();
        }
      }
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      return res.end(LOGIN_PAGE);
    }

    const oauthCallback = p === "/api/oauth/youtube/callback"; // protected by its own one-time state
    if (!authorised(req) && !oauthCallback) {
      if (p.startsWith("/api/")) return sendJson(res, 401, { error: "로그인이 필요합니다" });
      res.writeHead(302, { location: "/login" });
      return res.end();
    }

    // Live updates
    if (p === "/api/events") {
      res.writeHead(200, { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-store", connection: "keep-alive", "x-accel-buffering": "no" });
      res.write("retry: 2000\n\n");
      const onEvent = (e) => res.write(`data: ${JSON.stringify(e)}\n\n`);
      const ping = setInterval(() => res.write(": ping\n\n"), 20_000);
      bus.on("event", onEvent);
      req.on("close", () => {
        clearInterval(ping);
        bus.off("event", onEvent);
      });
      return;
    }

    if (p.startsWith("/api/")) {
      // State-changing requests must come from our own page.
      if (req.method !== "GET" && req.headers.origin && new URL(req.headers.origin).host !== req.headers.host) return sendJson(res, 403, { error: "허용되지 않은 출처입니다" });
      for (const r of routes) {
        const m = r.method === req.method && r.pattern.exec(p);
        if (!m) continue;
        const out = await r.handler(req, m.slice(1), url, res);
        if (out !== undefined && !res.headersSent) sendJson(res, 200, out);
        return;
      }
      return sendJson(res, 404, { error: "없는 API입니다" });
    }

    const media = /^\/media\/([\w-]+)\/(.+)$/.exec(p);
    if (media) return sendFile(req, res, safeJoin(path.join(PROJECTS_DIR, media[1], "media"), media[2]), { cache: url.searchParams.has("v") ? "private, max-age=86400" : "no-cache" });

    if (VENDOR[p]) return sendFile(req, res, VENDOR[p], { cache: "public, max-age=604800" });

    // Caption / title fonts, so the style previews show the real typefaces.
    if (p.startsWith("/fonts/@fontsource/")) return sendFile(req, res, safeJoin(path.join(NODE_MODULES, "@fontsource"), p.slice("/fonts/@fontsource/".length)), { cache: "public, max-age=604800" });

    // Dashboard (no build step: files are served as they are on disk, so edits show on refresh)
    const file = p === "/" ? "index.html" : p.slice(1);
    return sendFile(req, res, safeJoin(WEB_DIR, file));
  } catch (err) {
    const status = err.status || 500;
    if (status >= 500) console.error(`[http] ${req.method} ${p}:`, err);
    if (!res.headersSent) sendJson(res, status, { error: String(err.message || err) });
    else res.end();
  }
});

// ── Boot ─────────────────────────────────────────────────────────────────

initStore();
loadAll();
if (config.errors.length) for (const e of config.errors) console.error(`[config] ${e.file}: ${e.message}`);
watchConfig();
recover();
startScheduler();
// Switching autopilot on (or raising a quota) takes effect right away, not at the next minute tick.
bus.on("event", (e) => e.type === "config" && autopilot());

server.listen(PORT, HOST, () => {
  const mode = brainIsSimulated() ? "시뮬레이션 두뇌 (Anthropic 키 미연결)" : "Claude 연결됨";
  console.log(`\n  ${config.studio.name || "스튜디오"} 가동 — http://${LOOPBACK ? "localhost" : HOST}:${PORT}\n  ${mode} · 설정 폴더 studio/ 는 저장 즉시 반영됩니다\n`);
});

async function shutdown() {
  server.close();
  await flush().catch(() => {});
  await closeBrowser();
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
process.on("unhandledRejection", (err) => console.error("[unhandled]", err));
