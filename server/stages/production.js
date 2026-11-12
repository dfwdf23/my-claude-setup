import fs from "node:fs";
import { config } from "../config.js";
import { hashOf } from "../store.js";
import { providers, resolve } from "../providers/registry.js";
import { durationOf, fitClip, kenBurns } from "../media/ffmpeg.js";
import { renderStill, fileUrl } from "../media/render.js";
import { mapLimit, speechUnits, round } from "../util.js";
import * as S from "../schemas.js";

/** 제작 단계: 성우 → (이미지·영상 | 모션그래픽 | 음악) · 썸네일/메타데이터 */

const LEAD = 0.12; // silence before the voice starts in each scene
const TAIL = 0.2; // breathing room after it ends

const mediaLimit = () => config.studio.concurrency?.media ?? 3;
const exists = (f) => Boolean(f) && fs.existsSync(f);

/** Spread word timings over [start, end] in proportion to how long each word takes to say. */
function spreadWords(words, start, end) {
  const units = words.map((w) => Math.max(0.5, speechUnits(w)));
  const total = units.reduce((a, b) => a + b, 0);
  let t = start;
  return words.map((w, i) => {
    const d = ((end - start) * units[i]) / total;
    const item = { w, start: round(t, 3), end: round(t + d * 0.94, 3) };
    t += d;
    return item;
  });
}

export async function voice(sc) {
  const script = sc.art.script;
  const brief = sc.art.storyboard.voice_brief;
  const language = sc.channel.language || "ko";
  const persona = { ...sc.style.voice, ...(sc.channel.voice || {}), persona: brief?.persona || sc.style.voice?.persona };
  const voiceCfg = { id: persona.elevenlabs_voice_id || "", name: persona.voice_name || "", speed: persona.speed || 1, persona: persona.persona };

  sc.progress(0.05, "읽기 대본을 다듬는 중");
  const direction = await sc.agent("voice-director", "voice_direction", {
    instruction: sc.revision ? "목소리 재작업 요청이 있습니다. 지적을 반영해 읽기 텍스트를 다시 다듬으세요." : "각 장면의 내레이션을 음성 합성이 자연스럽게 읽을 텍스트로 바꾸세요. 뜻은 바꾸지 않습니다.",
    input: { scenes: script.scenes.map((s) => ({ id: s.id, beat: s.beat, narration: s.narration })), voice_brief: brief, revision: sc.revision?.notes || "" },
    schema: S.VoiceDirection,
  });
  const ttsText = new Map(direction.scenes.map((s) => [s.id, s.tts_text?.trim()]));

  const route = resolve("tts", sc.routeCtx())?.use;
  const previous = new Map((sc.art.voice?.scenes || []).map((s) => [s.id, s]));
  let done = 0;

  const scenes = await mapLimit(script.scenes, mediaLimit(), async (scene, i) => {
    const text = ttsText.get(scene.id) || scene.narration;
    const hash = hashOf(text, voiceCfg, route);
    const prev = previous.get(scene.id);
    let entry;
    if (prev?.hash === hash && exists(sc.abs(prev.file))) {
      entry = { ...prev };
    } else if (!text) {
      entry = { id: scene.id, file: "", duration: 0, rawWords: [], hash };
    } else {
      const out = sc.file("voice", `${scene.id}_${hash}.${route?.startsWith("local/") ? "wav" : "mp3"}`);
      const r = await sc.call("tts", {
        text,
        voice: voiceCfg,
        language,
        previousText: i > 0 ? ttsText.get(script.scenes[i - 1].id) || "" : "",
        nextText: i < script.scenes.length - 1 ? ttsText.get(script.scenes[i + 1].id) || "" : "",
        outFile: out,
      });
      const duration = await durationOf(out);
      entry = { id: scene.id, file: sc.rel(out), duration: round(duration, 3), rawWords: r.words || null, hash, use: r.use };
    }
    entry.tts_text = text;

    // Captions always show the script as written, even when the voice read an expanded form ("3.5%" → "삼 점 오 퍼센트").
    const original = scene.narration.split(/\s+/).filter(Boolean);
    const raw = entry.rawWords;
    let words;
    if (raw?.length && raw.length === original.length) words = raw.map((w, k) => ({ w: original[k], start: w.start, end: w.end }));
    else if (raw?.length) words = spreadWords(original, raw[0].start, raw[raw.length - 1].end);
    else words = spreadWords(original, 0, Math.max(0.3, entry.duration - 0.05));
    const em = scene.emphasis_words || [];
    entry.words = words.map((w) => ({ ...w, em: em.some((e) => w.w.includes(e)) }));

    sc.progress(0.1 + (0.9 * ++done) / script.scenes.length, `${done}/${script.scenes.length} 장면 녹음`);
    return entry;
  });

  // The voice sets the clock for everything downstream.
  let t = 0;
  const timeline = scenes.map((v) => {
    const duration = round(Math.max(1.4, LEAD + v.duration + TAIL), 3);
    const slot = { id: v.id, start: round(t, 3), duration };
    t += duration;
    return slot;
  });

  sc.set("voice", { scenes, lead: LEAD, placeholder: Boolean(route?.startsWith("local/")), use: route });
  sc.set("timeline", { scenes: timeline, total: round(t, 3) });
  return `${scenes.length}장면 · 총 ${t.toFixed(1)}초${route?.startsWith("local/") ? " (성우 AI 미연결 — 무음)" : ""}`;
}

