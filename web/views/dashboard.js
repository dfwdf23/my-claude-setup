// 관제실 — everything at a glance: what needs your decision, what the team is doing, how quality is trending.
import { html, Icon, go, timeAgo, compact, duration, PLATFORM } from "/lib.js";
import { StatusBadge, FormatChip, StyleChip, Track, currentStage, progressOf, LineChart, Poster, Empty, Ago, activityIcon } from "/ui.js";

const GATE = { plan: { label: "기획 결재", icon: "clapperboard", verb: "기획안 검토" }, upload: { label: "업로드 결재", icon: "upload", verb: "완성본 검토" } };

export function pendingGates(state) {
  const out = [];
  for (const p of state.projects) {
    if (p.status === "canceled") continue;
    for (const [gate, g] of Object.entries(p.gates)) if (g.status === "pending") out.push({ project: p, gate, since: g.requestedAt });
  }
  return out.sort((a, b) => a.since - b.since);
}

export function ApprovalCard({ item, state }) {
  const { project: p, gate } = item;
  const g = GATE[gate];
  const format = state.formats.find((f) => f.id === p.brief.format);
  return html`<a class="card approve-card" href=${`#/approvals/${p.id}`}>
    ${gate === "upload" ? html`<${Poster} project=${p} format=${format} />` : html`<div class="approve-kind"><${Icon} name=${g.icon} size="l" /></div>`}
    <div class="grow col gap-s">
      <div class="row gap-s wrap"><span class="badge wait"><${Icon} name="inbox" size="s" />${g.label}</span><${FormatChip} format=${format} />${p.qa && gate === "upload" ? html`<span class="chip num">품질 ${p.qa.score}점</span>` : null}</div>
      <div style="font-weight:700;font-size:15px" class="clamp2">${p.title}</div>
      <div class="muted small">${timeAgo(item.since)} 올라옴${p.gates[gate].rounds ? ` · ${p.gates[gate].rounds + 1}번째 검토` : ""}</div>
    </div>
    <span class="btn primary sm nowrap">${g.verb}<${Icon} name="chevron" size="s" /></span>
  </a>`;
}

function Tile({ label, icon, value, unit, note, attn, href }) {
  const body = html`<div class="k"><${Icon} name=${icon} size="s" />${label}</div>
    <div class="v num">${value}${unit ? html`<small>${unit}</small>` : null}</div>
    <div class="d">${note}</div>`;
  return href ? html`<a class=${`card tile ${attn ? "attn" : ""}`} href=${href}>${body}</a>` : html`<div class=${`card tile ${attn ? "attn" : ""}`}>${body}</div>`;
}

function LineRow({ p, state }) {
  const format = state.formats.find((f) => f.id === p.brief.format);
  const style = state.styles.find((s) => s.id === p.styleId);
  const cur = currentStage(p, state.pipeline);
  const pct = Math.round(progressOf(p, state.pipeline) * 100);
  const agent = cur.def?.agent && state.agents.find((a) => a.id === cur.def.agent);
  return html`<a class="line-row" href=${`#/projects/${p.id}`}>
    <div class="row top between gap-l">
      <div class="col gap-s" style="min-width:0">
        <span style="font-weight:700;font-size:15px" class="clip">${p.title}</span>
        <div class="row gap-s wrap"><${StatusBadge} project=${p} /><${FormatChip} format=${format} /><${StyleChip} style=${style} /></div>
      </div>
      <div class="col" style="gap:2px;text-align:right;min-width:0;flex:none;max-width:46%">
        <span class="small clip" style="font-weight:650">${agent ? `${agent.name} · ` : ""}${cur.text}</span>
        <span class="muted tiny clip">${cur.st?.error || cur.label || `전체 ${pct}% 진행`}</span>
      </div>
    </div>
    <${Track} project=${p} pipeline=${state.pipeline} fluid />
  </a>`;
}

export function Dashboard({ state, activity, onNew }) {
  const gates = pendingGates(state);
  const live = state.projects.filter((p) => ["active", "waiting", "paused", "failed"].includes(p.status));
  const done = state.projects.filter((p) => p.status === "done");
  const weekAgo = Date.now() - 7 * 86_400_000;
  const scored = state.projects.filter((p) => p.qa?.score && p.status !== "canceled").sort((a, b) => a.createdAt - b.createdAt);
  const recent = scored.slice(-10);
  const prev = scored.slice(-20, -10);
  const avg = (list) => (list.length ? Math.round(list.reduce((n, p) => n + p.qa.score, 0) / list.length) : null);
  const avgNow = avg(recent);
  const avgPrev = avg(prev);
  const delta = avgNow != null && avgPrev != null ? avgNow - avgPrev : null;
  const working = Object.keys(state.working).length;
  const feed = [...activity].reverse().slice(0, 40);
  const passScore = state.studio.quality.pass_score ?? 80;

  return html`<div class="page">
    <div class="page-head">
      <div>
        <h1>관제실</h1>
        <p>${new Date().toLocaleDateString("ko-KR", { month: "long", day: "numeric", weekday: "long" })} · AI 팀 ${state.agents.length}명 중 ${working}명 작업 중</p>
      </div>
      <button class="btn primary lg" onClick=${onNew}><${Icon} name="plus" />새 영상 만들기</button>
    </div>

    ${state.brainSimulated ? html`<div class="banner warn"><${Icon} name="alert" /><div><b>시뮬레이션 모드입니다.</b> AI 키가 연결되지 않아 기획·대본은 템플릿으로, 영상·음성은 대체 엔진으로 만들어집니다. 전체 흐름과 화면을 점검하는 용도이며, <a href="#/settings" style="text-decoration:underline">설정</a>에서 키를 넣으면 그 분야부터 실제 AI로 바뀝니다.</div></div>` : null}
    ${state.studio.errors.length ? html`<div class="banner bad"><${Icon} name="alert" /><div><b>설정 파일 오류</b> — ${state.studio.errors.map((e) => `${e.file}: ${e.message}`).join(" / ")}. 고칠 때까지 이전 설정으로 동작합니다.</div></div>` : null}

    ${gates.length
      ? html`<div class="col">
          <div class="section-title"><h2>결재가 필요합니다 <span class="muted num">${gates.length}</span></h2><a class="btn ghost sm" href="#/approvals">결재함 열기<${Icon} name="chevron" size="s" /></a></div>
          <div class="grid g2">${gates.slice(0, 4).map((item) => html`<${ApprovalCard} item=${item} state=${state} />`)}</div>
        </div>`
      : null}

    <div class="grid g4">
      <${Tile} label="제작 중" icon="film" value=${live.filter((p) => p.status === "active").length} unit="편" note=${live.some((p) => p.status === "failed") ? `오류 ${live.filter((p) => p.status === "failed").length}편 확인 필요` : "AI 팀이 진행 중인 영상"} href="#/projects" />
      <${Tile} label="결재 대기" icon="inbox" value=${gates.length} unit="건" note=${gates.length ? `가장 오래된 건 ${timeAgo(gates[0].since)}` : "밀린 결재가 없습니다"} attn=${gates.length > 0} href="#/approvals" />
      <${Tile} label="최근 7일 완성" icon="check" value=${done.filter((p) => p.updatedAt > weekAgo).length} unit="편" note=${`누적 ${done.length}편`} href="#/projects" />
      <${Tile} label="평균 품질 점수" icon="shield" value=${avgNow ?? "–"} unit=${avgNow != null ? "점" : ""} note=${delta != null ? html`<span class=${`delta ${delta >= 0 ? "up" : "down"}`}>${delta >= 0 ? "▲" : "▼"} ${Math.abs(delta)}점</span> 직전 10편 대비` : `최근 ${recent.length}편 기준 · 합격선 ${passScore}점`} href="#/learning" />
    </div>

    <div class="split">
      <div class="card">
        <div class="card-head"><h2>제작 라인</h2><span class="muted small">● AI 단계 ◆ 결재</span></div>
        ${live.length
          ? live.map((p) => html`<${LineRow} p=${p} state=${state} />`)
          : html`<${Empty} icon="film" title="진행 중인 영상이 없습니다">주제를 주거나 비워 두면 AI 팀이 리서치부터 시작해 기획안을 결재함에 올립니다.<div style="margin-top:12px"><button class="btn primary" onClick=${onNew}><${Icon} name="plus" />새 영상 만들기</button></div><//>`}
      </div>
      <div class="card">
        <div class="card-head"><h2>지금 일하는 AI</h2><span class="row gap-s muted small"><span class=${`dot ${working ? "work" : ""}`}></span>${working ? `${working}명 작업 중` : "대기 중"}</span></div>
        <div class="feed">
          ${feed.length
            ? feed.map(
                (a) => html`<div class=${`feed-item ${a.level}`}>
                  <div class="feed-ico"><${Icon} name=${activityIcon(a, state.agents)} size="s" /></div>
                  <div class="grow">${a.projectId && state.projects.some((p) => p.id === a.projectId) ? html`<a href=${`#/projects/${a.projectId}`}>${a.text}</a>` : a.text}</div>
                  <${Ago} ts=${a.at} />
                </div>`,
              )
            : html`<div class="empty small">아직 활동이 없습니다</div>`}
        </div>
      </div>
    </div>

    <div class="split">
      <div class="card">
        <div class="card-head"><h2>품질 점수 추이</h2><span class="muted small">영상별 QA 점수 · 최근 ${Math.min(30, scored.length)}편</span></div>
        <div class="card-body">
          <${LineChart}
            points=${scored.slice(-30).map((p) => ({ y: p.qa.score, label: p.title.slice(0, 22), sub: new Date(p.createdAt).toLocaleDateString("ko-KR", { month: "short", day: "numeric" }) }))}
            refLine=${{ value: passScore, label: `합격선 ${passScore}` }}
            format=${(v) => `${v}점`}
          />
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>최근 완성작</h2><a class="btn ghost sm" href="#/projects">전체</a></div>
        ${done.length
          ? done.slice(0, 5).map((p) => {
              const format = state.formats.find((f) => f.id === p.brief.format);
              const m = p.metrics && Object.values(p.metrics.platforms)[0];
              return html`<a class="list-row" href=${`#/projects/${p.id}`}>
                <${Poster} project=${p} format=${format} />
                <div class="grow col gap-s">
                  <div class="clamp2" style="font-weight:650">${p.title}</div>
                  <div class="row gap-s wrap muted small">
                    <span class="num">품질 ${p.qa?.score ?? "-"}점</span><span>·</span><span>${duration(p.duration)}</span>
                    ${m && !m.error ? html`<span>·</span><span class="num">조회수 ${compact(m.views)}${m.simulated ? " (시뮬)" : ""}</span>` : null}
                  </div>
                  <div class="row gap-s wrap">${(p.publish || []).map((r) => html`<span class="chip line">${PLATFORM[r.platform] || r.platform}${r.dryRun ? " · 패키지" : ""}</span>`)}</div>
                </div>
              </a>`;
            })
          : html`<div class="empty small">완성된 영상이 여기에 쌓입니다</div>`}
      </div>
    </div>
  </div>`;
}
