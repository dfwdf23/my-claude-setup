import { config, getAgent } from "../config.js";
import { bus } from "../bus.js";
import { call, resolve } from "../providers/registry.js";
import { relevantLessons } from "../learning/playbook.js";

/**
 * Runs one AI team member on one task.
 *
 * The instructions an agent receives are assembled fresh on every call from
 * the files in studio/ — its role (agents/*.md), the channel, the format, the
 * art style — plus the lessons the team has learned so far. Edit any of those
 * and the very next task uses the new version.
 */

function channelBlock(ch) {
  if (!ch) return "";
  return [
    `# 채널`,
    `- 이름: ${ch.name}`,
    `- 주제: ${ch.niche || "-"}`,
    `- 시청자: ${ch.audience || "-"}`,
    `- 말투: ${ch.tone || "-"}`,
    ch.avoid?.length ? `- 피할 것: ${ch.avoid.join(" / ")}` : null,
    ch.cta ? `- 기본 CTA: ${ch.cta}` : null,
    `- 언어: ${ch.language || config.studio.language || "ko"}`,
  ].filter(Boolean).join("\n");
}

function formatBlock(f) {
  if (!f) return "";
  return [
    `# 포맷: ${f.name}`,
    `- 화면: ${f.canvas.width}×${f.canvas.height} (${f.canvas.width > f.canvas.height ? "가로" : "세로"})`,
    `- 길이: ${f.duration.min}~${f.duration.max}초 (목표 ${f.duration.target}초)`,
    `- 컷 길이: 평균 ${f.shot.target_sec}초, 최대 ${f.shot.max_sec}초`,
    `- 제목 최대 ${f.metadata?.title_max ?? "-"}자, 해시태그 ${f.metadata?.hashtags ?? "-"}개`,
    f.guide ? `- 제작 지침:\n${f.guide.trim().split("\n").map((l) => "  " + l.trim()).join("\n")}` : null,
  ].filter(Boolean).join("\n");
}

function styleBlock(s, mix) {
  if (!s) return "";
  return [
    `# 그림체: ${s.name} (${s.id})`,
    `- 설명: ${s.summary}`,
    `- 이미지 프롬프트 접두/접미: "${s.image_prompt?.prefix || ""}" … "${s.image_prompt?.suffix || ""}"`,
    `- 영상 움직임: ${s.video_prompt || "-"}`,
    `- 목소리: ${s.voice?.persona || "-"}`,
    `- 음악: ${s.music?.mood || "-"} / ${s.music?.genre || "-"} / ${s.music?.bpm || "-"} BPM`,
    mix ? `- 장면 제작 비율(참고): 영상 생성 ${Math.round(mix.video * 100)}% · 이미지 ${Math.round(mix.image * 100)}% · 모션그래픽 ${Math.round(mix.motion * 100)}%` : null,
  ].filter(Boolean).join("\n");
}

function lessonsBlock(lessons) {
  if (!lessons.length) return "";
  return `# 팀 학습 노트 (지난 작업에서 배운 것 — 반드시 반영)\n${lessons.map((l) => `- ${l.text}`).join("\n")}`;
}

export function buildSystem(agent, { channel, format, style, mix, lessons = [] }) {
  return [
    agent.prompt,
    channelBlock(channel),
    formatBlock(format),
    styleBlock(style, mix),
    lessonsBlock(lessons),
    `# 출력\n지정된 JSON 형식으로만 답합니다. 사람이 읽는 글은 모두 채널 언어로 쓰고, 생성 AI에 들어갈 프롬프트 필드만 영어로 씁니다.`,
  ].filter(Boolean).join("\n\n");
}

/**
 * @param {string} agentId   studio/agents/<id>.md
 * @param {string} task      task name (the simulator dispatches on this)
 * @param {object} opts      { instruction, input, schema, images, ctx, maxTokens }
 *   ctx: { project, channel, format, style, mix, spend(cost), log(text) }
 */
export async function runAgent(agentId, task, { instruction, input, schema, images, ctx, maxTokens }) {
  const agent = getAgent(agentId);
  const routeCtx = { projectId: ctx.project?.id, styleId: ctx.style?.id, formatId: ctx.format?.id };
  const simulated = Boolean(resolve("llm", routeCtx)?.isLocal);
  const lessons = relevantLessons({ agentId, formatId: ctx.format?.id, styleId: ctx.style?.id, simulated });
  const system = buildSystem(agent, { ...ctx, lessons });
  const user = `${instruction}\n\n<입력>\n${JSON.stringify(input, null, 1)}\n</입력>`;

  bus.send("agent", { agentId, projectId: ctx.project?.id, state: "working", task });
  try {
    const result = await call(
      "llm",
      { system, user, schema, images, effort: agent.effort || "high", maxTokens: maxTokens || 32000, task, input, agentModel: agent.model },
      routeCtx,
    );
    ctx.spend?.(result.costUsd || 0, result.use);
    return { data: result.data, simulated: result.local, use: result.use, lessonsUsed: lessons.map((l) => l.id) };
  } finally {
    bus.send("agent", { agentId, projectId: ctx.project?.id, state: "idle", task });
  }
}

/** Is the brain running on the offline simulator right now? */
export const brainIsSimulated = () => Boolean(resolve("llm")?.isLocal);
