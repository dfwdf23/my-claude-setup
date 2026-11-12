// End-to-end smoke test: makes one video through the running studio's API and
// approves both gates itself, printing each stage change.
//
//   npm start            (in another terminal)
//   npm run demo -- --format=shorts --topic="잠을 줄이면 뇌에서 벌어지는 일"
//
// Options: --format=shorts|long|reels  --style=<id>|auto  --topic="..."
//          --revise-plan   ask for one plan revision before approving
//          --revise-upload ask for one change to the finished video before approving
// With no API keys this runs entirely on the local stand-in engines (no cost).

const BASE = process.env.STUDIO_URL || `http://localhost:${process.env.PORT || 4400}`;
const args = Object.fromEntries(process.argv.slice(2).map((a) => a.replace(/^--/, "").split("=")).map(([k, v]) => [k, v ?? true]));
const headers = { "content-type": "application/json", ...(process.env.STUDIO_TOKEN ? { authorization: `Bearer ${process.env.STUDIO_TOKEN}` } : {}) };

async function call(path, body) {
  const res = await fetch(BASE + path, body ? { method: "POST", headers, body: JSON.stringify(body) } : { headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`${path} → ${res.status} ${data.error || ""}`);
  return data;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const { id } = await call("/api/projects", { topic: args.topic === true ? "" : (args.topic ?? "잠을 줄이면 뇌에서 벌어지는 일"), format: args.format || "shorts", style: args.style || "auto" });
console.log(`프로젝트 ${id} 접수 — ${BASE}/#/projects/${id}`);

const started = Date.now();
let last = "";
let planRevised = false;
let uploadRevised = false;

for (;;) {
  await sleep(1500);
  const p = await call(`/api/projects/${id}`);
  const line = Object.entries(p.stages).filter(([, s]) => s.status !== "pending").map(([k, s]) => `${k}:${{ done: "✓", running: `${Math.round(s.progress * 100)}%`, waiting: "결재", failed: "✗" }[s.status]}`).join(" ");
  if (line !== last) {
    console.log(`${String(Math.round((Date.now() - started) / 1000)).padStart(4)}s  ${line}`);
    last = line;
  }
  if (p.stages.plan_gate?.status === "waiting") {
    const plan = p.artifacts.plan;
    console.log(`      기획안: 「${plan.working_title}」 · 그림체 ${plan.style_id} · ${plan.duration_sec}초`);
    if (args["revise-plan"] && !planRevised) {
      planRevised = true;
      await call(`/api/projects/${id}/gates/plan`, { decision: "revise", comment: "제목에 숫자를 꼭 넣어 주세요" });
      console.log("      → 수정 요청");
    } else {
      await call(`/api/projects/${id}/gates/plan`, { decision: "approve", options: { title: plan.title_options[0] } });
      console.log("      → 기획 승인");
    }
  } else if (p.stages.upload_gate?.status === "waiting") {
    console.log(`      완성본: 품질 ${p.qa.score}점 (검수 ${p.qa.rounds}회) · ${p.duration}초${p.placeholders.length ? ` · 대체 엔진: ${p.placeholders.join(", ")}` : ""}`);
    if (args["revise-upload"] && !uploadRevised) {
      uploadRevised = true;
      await call(`/api/projects/${id}/gates/upload`, { decision: "revise", comment: "썸네일 문구를 더 짧고 강하게 바꿔 주세요" });
      console.log("      → 수정 요청");
    } else {
      await call(`/api/projects/${id}/gates/upload`, { decision: "approve", options: { privacy: "private" } });
      console.log("      → 업로드 승인");
    }
  } else if (p.status === "failed") {
    for (const [k, s] of Object.entries(p.stages)) if (s.error) console.error(`실패 — ${k}: ${s.error}`);
    process.exit(1);
  } else if (p.status === "done") {
    const r = p.artifacts.publish.results[0];
    console.log(`완료 — ${p.artifacts.edit.duration}초 영상, ${(p.artifacts.edit.size / 1048576).toFixed(1)}MB`);
    console.log(r.dryRun ? `업로드 패키지: ${r.outboxDir}` : `게시됨: ${r.url}`);
    break;
  } else if (Date.now() - started > 30 * 60_000) {
    console.error("30분 안에 끝나지 않았습니다");
    process.exit(1);
  }
}
