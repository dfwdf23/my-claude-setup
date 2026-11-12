import fs from "node:fs";
import fsp from "node:fs/promises";
import { config } from "../config.js";
import { providers, resolve } from "../providers/registry.js";
import { composite, voiceSlot, concatVideo, concatAudio, master, grabFrame, measure } from "../media/ffmpeg.js";
import { mapLimit, round } from "../util.js";
import { secret } from "../secrets.js";
import { learn } from "../learning/retro.js";
import { signedMediaUrl } from "../public-url.js";
import * as S from "../schemas.js";

/** 후반 · 검수 · 배포 · 학습 단계 */

export async function edit(sc) {
  const { fps } = sc.format.canvas;
  const timeline = sc.art.timeline;
  const clips = new Map(sc.art.visuals.scenes.map((s) => [s.id, s]));
  const overlays = new Map(sc.art.motion.scenes.map((s) => [s.id, s]));
  const voices = new Map(sc.art.voice.scenes.map((s) => [s.id, s]));
  const work = sc.dir("edit");
  let done = 0;

  // 1) Each scene: footage + motion-graphics overlay.
  const sceneFiles = await mapLimit(timeline.scenes, 2, async (slot) => {
    const out = `${work}/scene_${slot.id}.mp4`;
    await composite({ baseFile: sc.abs(clips.get(slot.id).clip), overlayFile: sc.abs(overlays.get(slot.id)?.overlay) || null, outFile: out, fps, duration: slot.duration });
    sc.progress((0.7 * ++done) / timeline.scenes.length, `${done}/${timeline.scenes.length} 장면 합성`);
    return out;
  });

  // 2) Narration track: every voice clip sits in a slot exactly as long as its scene.
  const slotFiles = await mapLimit(timeline.scenes, 4, async (slot) => {
    const v = voices.get(slot.id);
    return voiceSlot({ voiceFile: v?.file ? sc.abs(v.file) : null, outFile: `${work}/vo_${slot.id}.wav`, duration: slot.duration, lead: sc.art.voice.lead });
  });

  sc.progress(0.78, "장면을 이어 붙이는 중");
  const video = await concatVideo(sceneFiles, `${work}/video.mp4`);
  const narration = await concatAudio(slotFiles, `${work}/narration.wav`);

  // 3) Mix, duck the music under the voice, normalise loudness, mux.
  sc.progress(0.88, "오디오 믹싱·마스터링");
  const final = sc.file("", "final.mp4");
  await master({ videoFile: video, voiceFile: narration, musicFile: sc.art.sound?.music ? sc.abs(sc.art.sound.music) : null, outFile: final, duration: timeline.total, hasVoice: !sc.art.voice.placeholder });

  const poster = sc.file("", "poster.jpg");
  await grabFrame(final, Math.min(1.2, timeline.total / 2), poster, { width: 540 });
  for (const f of [...sceneFiles, ...slotFiles, video, narration]) fs.rmSync(f, { force: true });

  const size = (await fsp.stat(final)).size;
  sc.set("edit", { file: sc.rel(final), poster: sc.rel(poster), duration: timeline.total, size, version: Date.now() });
  return `${timeline.total.toFixed(1)}초 · ${(size / 1_048_576).toFixed(1)}MB`;
}

/** Which jobs were done by a local stand-in rather than a real AI. */
export function placeholdersOf(project) {
  const a = project.artifacts;
  const out = [];
  if (["plan", "script", "storyboard", "package"].some((id) => project.stages[id]?.simulated)) out.push("두뇌(기획·대본)");
  if (a.visuals?.placeholder) out.push("이미지·영상");
  if (a.voice?.placeholder) out.push("성우");
  if (a.sound?.placeholder) out.push("음악");
  return out;
}

function technicalCheck(sc, m) {
  const { width, height } = sc.format.canvas;
  const problems = [];
  if (m.width !== width || m.height !== height) problems.push(`해상도가 ${m.width}×${m.height} 입니다 (규격 ${width}×${height})`);
  if (m.duration > sc.format.duration.max + 0.5) problems.push(`길이 ${m.duration.toFixed(1)}초 — 최대 ${sc.format.duration.max}초 초과`);
  if (!sc.simulated && m.duration < sc.format.duration.min - 0.5) problems.push(`길이 ${m.duration.toFixed(1)}초 — 최소 ${sc.format.duration.min}초 미달`);
  if (m.loudness != null && (m.loudness < -17 || m.loudness > -11)) problems.push(`음량 ${m.loudness} LUFS (권장 -14 안팎)`);
  if (!sc.art.voice.placeholder && m.silences.some((s) => s > 3)) problems.push(`${Math.max(...m.silences).toFixed(1)}초짜리 무음 구간이 있습니다`);
  if (m.blackSeconds > 1) problems.push(`검은 화면이 ${m.blackSeconds.toFixed(1)}초 있습니다`);
  return problems;
}