export async function visuals(sc) {
  const board = sc.art.storyboard;
  const { width, height, fps } = sc.format.canvas;
  const slots = new Map(sc.art.timeline.scenes.map((s) => [s.id, s]));
  const previous = new Map((sc.art.visuals?.scenes || []).map((s) => [s.id, s]));
  const imageRoute = resolve("image", sc.routeCtx())?.use;
  const videoRoute = resolve("video", sc.routeCtx())?.use;
  const p = sc.style.image_prompt || {};
  let done = 0;
  let reference = null; // first generated frame, reused to keep recurring characters consistent

  const makeScene = async (scene) => {
    const duration = slots.get(scene.id).duration;
    const prompt = [p.prefix, scene.image_prompt, `${scene.shot}.`, board.visual_bible.characters, board.visual_bible.consistency_tokens || p.suffix].filter(Boolean).join(" ");
    const isMotion = scene.mode === "motion";
    const imgHash = hashOf(isMotion ? ["motion", sc.style.id, scene.id] : [prompt, imageRoute, width, height]);
    const clipHash = hashOf(imgHash, scene.mode, scene.camera, duration, scene.mode === "video" ? [videoRoute, scene.motion_prompt] : "kb");
    const prev = previous.get(scene.id);
    const entry = { id: scene.id, mode: scene.mode, imgHash, clipHash, image: prev?.image, clip: prev?.clip, imageUse: prev?.imageUse, clipUse: prev?.clipUse };

    if (prev?.imgHash !== imgHash || !exists(sc.abs(prev.image))) {
      const out = sc.file("visuals", `${scene.id}_${imgHash}.png`);
      const args = { prompt, negative: p.negative, width, height, style: sc.style, seedKey: `${sc.project.id}:${scene.id}`, outFile: out, refFiles: reference && board.visual_bible.characters ? [reference] : [] };
      // Full-screen motion graphics always sit on a locally rendered backdrop in the style's palette.
      const r = isMotion ? { ...(await providers.local.capabilities.image(args)), use: "local/art" } : await sc.call("image", args);
      entry.image = sc.rel(out);
      entry.imageUse = r.use;
    }

    if (prev?.clipHash !== clipHash || !exists(sc.abs(prev.clip))) {
      const out = sc.file("visuals", `${scene.id}_${clipHash}.mp4`);
      const imageFile = sc.abs(entry.image);
      if (scene.mode === "video") {
        const raw = sc.file("visuals", `${scene.id}_${clipHash}_raw.mp4`);
        const r = await sc.call("video", { imageFile, prompt: `${scene.motion_prompt} ${sc.style.video_prompt || ""}`.trim(), duration, width, height, fps, camera: scene.camera, outFile: raw });
        if (r.local) fs.renameSync(raw, out);
        else {
          await fitClip({ inFile: raw, outFile: out, width, height, fps, duration });
          fs.rmSync(raw, { force: true });
        }
        entry.clipUse = r.use;
      } else {
        await kenBurns({ imageFile, outFile: out, width, height, fps, duration, camera: isMotion ? "static" : scene.camera });
        entry.clipUse = "local/kenburns";
      }
      entry.clip = sc.rel(out);
    }
    sc.progress(++done / board.scenes.length, `${done}/${board.scenes.length} 장면 생성`);
    return entry;
  };

  // Generate the first scene alone so the rest can reference it, then fan out.
  const first = await makeScene(board.scenes[0]);
  if (board.scenes[0].mode !== "motion") reference = sc.abs(first.image);
  const rest = await mapLimit(board.scenes.slice(1), mediaLimit(), makeScene);
  const scenes = [first, ...rest];

  const usedPlaceholderArt = scenes.some((s) => s.mode !== "motion" && s.imageUse?.startsWith("local/"));
  sc.set("visuals", { scenes, placeholder: usedPlaceholderArt });
  return `${scenes.length}장면${usedPlaceholderArt ? " (이미지 AI 미연결 — 대체 배경)" : ""}`;
}

