import { config } from "../config.js";
import { bus } from "../bus.js";
import { touch } from "../store.js";
import { runAgent, brainIsSimulated } from "../agents/runtime.js";
import { call, providers } from "../providers/registry.js";
import { contextFor, spender } from "../context.js";
import { addLessons, allLessons, setLessonStatus } from "./playbook.js";
import { Retro } from "../schemas.js";

/**
 * The learning loop. Three things teach the team:
 *   feedback    the owner's comments at the two approval gates
 *   production  what QA had to send back during this production
 *   metrics     how the published video actually performed vs. what was predicted
 * The analyst turns each into scoped, actionable lessons in the playbook.
 */

const INSTRUCTION = {
  feedback: "채널 주인이 결재 단계에서 남긴 피드백입니다. 이 사람의 취향과 기준에 대해 팀이 앞으로 지켜야 할 교훈을 뽑으세요. 이번 영상에만 해당하는 지적은 교훈으로 만들지 않습니다.",
  production: "이번 제작 과정에서 품질 검수가 돌려보낸 내용입니다. 같은 재작업이 다시 생기지 않도록 담당 AI별 교훈을 뽑으세요.",
  metrics: "업로드된 영상의 실제 성과입니다. 기획 단계의 예측과 비교하고, 무엇이 통했고 무엇이 안 통했는지에서 교훈을 뽑으세요.",
};

export async function learn(project, trigger, extra = {}) {
  if (config.studio.learning?.enabled === false) return [];
  const art = project.artifacts;
  const qaRounds = art.qa?.rounds || [];
  const sentBack = qaRounds.flatMap((r) => (r.passed ? [] : r.report.issues));

  // Nothing went wrong and nobody said anything → nothing to learn from this trigger.
  if (trigger === "production" && !sentBack.length) return [];
  if (trigger === "feedback" && !extra.feedback?.comment?.trim()) return [];

  const ctx = { ...contextFor(project), spend: spender(project) };
  const simulated = brainIsSimulated();
  const { data } = await runAgent("analyst", "retro", {
    instruction: INSTRUCTION[trigger],
    input: {
      trigger,
      format: project.brief.format,
      style: project.styleId || "",
      video: { title: project.title, hook: art.plan?.hook, predicted: art.plan?.predicted },
      feedback: extra.feedback || null,
      qa: trigger === "feedback" ? null : { final_score: art.qa?.score, rounds: qaRounds.length, issues: sentBack },
      metrics: extra.metrics || null,
      existing_lessons: allLessons().filter((l) => l.status === "active").slice(0, 40).map((l) => ({ id: l.id, agent: l.agent, text: l.text })),
    },
    schema: Retro,
    ctx,
  });

  const source = { feedback: "feedback", production: "qa", metrics: "analytics" }[trigger];
  const added = addLessons(data.lessons, { source, origin: simulated || extra.metrics?.simulated ? "sim" : "real", projectId: project.id });
  for (const id of data.retire) {
    try {
      setLessonStatus(id, "retired", `성과 분석가가 폐기: ${data.summary.slice(0, 80)}`);
    } catch {
      /* unknown id from the model — ignore */
    }
  }
  if (added.length) bus.log({ kind: "learning", projectId: project.id, agentId: "analyst", text: `학습 노트 ${added.length}건 추가 — ${added[0].text.slice(0, 60)}${added.length > 1 ? " 외" : ""}` });
  return added;
}

/** Pull performance numbers for a published project at one checkpoint and learn from them. */
export async function collectMetrics(project, hours) {
  const art = project.artifacts;
  const an = art.analyze;
  const byPlatform = {};
  for (const r of art.publish?.results || []) {
    const cap = r.platform.startsWith("youtube") ? "analytics.youtube" : "analytics.instagram";
    try {
      const args = { videoId: r.id, publishedAt: r.at, qaScore: art.qa?.score ?? 80, hoursSincePublish: hours, seed: project.id + r.platform };
      // A dry-run upload has no real video to measure — use simulated numbers and say so.
      const m = r.dryRun ? await providers.local.capabilities[cap](args) : await call(cap, args, { projectId: project.id });
      const { provider, model, use, local, costUsd, ...metrics } = m;
      byPlatform[r.platform] = metrics;
    } catch (err) {
      byPlatform[r.platform] = { error: String(err.message).slice(0, 200) };
    }
  }
  an.checkpoints.push({ hours, at: Date.now(), platforms: byPlatform });
  an.pending = an.pending.filter((h) => h !== hours);
  touch(project);

  const best = Object.values(byPlatform).filter((m) => !m.error).sort((a, b) => (b.views || 0) - (a.views || 0))[0];
  if (best) {
    const when = hours >= 24 ? `${hours / 24}일` : `${hours}시간`;
    bus.log({ kind: "metrics", projectId: project.id, agentId: "analyst", text: `「${project.title}」 ${when}차 성과: 조회수 ${best.views?.toLocaleString() ?? "-"}${best.avgViewPct != null ? `, 평균 시청 ${best.avgViewPct}%` : ""}${best.simulated ? " (시뮬레이션)" : ""}` });
    // Learn once the numbers have settled a little: from 72h on, or at the last checkpoint.
    if (hours >= 72 || !an.pending.length) {
      await learn(project, "metrics", { metrics: { hours, ...best } }).catch(() => {});
    }
  }
  touch(project);
}