const REVISABLE = { script: "script", storyboard: "storyboard", visuals: "storyboard", motion: "storyboard", voice: "voice", sound: "sound", package: "package" };

export async function qa(sc) {
  const final = sc.abs(sc.art.edit.file);
  const rounds = sc.art.qa?.rounds || [];
  const roundNo = rounds.length;
  const criteria = config.rubric.criteria;

  sc.progress(0.1, "기술 규격을 측정하는 중");
  const m = await measure(final);
  const problems = technicalCheck(sc, m);
  const tech = { width: m.width, height: m.height, duration: round(m.duration, 2), fps: round(m.fps, 2), loudness: m.loudness, problems };

  // Frames for the reviewer to actually look at: the opening, then evenly through the cut.
  sc.progress(0.3, "검수용 프레임을 뽑는 중");
  const count = Math.min(8, Math.max(4, Math.round(m.duration / 6)));
  const times = [0.4, ...Array.from({ length: count - 1 }, (_, i) => ((i + 1) * m.duration) / count)];
  const frames = await mapLimit(times, 4, (t, i) => grabFrame(final, Math.min(t, m.duration - 0.1), `${sc.dir("qa")}/r${roundNo}_f${i}.jpg`, { width: 480 }));

  const placeholders = placeholdersOf(sc.project);
  sc.progress(0.5, "기준표로 채점하는 중");
  const report = await sc.agent("qa-director", "qa", {
    instruction: `완성본을 기준표로 채점하세요. 첨부 이미지는 영상에서 뽑은 프레임 ${frames.length}장입니다 (첫 장이 시작 0.4초 지점, 이후 시간순).`,
    input: {
      projectId: sc.project.id,
      round: roundNo,
      rubric: criteria.map((c) => ({ id: c.id, name: c.name, weight: c.weight, standard: c.standard })),
      plan: { viewer_promise: sc.art.plan.viewer_promise, hook: sc.art.plan.hook, predicted: sc.art.plan.predicted },
      script: sc.art.script.scenes.map((s) => ({ id: s.id, beat: s.beat, narration: s.narration })),
      storyboard: sc.art.storyboard.scenes.map((s) => ({ id: s.id, mode: s.mode, graphics: s.graphics.map((g) => g.type) })),
      package: { title: sc.art.package.title, description: sc.art.package.description, thumbnail: sc.art.package.thumbnail, hashtags: sc.art.package.hashtags },
      tech,
      frame_times_sec: times.map((t) => round(t, 1)),
      hasPlaceholders: placeholders.length > 0,
      placeholder_parts: placeholders,
      previous_issues: rounds.at(-1)?.report.issues || [],
    },
    schema: S.QaReport,
    images: frames.map((file) => ({ file, mediaType: "image/jpeg" })),
  });

  // Weighted total. The technical row is ours to score — it is measured, not judged.
  const byId = new Map(report.scores.map((s) => [s.id, s]));
  if (problems.length) byId.set("technical", { id: "technical", score: Math.min(byId.get("technical")?.score ?? 60, 60), note: problems.join(" / ") });
  let weightSum = 0;
  let total = 0;
  const scores = criteria.map((c) => {
    const s = byId.get(c.id) || { score: 0, note: "채점 누락" };
    weightSum += c.weight;
    total += c.weight * Math.max(0, Math.min(100, s.score));
    return { id: c.id, name: c.name, weight: c.weight, score: Math.round(s.score), note: s.note };
  });
  const score = Math.round(total / weightSum);
  const passScore = config.studio.quality?.pass_score ?? 80;
  const blocking = report.issues.filter((i) => i.severity === "high" && REVISABLE[i.stage]);
  const passed = score >= passScore && blocking.length === 0;

  rounds.push({ round: roundNo, at: Date.now(), score, passed, scores, tech, report: { strengths: report.strengths, issues: report.issues, predicted: report.predicted, summary: report.summary }, frames: frames.map((f) => sc.rel(f)), simulated: sc.simulated });
  const maxRounds = config.studio.quality?.max_revision_rounds ?? 2;

  // Group fixable issues by the stage that has to redo its work.
  const targets = new Map();
  for (const issue of report.issues) {
    const stage = REVISABLE[issue.stage];
    if (!stage || (passed && issue.severity !== "high")) continue;
    const t = targets.get(stage) || { stage, notes: [], sceneIds: [], whole: false };
    t.notes.push(`${issue.scene_id ? `[${issue.scene_id}] ` : ""}${issue.problem} → ${issue.fix}`);
    if (issue.scene_id) t.sceneIds.push(issue.scene_id);
    else t.whole = true;
    targets.set(stage, t);
  }
  const revise = !passed && roundNo < maxRounds && targets.size > 0;
  const belowBar = !passed && !revise;

  sc.set("qa", { rounds, score, passed, belowBar, passScore, placeholders });
  if (revise) {
    return {
      summary: `${score}점 — 기준(${passScore}점) 미달, ${[...targets.keys()].join("·")} 재작업 지시`,
      revise: [...targets.values()].map((t) => ({ stage: t.stage, notes: t.notes.join("\n"), sceneIds: t.whole ? [] : [...new Set(t.sceneIds)] })),
    };
  }
  return `${score}점 ${passed ? "합격" : `— 기준 미달이지만 재작업 한도(${maxRounds}회) 도달, 결재로 올립니다`}`;
}