/** Build the overlay composition for one scene: transition, graphics, captions, progress bar. */
function sceneComp(sc, scene, scriptScene, voiceScene, slot, total) {
  const { width, height, fps } = sc.format.canvas;
  const vertical = height > width;
  const capCfg = sc.format.captions || {};
  const layers = [];
  if (scene.transition && scene.transition !== "cut") layers.push({ type: "transition", kind: scene.transition === "slide" ? "flash" : scene.transition });

  const graphics = [...scene.graphics];
  const hasHeadline = graphics.some((g) => ["title", "stat", "list", "quote"].includes(g.type));
  if (scriptScene?.on_screen_text && !hasHeadline) graphics.unshift({ type: "title", text: scriptScene.on_screen_text, sub: "", value: "", items: [] });

  // Several graphics in one scene share its time back to back.
  const usable = Math.max(0.8, slot.duration - 0.25);
  const each = usable / Math.max(1, graphics.length);
  graphics.forEach((g, i) => {
    layers.push({ ...g, start: round(0.12 + i * each, 3), end: g.type === "cta" || graphics.length === 1 ? null : round(0.12 + (i + 1) * each, 3) });
  });

  if (voiceScene?.words?.length) {
    layers.push({
      type: "captions",
      words: voiceScene.words.map((w) => ({ ...w, start: round(w.start + LEAD, 3), end: round(w.end + LEAD, 3) })),
      style: vertical ? sc.style.captions?.style || capCfg.style || "pop" : capCfg.style || "clean",
      y: capCfg.y ?? (vertical ? 0.62 : 0.86),
      maxWords: capCfg.max_words,
    });
  }
  if (vertical) layers.push({ type: "progress", from: slot.start / total, to: (slot.start + slot.duration) / total });

  return { width, height, fps, duration: slot.duration, style: { palette: sc.style.palette, fonts: sc.style.fonts, captions: sc.style.captions, motion: sc.style.motion }, safe: sc.format.safe_area, prominent: scene.mode === "motion", layers };
}

export async function motion(sc) {
  const board = sc.art.storyboard;
  const scriptById = new Map(sc.art.script.scenes.map((s) => [s.id, s]));
  const voiceById = new Map(sc.art.voice.scenes.map((s) => [s.id, s]));
  const slots = new Map(sc.art.timeline.scenes.map((s) => [s.id, s]));
  const previous = new Map((sc.art.motion?.scenes || []).map((s) => [s.id, s]));
  const total = sc.art.timeline.total;
  let done = 0;
  let framesCaptured = 0;
  let framesTotal = 0;

  // Rendering is CPU-bound in one browser; two pages at a time keeps it steady.
  const scenes = await mapLimit(board.scenes, 2, async (scene) => {
    const comp = sceneComp(sc, scene, scriptById.get(scene.id), voiceById.get(scene.id), slots.get(scene.id), total);
    const hash = hashOf(comp);
    const prev = previous.get(scene.id);
    let entry = prev;
    if (prev?.hash !== hash || !exists(sc.abs(prev.overlay))) {
      const out = sc.file("motion", `${scene.id}_${hash}.mov`);
      const r = await sc.call("motion", { comp, outFile: out });
      framesCaptured += r.captured || 0;
      framesTotal += r.frames || 0;
      entry = { id: scene.id, overlay: sc.rel(out), hash, layers: comp.layers.map((l) => l.type) };
    }
    sc.progress(++done / board.scenes.length, `${done}/${board.scenes.length} 장면 렌더링`);
    return entry;
  });

  sc.set("motion", { scenes });
  const graphics = board.scenes.reduce((n, s) => n + s.graphics.length, 0);
  return `자막 ${scenes.length}장면 · 그래픽 ${graphics}개${framesTotal ? ` · ${framesCaptured}/${framesTotal} 프레임 렌더` : ""}`;
}

