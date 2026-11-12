// Environment check: is everything the studio needs installed, and which AIs are connected?
//   npm run check
import { spawnSync } from "node:child_process";
import { loadDotEnv, secretStatus } from "../server/secrets.js";
import { initStore } from "../server/store.js";
import { loadAll, config } from "../server/config.js";
import { routingStatus } from "../server/providers/registry.js";
import { getBrowser, closeBrowser } from "../server/media/render.js";
import { FFMPEG, FFPROBE } from "../server/media/bin.js";

loadDotEnv();
initStore();
let failed = false;
const ok = (label, detail = "") => console.log(`  ✓ ${label}${detail ? `  ${detail}` : ""}`);
const bad = (label, detail = "") => {
  failed = true;
  console.log(`  ✗ ${label}${detail ? `  ${detail}` : ""}`);
};

console.log("\n[필수 프로그램]");
const major = Number(process.versions.node.split(".")[0]);
major >= 20 ? ok("Node.js", process.versions.node) : bad("Node.js 20 이상이 필요합니다", process.versions.node);
for (const [name, bin] of [["ffmpeg", FFMPEG], ["ffprobe", FFPROBE]]) {
  const r = spawnSync(bin, ["-version"], { encoding: "utf8", windowsHide: true });
  r.status === 0 ? ok(name, r.stdout.split("\n")[0].split(" ").slice(0, 3).join(" ")) : bad(`${name} 을(를) 찾지 못했습니다`, "npm install 을 다시 실행하세요");
}
try {
  const browser = await getBrowser();
  ok("모션그래픽 렌더러 (Chromium)", browser.version());
} catch (err) {
  bad("모션그래픽 렌더러", err.message);
}
await closeBrowser();

console.log("\n[설정 파일]");
loadAll();
if (config.errors.length) for (const e of config.errors) bad(e.file, e.message);
else ok(`studio/ 폴더`, `AI 팀 ${config.agents.size}명 · 그림체 ${config.styles.size}종 · 포맷 ${config.formats.size}종 · 채널 ${config.channels.size}개`);

console.log("\n[AI 연결 키]");
for (const k of secretStatus()) console.log(`  ${k.set ? "●" : "○"} ${k.key.padEnd(24)} ${k.set ? "설정됨" : "미설정"}  — ${k.label}`);

console.log("\n[지금 각 분야를 맡는 AI]");
for (const [cap, list] of Object.entries(routingStatus())) {
  const active = list.find((c) => c.active);
  console.log(`  ${cap.padEnd(20)} ${active ? (active.local ? `로컬 엔진 (${active.model})` : active.use) : "없음"}`);
}

console.log(failed ? "\n위의 ✗ 항목을 해결한 뒤 다시 실행하세요.\n" : "\n준비 완료 — npm start 로 스튜디오를 켜세요.\n");
process.exit(failed ? 1 : 0);
