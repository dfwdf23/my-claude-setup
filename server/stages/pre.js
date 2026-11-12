import { config } from "../config.js";
import { allProjects } from "../store.js";
import { benchmarks } from "../providers/youtube.js";
import { resolve } from "../providers/registry.js";
import { hasSecret } from "../secrets.js";
import { buildSystem } from "../agents/runtime.js";
import { relevantLessons } from "../learning/playbook.js";
import { getAgent } from "../config.js";
import * as S from "../schemas.js";
import { clamp } from "../util.js";

/** 기획 · 프리프로덕션 단계: 리서치 → 기획안 → 대본 → 스토리보드 */

const pad = (n) => String(n).padStart(2, "0");

export async function research(sc) {
  const { brief } = sc.project;
  const query = brief.topic || sc.channel.autopilot?.topics_hint || sc.channel.niche || "";

  // 1) What is already getting 100K+ views on this subject (YouTube Data API, optional).
  let bench = [];
  if (hasSecret("YOUTUBE_API_KEY") && config.studio.mode !== "simulation") {
    bench = await benchmarks(query, { language: sc.channel.language || "ko" }).catch((err) => {
      sc.log(`벤치마크 조회를 건너뜁니다 — ${err.message}`, "warn");
      return [];
    });
  }

  // 2) Live web research, when a search-capable model is connected.
  let notes = { text: "", sources: [] };
  if (!resolve("search")?.isLocal) {
    sc.progress(0.3, "웹에서 최근 반응을 조사하는 중");
    const agent = getAgent("trend-scout");
    notes = await sc.call("search", {
      llmModel: agent.model,
      effort: agent.effort || "medium",
      system: buildSystem(agent, { ...sc.agentCtx(), lessons: relevantLessons({ agentId: agent.id, formatId: sc.format.id, simulated: false }) }),
      user: `다음 조건으로 영상 각도를 찾기 위한 조사를 하세요. 최근 반응이 좋았던 영상·기사·커뮤니티 글을 검색하고, 확인한 사실과 출처를 정리한 조사 노트를 작성하세요.\n\n주제: ${brief.topic || "(채널 주제 안에서 직접 선정)"}\n포맷: ${sc.format.name}\n요청 메모: ${brief.notes || "-"}`,
    });
  }

  sc.progress(0.7, "각도 후보를 정리하는 중");
  const data = await sc.agent("trend-scout", "research", {
    instruction: "아래 조사 자료를 바탕으로 영상 각도 후보를 정리하세요. 주제가 비어 있으면 채널에 맞는 주제를 직접 고릅니다.",
    input: {
      brief: { id: sc.project.id, topic: brief.topic, notes: brief.notes },
      benchmark_videos: bench,
      research_notes: notes.text,
      found_sources: notes.sources,
    },
    schema: S.Research,
  });

  sc.set("research", { ...data, benchmarks: bench });
  return `각도 후보 ${data.angles.length}개 · 벤치마크 ${bench.length}편 · 출처 ${data.sources.length}건`;
}

export async function plan(sc) {
  const { brief } = sc.project;
  const gate = sc.project.gates.plan;
  const styles = [...config.styles.values()].map((s) => ({ id: s.id, name: s.name, summary: s.summary, best_for: s.best_for || [] }));
  const recentStyles = allProjects().filter((p) => p.id !== sc.project.id && p.styleId).slice(0, 5).map((p) => p.styleId);
  const research = sc.art.research;

  const data = await sc.agent("producer", "plan", {
    instruction: gate?.feedback
      ? "결재자가 기획안 수정을 요청했습니다. 피드백을 반영해 기획안을 다시 쓰세요."
      : "리서치를 바탕으로 이 영상의 기획안을 쓰세요. 결재자는 이 기획안만 보고 제작 여부를 결정합니다.",
    input: {
      brief: { topic: brief.topic, notes: brief.notes, style: brief.style, targets: brief.targets },
      research: { summary: research.summary, angles: research.angles, benchmark_patterns: research.benchmark_patterns },
      styles,
      recently_used_styles: recentStyles,
      format: { id: sc.format.id, duration: sc.format.duration },
      feedback: gate?.feedback || "",
      previous_plan: gate?.feedback ? sc.art.plan : null,
    },
    schema: S.Plan,
  });

  // The owner's explicit style choice always wins; an unknown id falls back to the first style.
  if (brief.style && brief.style !== "auto" && config.styles.has(brief.style)) data.style_id = brief.style;
  if (!config.styles.has(data.style_id)) data.style_id = styles[0].id;
  if (!sc.simulated) data.duration_sec = clamp(Math.round(data.duration_sec), sc.format.duration.min, sc.format.duration.max);

  sc.project.title = data.working_title;
  sc.project.styleId = data.style_id;
  sc.set("plan", data);
  return `「${data.working_title}」 · ${config.styles.get(data.style_id).name} · ${data.duration_sec}초`;
}