export async function sound(sc) {
  const brief = sc.art.storyboard.music_brief;
  const total = sc.art.timeline.total;
  const route = resolve("music", sc.routeCtx())?.use;
  const prompt = `${brief.prompt}. Instrumental only, no vocals. Mood: ${brief.mood}. ${brief.bpm} BPM.${sc.revision ? ` Revision note: ${sc.revision.notes}` : ""}`;
  // Reuse the track unless the brief changed or the cut got noticeably longer.
  const hash = hashOf(prompt, route, Math.ceil(total / 10));
  const prev = sc.art.sound;
  if (prev?.hash === hash && exists(sc.abs(prev.music))) return `기존 음악 유지 (${brief.genre})`;

  const out = sc.file("sound", `music_${hash}.${route?.startsWith("local/") ? "wav" : "mp3"}`);
  const r = await sc.call("music", { prompt, duration: Math.ceil(total / 10) * 10 + 2, bpm: brief.bpm, mood: `${brief.mood} ${brief.genre}`, outFile: out });
  sc.set("sound", { music: sc.rel(out), hash, use: r.use, placeholder: r.local, brief });
  return `${brief.genre} · ${brief.bpm} BPM${r.local ? " (음악 AI 미연결 — 합성 대체음)" : ""}`;
}

export async function pack(sc) {
  const plan = sc.art.plan;
  const script = sc.art.script;
  const edits = sc.project.gates.plan?.options || {};
  const previous = sc.art.package;

  sc.progress(0.1, "제목·설명·태그를 쓰는 중");
  const data = await sc.agent("packager", "package", {
    instruction: sc.revision ? "패키징 재작업 요청이 있습니다. 지적된 부분을 고치세요." : "완성된 대본을 보고 제목·설명·태그와 썸네일 설계를 만드세요.",
    input: {
      plan: { working_title: plan.working_title, title_options: plan.title_options, viewer_promise: plan.viewer_promise, key_points: plan.key_points, hook: plan.hook, thumbnail_concept: plan.thumbnail_concept, cta: plan.cta },
      chosen_title: edits.title || "",
      script: { title: script.title, scenes: script.scenes.map((s) => ({ id: s.id, beat: s.beat, narration: s.narration })) },
      format: { id: sc.format.id, metadata: sc.format.metadata, platforms: sc.format.platforms },
      targets: sc.project.brief.targets,
      revision: sc.revision?.notes || "",
      previous_package: sc.revision && previous ? { title: previous.title, thumbnail: previous.thumbnail } : null,
    },
    schema: S.Package,
  });

  const limit = sc.format.metadata?.title_max;
  if (limit && data.title.length > limit) data.title = data.title.slice(0, limit);
  data.hashtags = data.hashtags.map((h) => h.replace(/^#/, "").replace(/\s+/g, "")).filter(Boolean).slice(0, sc.format.metadata?.hashtags || 5);

  sc.progress(0.5, "썸네일을 만드는 중");
  const { width, height } = sc.format.thumbnail || sc.format.canvas;
  const p = sc.style.image_prompt || {};
  const prompt = [p.prefix, data.thumbnail.image_prompt, "high contrast, single clear subject, empty space for headline text", p.suffix].filter(Boolean).join(" ");
  const bgHash = hashOf(prompt, resolve("image", sc.routeCtx())?.use, width, height);
  const bg = sc.file("package", `thumb_bg_${bgHash}.png`);
  let bgUse = previous?.thumbnailBgUse;
  if (!exists(bg)) {
    const r = await sc.call("image", { prompt, negative: p.negative, width, height, style: sc.style, seedKey: `${sc.project.id}:thumb`, outFile: bg });
    bgUse = r.use;
  }
  const out = sc.file("package", `thumbnail_${hashOf(bgHash, data.thumbnail)}.jpg`);
  await renderStill(
    {
      width, height, duration: 0,
      style: { palette: sc.style.palette, fonts: sc.style.fonts, motion: sc.style.motion },
      background: { image: fileUrl(bg), dim: width > height ? "linear-gradient(90deg, rgba(0,0,0,.62), rgba(0,0,0,.05) 70%)" : "linear-gradient(180deg, rgba(0,0,0,.6), rgba(0,0,0,0) 55%)" },
      layers: [{ type: "thumb", text: data.thumbnail.headline, sub: data.thumbnail.sub, layout: data.thumbnail.layout }],
    },
    out,
  );

  sc.set("package", { ...data, thumbnailFile: sc.rel(out), thumbnailBgUse: bgUse });
  return `「${data.title}」 · 썸네일 "${data.thumbnail.headline}"`;
}