export async function publish(sc) {
  const gate = sc.project.gates.upload;
  const opts = gate?.options || {};
  const pkg = sc.art.package;
  const final = sc.abs(sc.art.edit.file);
  const live = config.studio.publish?.live === true && config.studio.mode !== "simulation";
  const placeholders = placeholdersOf(sc.project);
  const targets = opts.targets?.length ? opts.targets : sc.project.brief.targets;
  const title = opts.title || pkg.title;
  const hashtags = pkg.hashtags.map((h) => `#${h}`).join(" ");
  const description = `${opts.description || pkg.description}\n\n${hashtags}`.trim();
  const privacy = opts.privacy || config.studio.publish?.default_privacy || "private";
  const disclose = opts.disclose ?? config.studio.publish?.disclose_ai ?? true;

  if (live && placeholders.length) {
    throw Object.assign(new Error(`대체 엔진으로 만든 구간(${placeholders.join(", ")})이 있어 실제 업로드를 막았습니다. 해당 AI를 연결해 다시 제작하거나, 업로드 패키지만 받으려면 studio.yaml 의 publish.live 를 false 로 두세요.`), { fatal: true });
  }

  const results = sc.art.publish?.results?.filter((r) => r.ok) || [];
  const done = new Set(results.map((r) => r.platform));
  for (const platform of targets) {
    if (done.has(platform)) continue; // a retry never uploads the same video twice
    const isYoutube = platform.startsWith("youtube");
    const capability = isYoutube ? "publish.youtube" : "publish.instagram";
    const args = isYoutube
      ? {
          file: final, title, description, tags: pkg.tags, privacy, publishAt: opts.publishAt || null,
          thumbnailFile: sc.abs(pkg.thumbnailFile), categoryId: sc.channel.upload?.youtube?.category_id, madeForKids: sc.channel.upload?.youtube?.made_for_kids,
          disclose, language: sc.channel.language || "ko", isShort: platform === "youtube_shorts",
        }
      : {
          file: final, caption: `${pkg.instagram_caption || title}\n\n${hashtags}`.trim(), shareToFeed: sc.channel.upload?.instagram?.share_to_feed ?? true,
          thumbOffsetMs: 1000, disclose, publicUrl: secret("PUBLIC_BASE_URL") ? signedMediaUrl(sc.project.id, sc.art.edit.file) : null,
        };
    sc.progress(results.length / targets.length, `${platform} 업로드 중`);
    // Dry-run by default: nothing leaves this machine until publish.live is switched on.
    const r = live ? await sc.call(capability, args) : { ...(await providers.local.capabilities[capability](args, sc.routeCtx())), use: "local/outbox" };
    results.push({ platform, ok: true, id: r.id, url: r.url, dryRun: Boolean(r.dryRun), outboxDir: r.outboxDir || null, warnings: r.warnings || [], at: Date.now(), use: r.use });
    sc.set("publish", { results, title, privacy, publishAt: opts.publishAt || null });
  }

  sc.set("publish", { results, title, privacy, publishAt: opts.publishAt || null });
  sc.project.publishedAt = Date.now();
  const dry = results.every((r) => r.dryRun);
  return dry ? `업로드 패키지 저장 (${results.map((r) => r.platform).join(", ")}) — 실제 업로드는 꺼져 있습니다` : results.map((r) => `${r.platform}: ${r.url || r.id}`).join(" · ");
}

export async function analyze(sc) {
  // Learn from how the production itself went; performance data is collected later, on a schedule.
  const lessons = await learn(sc.project, "production").catch((err) => {
    sc.log(`학습 정리를 건너뜁니다 — ${err.message}`, "warn");
    return [];
  });
  const hours = config.studio.learning?.analytics_checkpoints_hours || [24, 72, 168];
  const simulated = sc.art.publish.results.every((r) => r.dryRun) || Boolean(resolve("analytics.youtube")?.isLocal);
  sc.set("analyze", { checkpoints: [], pending: [...hours], simulated, lessonsAdded: lessons.length });
  return `학습 노트 ${lessons.length}건 추가 · 성과 확인 예약 ${hours.map((h) => (h >= 24 ? `${h / 24}일` : `${h}시간`)).join("/")} 뒤`;
}