function tidyScript(script) {
  script.scenes = script.scenes
    .filter((s) => s.narration.trim() || s.on_screen_text.trim())
    .map((s, i) => ({
      ...s,
      id: `s${pad(i + 1)}`,
      narration: s.narration.trim(),
      emphasis_words: s.emphasis_words.filter((w) => w && s.narration.includes(w)).slice(0, 2),
    }));
  if (!script.scenes.length) throw new Error("대본에 장면이 없습니다");
  return script;
}

export async function script(sc) {
  const plan = sc.art.plan;
  const edits = sc.project.gates.plan?.options || {};
  const input = {
    plan,
    owner_notes: edits.note || "",
    chosen_title: edits.title || "",
    research: { angles: sc.art.research.angles, benchmark_patterns: sc.art.research.benchmark_patterns },
    format: { id: sc.format.id, duration: sc.format.duration, shot: sc.format.shot },
    revision: sc.revision?.notes || "",
    previous_script: sc.revision ? sc.art.script : null,
  };

  sc.progress(0.2, "초안을 쓰는 중");
  let data = await sc.agent("scriptwriter", "script", {
    instruction: sc.revision
      ? "품질 검수(또는 결재자)가 대본 재작업을 요청했습니다. 지적된 부분만 고치고 나머지는 유지하세요."
      : "승인된 기획안으로 장면 단위 대본을 쓰세요.",
    input,
    schema: S.Script,
    maxTokens: 48000,
  });

  if ((config.studio.quality?.script_passes ?? 2) >= 2 && !sc.revision) {
    sc.progress(0.65, "스크립트 닥터가 퇴고하는 중");
    data = await sc.agent("script-doctor", "script_doctor", {
      instruction: "이 초안을 처음 보는 시청자의 눈으로 읽고, 넘길 만한 지점을 고친 최종 대본을 내놓으세요.",
      input: { plan: { viewer_promise: plan.viewer_promise, hook: plan.hook, duration_sec: plan.duration_sec }, draft: data, format: { id: sc.format.id, shot: sc.format.shot } },
      schema: S.Script,
      maxTokens: 48000,
    });
  }

  tidyScript(data);
  if (sc.art.script) sc.art.prevScript = sc.art.script; // lets the storyboard keep scenes whose text did not change
  sc.set("script", data);
  const chars = data.scenes.reduce((n, s) => n + s.narration.length, 0);
  return `${data.scenes.length}장면 · ${chars}자`;
}

export async function storyboard(sc) {
  const script = sc.art.script;
  const previous = sc.art.storyboard;
  const data = await sc.agent("art-director", "storyboard", {
    instruction: sc.revision
      ? "품질 검수(또는 결재자)가 화면 재작업을 요청했습니다. 지적된 장면만 고치고, 나머지 장면은 이전 값을 글자 그대로 유지하세요."
      : "이 대본의 비주얼 바이블과 장면별 스토리보드를 만드세요. 장면 id는 대본과 똑같이 맞춥니다.",
    input: {
      plan: { working_title: sc.art.plan.working_title, hook: sc.art.plan.hook, viewer_promise: sc.art.plan.viewer_promise },
      script: { scenes: script.scenes },
      style: sc.style,
      format: { id: sc.format.id, canvas: sc.format.canvas, safe_area: sc.format.safe_area },
      mix: sc.mix,
      revision: sc.revision?.notes || "",
      previous_storyboard: sc.revision ? previous : null,
    },
    schema: S.Storyboard,
    maxTokens: 48000,
  });

  // Line the board up with the script: one entry per script scene, in script order.
  const byId = new Map(data.scenes.map((s) => [s.id, s]));
  const keep = sc.revision?.sceneIds?.length ? new Set(sc.revision.sceneIds) : null;
  const prevById = new Map((previous?.scenes || []).map((s) => [s.id, s]));

  // After a script rewrite, scenes whose text did not change keep their previous design —
  // identical prompts mean the already generated images and clips are reused.
  const sig = (s) => `${s.narration}|${s.visual_idea}|${s.on_screen_text}`;
  const unchanged = new Map();
  if (previous && sc.art.prevScript && !sc.revision) {
    const prevBySig = new Map(sc.art.prevScript.scenes.map((s) => [sig(s), prevById.get(s.id)]));
    for (const s of script.scenes) {
      const design = prevBySig.get(sig(s));
      if (design) unchanged.set(s.id, { ...design, id: s.id });
    }
  }

  data.scenes = script.scenes.map((s) => {
    if (unchanged.has(s.id)) return unchanged.get(s.id);
    // On a targeted revision, scenes that were not flagged keep their exact previous design (and their generated media).
    if (keep && !keep.has(s.id) && prevById.has(s.id)) return prevById.get(s.id);
    return byId.get(s.id) || { id: s.id, mode: "image", shot: "미디엄 샷", image_prompt: s.visual_idea, motion_prompt: "slow camera push", camera: "push_in", graphics: [], transition: "cut" };
  });
  if ((keep || unchanged.size) && previous) data.visual_bible = previous.visual_bible;

  sc.set("storyboard", data);
  const count = (m) => data.scenes.filter((s) => s.mode === m).length;
  return `영상 ${count("video")} · 이미지 ${count("image")} · 모션 ${count("motion")}장면`;
}
