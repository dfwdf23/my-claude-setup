// 프로젝트 — every video and the full paper trail of how it was made.
import { html, useState, Icon, api, go, useAction, duration, compact, money, timeAgo, dateTime, clock, PLATFORM, safeUrl } from "/lib.js";
import { StatusBadge, FormatChip, StyleChip, Track, currentStage, Seg, Poster, Empty, STAGE_WORD, LineChart } from "/ui.js";
import { useProject, QaReport } from "/views/approvals.js";

export function Projects({ state, onNew }) {
  const [filter, setFilter] = useState("all");
  const groups = {
    all: state.projects,
    live: state.projects.filter((p) => ["active", "waiting", "paused", "failed"].includes(p.status)),
    done: state.projects.filter((p) => p.status === "done"),
    canceled: state.projects.filter((p) => p.status === "canceled"),
  };
  const list = groups[filter];
  return html`<div class="page">
    <div class="page-head">
      <div><h1>프로젝트</h1><p>지금까지 접수된 모든 영상입니다.</p></div>
      <button class="btn primary" onClick=${onNew}><${Icon} name="plus" />새 영상 만들기</button>
    </div>
    <${Seg} value=${filter} onChange=${setFilter} options=${[{ value: "all", label: "전체", count: groups.all.length }, { value: "live", label: "진행 중", count: groups.live.length }, { value: "done", label: "완료", count: groups.done.length }, { value: "canceled", label: "중단", count: groups.canceled.length }]} />
    <div class="card">
      ${list.length
        ? list.map((p) => {
            const format = state.formats.find((f) => f.id === p.brief.format);
            const style = state.styles.find((s) => s.id === p.styleId);
            const cur = currentStage(p, state.pipeline);
            return html`<a class="list-row" href=${`#/projects/${p.id}`}>
              <${Poster} project=${p} format=${format} />
              <div class="grow col gap-s">
                <div style="font-weight:700" class="clamp2">${p.title}</div>
                <div class="row gap-s wrap"><${StatusBadge} project=${p} /><${FormatChip} format=${format} /><${StyleChip} style=${style} />${p.brief.auto ? html`<span class="chip line">오토파일럿</span>` : null}</div>
                <div class="muted small clip">${cur.text}${p.qa ? ` · 품질 ${p.qa.score}점` : ""}${p.cost ? ` · ${money(p.cost)}` : ""} · ${timeAgo(p.createdAt)}</div>
              </div>
              <div style="flex:none" class="hide-s"><${Track} project=${p} pipeline=${state.pipeline} /></div>
            </a>`;
          })
        : html`<${Empty} icon="film" title="해당하는 영상이 없습니다">새 영상을 만들면 여기에 표시됩니다.<//>`}
    </div>
  </div>`;
}

const STAGE_ICON = { pending: "clock", running: "loader", done: "check", waiting: "inbox", failed: "alert" };

function StageMap({ project, state, onRedo, onRetry }) {
  const { pipeline } = state;
  return html`<div class="phases">
    ${pipeline.phases.map((phase) => {
      const defs = pipeline.stages.filter((s) => s.phase === phase);
      if (!defs.length) return null;
      return html`<div class="phase">
        <div class="phase-name">${phase}</div>
        ${defs.map((def) => {
          const st = project.stages[def.id];
          const full = project.stagesFull[def.id] || {};
          const agent = state.agents.find((a) => a.id === def.agent);
          const took = st.startedAt && st.finishedAt ? Math.max(1, Math.round((st.finishedAt - st.startedAt) / 1000)) : null;
          const redoable = !def.gate && st.status === "done" && ["script", "storyboard", "voice", "visuals", "motion", "sound", "package", "edit"].includes(def.id) && !["canceled"].includes(project.status);
          return html`<div class=${`stage ${st.status}`}>
            <div class="row gap-s">
              <span class="stage-ico"><${Icon} name=${def.gate ? (st.status === "done" ? "check" : "flag") : STAGE_ICON[st.status]} size="s" /></span>
              <span style="font-weight:700" class="grow clip">${def.title}</span>
              ${took ? html`<span class="muted tiny num">${took >= 60 ? `${Math.round(took / 60)}분` : `${took}초`}</span>` : null}
            </div>
            <div class="tiny muted clip">${def.gate ? "사람 결재" : agent?.name || def.agent}${st.simulated ? " · 시뮬레이션" : ""}</div>
            ${st.status === "running" ? html`<div class="bar"><i style=${`width:${Math.round(st.progress * 100)}%`}></i></div><div class="tiny clip">${st.label || "작업 중"}</div>` : null}
            ${st.summary && st.status !== "running" ? html`<div class="tiny ink2 clamp2">${st.summary}</div>` : null}
            ${st.revising ? html`<div class="tiny" style="color:var(--warn-ink)">재작업 지시 받음</div>` : null}
            ${full.lastRevision && st.status === "done" ? html`<div class="tiny muted clamp2" title=${full.lastRevision.notes}>↺ ${full.lastRevision.by === "qa" ? "QA" : "결재자"} 지시 반영</div>` : null}
            ${st.error ? html`<div class="tiny" style="color:var(--critical-ink)">${st.error}</div>` : null}
            ${st.status === "failed" ? html`<button class="btn sm" onClick=${() => onRetry(def.id)}><${Icon} name="refresh" size="s" />다시 시도</button>` : null}
            ${st.status === "waiting" ? html`<a class="btn primary sm" href=${`#/approvals/${project.id}`}>결재하기</a>` : null}
            ${redoable ? html`<button class="btn ghost sm" style="align-self:flex-start;margin:-2px -8px -4px" onClick=${() => onRedo(def)}><${Icon} name="refresh" size="s" />다시 만들기</button>` : null}
          </div>`;
        })}
      </div>`;
    })}
  </div>`;
}

const MODE = { video: "영상 생성", image: "이미지+무빙", motion: "모션그래픽" };

function Scenes({ project, format }) {
  const a = project.artifacts;
  const tall = format.canvas.height > format.canvas.width;
  const board = new Map((a.storyboard?.scenes || []).map((s) => [s.id, s]));
  const vis = new Map((a.visuals?.scenes || []).map((s) => [s.id, s]));
  const slot = new Map((a.timeline?.scenes || []).map((s) => [s.id, s]));
  return html`<div class="scroll-x"><table class="t">
    <thead><tr><th>#</th><th>화면</th><th style="min-width:240px">내레이션</th><th style="min-width:200px">연출</th><th>길이</th></tr></thead>
    <tbody>
      ${a.script.scenes.map((s) => {
        const b = board.get(s.id);
        const v = vis.get(s.id);
        return html`<tr>
          <td class="muted num">${s.id.replace("s", "")}</td>
          <td>${v?.imageUrl ? html`<a href=${v.clipUrl || v.imageUrl} target="_blank"><div class=${`scene-img ${tall ? "" : "h"}`} style=${`background-image:url("${v.imageUrl}")`}></div></a>` : html`<div class=${`scene-img ${tall ? "" : "h"}`}></div>`}</td>
          <td><div class="row gap-s" style="margin-bottom:4px"><span class="chip">${s.beat}</span></div>${s.narration}${s.on_screen_text ? html`<div class="muted small" style="margin-top:4px">화면 문구: ${s.on_screen_text}</div>` : null}</td>
          <td class="small">${b ? html`<div class="row gap-s wrap" style="margin-bottom:4px"><span class="chip line">${MODE[b.mode]}</span><span class="chip line">${b.shot}</span>${b.graphics.map((g) => html`<span class="chip">${g.type}</span>`)}</div><div class="muted clamp2" title=${b.image_prompt}>${b.image_prompt}</div>` : html`<span class="muted">${s.visual_idea}</span>`}</td>
          <td class="num nowrap">${slot.get(s.id) ? `${slot.get(s.id).duration.toFixed(1)}초` : "-"}</td>
        </tr>`;
      })}
    </tbody>
  </table></div>`;
}

function Metrics({ analyze }) {
  if (!analyze?.checkpoints?.length) return html`<div class="muted small">업로드 후 ${analyze?.pending?.map((h) => (h >= 24 ? `${h / 24}일` : `${h}시간`)).join(" · ") || "-"} 시점에 성과를 자동으로 확인합니다.</div>`;
  return html`<div class="scroll-x"><table class="t">
    <thead><tr><th>시점</th><th>플랫폼</th><th>조회수</th><th>평균 시청</th><th>좋아요</th><th>댓글</th><th>공유</th></tr></thead>
    <tbody>
      ${analyze.checkpoints.flatMap((c) =>
        Object.entries(c.platforms).map(
          ([platform, m]) => html`<tr>
            <td class="nowrap">${c.hours >= 24 ? `${c.hours / 24}일차` : `${c.hours}시간`}</td>
            <td>${PLATFORM[platform] || platform}${m.simulated ? html` <span class="chip line">시뮬레이션</span>` : null}</td>
            ${m.error ? html`<td colspan="5" class="muted">${m.error}</td>` : html`<td class="num">${compact(m.views)}</td><td class="num">${m.avgViewPct != null ? `${m.avgViewPct}%` : "-"}</td><td class="num">${compact(m.likes)}</td><td class="num">${compact(m.comments)}</td><td class="num">${m.shares != null ? compact(m.shares) : "-"}</td>`}
          </tr>`,
        ),
      )}
    </tbody>
  </table></div>`;
}

export function ProjectDetail({ id, state }) {
  const [project, error] = useProject(id);
  const [busy, run] = useAction();
  const [redo, setRedo] = useState(null);
  const [notes, setNotes] = useState("");
  if (error) return html`<div class="page"><div class="card"><${Empty} icon="alert" title="프로젝트를 찾을 수 없습니다"><a class="btn" href="#/projects">목록으로</a><//></div></div>`;
  if (!project) return html`<div class="page"><div class="empty">불러오는 중…</div></div>`;

  const a = project.artifacts;
  const format = state.formats.find((f) => f.id === project.brief.format);
  const style = state.styles.find((s) => s.id === project.styleId);
  const tall = format.canvas.height > format.canvas.width;
  const act = (body, ok) => run(() => api(`/api/projects/${id}/actions`, { method: "POST", body }), ok);
  const pendingGate = Object.entries(project.gates).find(([, g]) => g.status === "pending")?.[0];
  const live = !["done", "canceled"].includes(project.status);

  return html`<div class="page">
    <div class="col gap-s">
      <a class="row gap-s muted small" href="#/projects"><${Icon} name="back" size="s" />프로젝트</a>
      <div class="page-head">
        <div class="col gap-s" style="min-width:0">
          <h1>${project.title}</h1>
          <div class="row gap-s wrap"><${StatusBadge} project=${project} /><${FormatChip} format=${format} /><${StyleChip} style=${style} />
            <span class="chip line num">추정 비용 ${money(project.cost)} / ${money(project.budget)}</span><span class="muted small">${dateTime(project.createdAt)} 접수</span></div>
        </div>
        <div class="row gap-s wrap">
          ${pendingGate ? html`<a class="btn primary" href=${`#/approvals/${id}`}><${Icon} name="inbox" size="s" />결재하기</a>` : null}
          ${live ? html`<button class="btn danger" disabled=${busy} onClick=${() => confirm("이 영상의 제작을 중단할까요?") && act({ action: "cancel" }, "중단했습니다")}>중단</button>` : html`<button class="btn danger" disabled=${busy} onClick=${() => confirm("이 프로젝트와 모든 파일을 삭제할까요? 되돌릴 수 없습니다.") && act({ action: "delete" }).then(() => go("projects"))}><${Icon} name="trash" size="s" />삭제</button>`}
        </div>
      </div>
    </div>

    ${project.paused ? html`<div class="banner warn"><${Icon} name="pause" /><div class="grow"><b>일시정지</b> — ${project.paused.message}</div><button class="btn sm" disabled=${busy} onClick=${() => act({ action: "resume", addBudget: 10 }, "예산을 $10 늘리고 재개했습니다")}>예산 +$10 후 계속</button></div>` : null}
    ${project.endedReason ? html`<div class="banner info"><${Icon} name="flag" /><div>${project.endedReason}</div></div>` : null}
    ${project.placeholders.length && a.edit ? html`<div class="banner warn"><${Icon} name="alert" /><div>대체 엔진으로 만든 구간: <b>${project.placeholders.join(", ")}</b> — 해당 AI를 연결한 뒤 그 단계를 ‘다시 만들기’ 하면 실제 결과물로 교체됩니다.</div></div>` : null}

    <div class="card pad col">
      <div class="section-title"><h2>제작 단계</h2><span class="muted small">${currentStage(project, state.pipeline).text}</span></div>
      <${StageMap} project=${project} state=${state} onRetry=${(stageId) => act({ action: "retry", stageId }, "다시 시도합니다")} onRedo=${(def) => (setNotes(""), setRedo(def))} />
    </div>

    ${redo
      ? html`<div class="card pad col" style="box-shadow:0 0 0 3px var(--accent-wash)">
          <h3>‘${redo.title}’ 다시 만들기</h3>
          <div class="hint">이 단계와 그 뒤 단계가 다시 진행됩니다. 바뀌지 않은 장면의 이미지·음성은 재사용합니다.</div>
          <textarea class="textarea" rows="2" placeholder="어떻게 바꿀지 적어 주세요 (선택)" value=${notes} onInput=${(e) => setNotes(e.target.value)}></textarea>
          <div class="row" style="justify-content:flex-end"><button class="btn" onClick=${() => setRedo(null)}>취소</button><button class="btn primary" disabled=${busy} onClick=${() => act({ action: "redo", stageId: redo.id, notes }, "다시 만들기를 시작했습니다").then(() => setRedo(null))}>다시 만들기</button></div>
        </div>`
      : null}

    ${a.edit
      ? html`<div class="split" style=${tall ? "grid-template-columns:minmax(240px,320px) minmax(0,1fr)" : ""}>
          <div class=${`player ${tall ? "v" : ""}`}><video src=${a.edit.url} poster=${a.edit.posterUrl} controls playsinline preload="metadata"></video></div>
          <div class="col gap-l">
            ${a.package
              ? html`<div class="card pad col">
                  <div class="row top gap-l">
                    <img src=${a.package.thumbnailUrl} alt="썸네일" style=${`width:${tall ? 84 : 180}px;border-radius:10px;flex:none`} />
                    <div class="col gap-s grow"><div style="font-weight:700;font-size:16px">${a.publish?.title || a.package.title}</div><div class="ink2 small pre clamp2">${a.package.description}</div><div class="row gap-s wrap">${a.package.hashtags.map((h) => html`<span class="chip">#${h}</span>`)}</div></div>
                  </div>
                  ${a.publish ? html`<hr class="divider" />${a.publish.results.map((r) => html`<div class="row small"><span class="badge done"><${Icon} name="check" size="s" />${PLATFORM[r.platform]}</span>${r.dryRun ? html`<span class="muted grow clip">업로드 패키지 저장됨 · <code class="k">${r.outboxDir?.split("/data/")[1] ? "data/" + r.outboxDir.split("/data/")[1] : "data/outbox"}</code></span>` : html`<a class="grow clip" href=${safeUrl(r.url)} target="_blank" rel="noreferrer" style="text-decoration:underline">${r.url || r.id}</a>`}<span class="muted tiny">${clock(r.at)}</span></div>`)}` : null}
                </div>`
              : null}
            ${a.qa ? html`<div class="card"><div class="card-head"><h2>품질 검수</h2><span class="muted small">${a.qa.rounds.length}차 검수</span></div><div class="card-body"><${QaReport} qa=${a.qa} /></div></div>` : null}
          </div>
        </div>`
      : null}

    ${a.analyze ? html`<div class="card"><div class="card-head"><h2>성과</h2>${a.analyze.simulated ? html`<span class="chip line">시뮬레이션 수치</span>` : null}</div><div class="card-body"><${Metrics} analyze=${a.analyze} /></div></div>` : null}

    ${a.plan
      ? html`<div class="card"><div class="card-head"><h2>기획안</h2>${project.gates.plan?.status === "approved" ? html`<span class="badge done"><${Icon} name="check" size="s" />승인됨</span>` : null}</div>
          <div class="card-body col">
            <div style="font-size:16px;font-weight:700">${a.plan.logline}</div>
            <dl class="kv"><dt>훅</dt><dd>“${a.plan.hook.line}”</dd><dt>시청자 약속</dt><dd>${a.plan.viewer_promise}</dd><dt>예상 성과</dt><dd class="num">시청 지속 ${a.plan.predicted.retention_pct}%${a.plan.predicted.ctr_pct ? ` · 클릭률 ${a.plan.predicted.ctr_pct}%` : ""}</dd></dl>
          </div></div>`
      : null}

    ${a.script ? html`<div class="card"><div class="card-head"><h2>대본 · 스토리보드</h2><span class="muted small num">${a.script.scenes.length}장면${a.timeline ? ` · ${duration(a.timeline.total)}` : ""}</span></div><${Scenes} project=${project} format=${format} />${a.sound?.url ? html`<div class="card-body row wrap" style="border-top:1px solid var(--hairline)"><span class="label">배경음악</span><audio src=${a.sound.url} controls preload="none" style="height:34px"></audio><span class="muted small">${a.sound.brief?.genre} · ${a.sound.brief?.bpm} BPM${a.sound.placeholder ? " · 대체음" : ""}</span></div>` : null}</div>` : null}

    <div class="card">
      <div class="card-head"><h2>활동 기록</h2><span class="muted small num">${project.log.length}건</span></div>
      <div class="feed" style="max-height:340px">
        ${[...project.log].reverse().map((l) => html`<div class=${`feed-item ${l.level}`}><span class="muted tiny num nowrap" style="width:44px">${clock(l.at)}</span><div class="grow small">${l.text}</div></div>`)}
      </div>
    </div>
  </div>`;
}
