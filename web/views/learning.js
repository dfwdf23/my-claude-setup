// 학습 — the team's memory. Lessons here are injected into each AI's instructions on every job.
import { html, useState, useEffect, Icon, api, useAction, timeAgo, SOURCE, compact } from "/lib.js";
import { Seg, Switch, Meter, LineChart, Empty } from "/ui.js";

function usePlaybook(tick) {
  const [data, setData] = useState(null);
  useEffect(() => {
    api("/api/playbook").then(setData).catch(() => {});
  }, [tick]);
  return data;
}

const SOURCE_ICON = { seed: "list", feedback: "inbox", qa: "shield", analytics: "chart", manual: "pen" };

function Lesson({ l, state, onChange }) {
  const [busy, run] = useAction();
  const agent = state.agents.find((a) => a.id === l.agent);
  const format = state.formats.find((f) => f.id === l.format);
  const style = state.styles.find((s) => s.id === l.style);
  const active = l.status === "active";
  return html`<div class="list-row" style=${`align-items:flex-start;${active ? "" : "opacity:.55"}`}>
    <div class="feed-ico"><${Icon} name=${SOURCE_ICON[l.source] || "brain"} size="s" /></div>
    <div class="grow col gap-s">
      <div style="font-weight:600">${l.text}</div>
      <div class="row gap-s wrap">
        <span class="chip">${SOURCE[l.source] || l.source}</span>
        <span class="chip line">${agent ? agent.name : "팀 전체"}</span>
        ${format ? html`<span class="chip line">${format.name}</span>` : null}
        ${style ? html`<span class="chip line">${style.name}</span>` : null}
        ${l.origin === "sim" ? html`<span class="chip line" title="시뮬레이션에서 생긴 노트는 실제 제작에 쓰이지 않습니다">시뮬레이션</span>` : null}
        ${l.createdAt ? html`<span class="muted tiny">${timeAgo(l.createdAt)}</span>` : null}
      </div>
      ${l.evidence && l.source !== "seed" ? html`<div class="muted small">근거: ${l.evidence}</div>` : null}
      ${l.source !== "seed" && l.source !== "manual" ? html`<div class="row gap-s" style="max-width:220px"><span class="muted tiny nowrap">확신도</span><div class="grow"><${Meter} value=${Math.round(l.confidence * 100)} /></div><span class="muted tiny num">${Math.round(l.confidence * 100)}%</span></div>` : null}
    </div>
    <div class="row gap-s">
      <${Switch} on=${active} disabled=${busy} label="이 노트 사용" onChange=${(v) => run(() => api(`/api/playbook/${l.id}`, { method: "PATCH", body: { status: v ? "active" : "retired" } }).then(onChange))} />
      ${l.source !== "seed" ? html`<button class="btn ghost sm icon" aria-label="삭제" disabled=${busy} onClick=${() => confirm("이 학습 노트를 삭제할까요?") && run(() => api(`/api/playbook/${l.id}`, { method: "DELETE" }).then(onChange))}><${Icon} name="trash" size="s" /></button>` : null}
    </div>
  </div>`;
}

export function Learning({ state, tick }) {
  const [bump, setBump] = useState(0);
  const data = usePlaybook(`${state.playbook.total}:${state.playbook.active}:${bump}`);
  const [filter, setFilter] = useState("learned");
  const [text, setText] = useState("");
  const [agent, setAgent] = useState("");
  const [format, setFormat] = useState("");
  const [busy, run] = useAction();
  const reload = () => setBump((n) => n + 1);

  const scored = state.projects.filter((p) => p.qa?.score && p.status !== "canceled").sort((a, b) => a.createdAt - b.createdAt);
  const published = state.projects.filter((p) => p.metrics).map((p) => ({ p, m: Object.values(p.metrics.platforms).find((x) => !x.error) })).filter((x) => x.m);
  const lessons = data?.lessons || [];
  const shown = lessons.filter((l) => (filter === "all" ? true : filter === "learned" ? l.source !== "seed" : filter === "seed" ? l.source === "seed" : l.status !== "active"));
  const stats = state.playbook;
  const passScore = state.studio.quality.pass_score ?? 80;

  const add = () =>
    run(async () => {
      await api("/api/playbook", { method: "POST", body: { text, agent, format } });
      setText("");
      reload();
    }, "팀에 전달했습니다 — 다음 작업부터 반영됩니다");

  return html`<div class="page">
    <div class="page-head"><div><h1>학습</h1><p>결재 피드백, 품질 검수, 실제 성과에서 배운 것이 학습 노트로 쌓이고, 다음 영상부터 각 담당 AI의 지시문에 들어갑니다.</p></div></div>

    <div class="grid g4">
      <div class="card tile"><div class="k"><${Icon} name="brain" size="s" />활성 학습 노트</div><div class="v num">${stats.active}<small>개</small></div><div class="d">전체 ${stats.total}개 중</div></div>
      <div class="card tile"><div class="k"><${Icon} name="sparkles" size="s" />스스로 배운 것</div><div class="v num">${stats.learned}<small>개</small></div><div class="d">기본 원칙 외에 새로 생긴 노트</div></div>
      <div class="card tile"><div class="k"><${Icon} name="inbox" size="s" />결재 피드백에서</div><div class="v num">${(stats.bySource.feedback || 0) + (stats.bySource.manual || 0)}<small>개</small></div><div class="d">내 취향·기준을 반영한 노트</div></div>
      <div class="card tile"><div class="k"><${Icon} name="chart" size="s" />성과·검수에서</div><div class="v num">${(stats.bySource.analytics || 0) + (stats.bySource.qa || 0)}<small>개</small></div><div class="d">조회 데이터와 QA가 가르친 노트</div></div>
    </div>

    <div class="split">
      <div class="card">
        <div class="card-head"><h2>품질 점수 추이</h2><span class="muted small">학습이 쌓일수록 올라가야 하는 선</span></div>
        <div class="card-body"><${LineChart} points=${scored.slice(-40).map((p) => ({ y: p.qa.score, label: p.title.slice(0, 22), sub: `재작업 ${p.qa.rounds - 1}회` }))} refLine=${{ value: passScore, label: `합격선 ${passScore}` }} format=${(v) => `${v}점`} /></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>팀에 직접 지시하기</h2></div>
        <div class="card-body col">
          <textarea class="textarea" rows="3" placeholder="예: 영어 단어는 되도록 쓰지 말고 우리말로 풀어 쓴다" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>
          <div class="grid g2">
            <select class="select" value=${agent} onChange=${(e) => setAgent(e.target.value)} aria-label="적용 대상"><option value="">팀 전체</option>${state.agents.filter((a) => a.uses.includes("llm")).map((a) => html`<option value=${a.id}>${a.name}</option>`)}</select>
            <select class="select" value=${format} onChange=${(e) => setFormat(e.target.value)} aria-label="적용 포맷"><option value="">모든 포맷</option>${state.formats.map((f) => html`<option value=${f.id}>${f.name}</option>`)}</select>
          </div>
          <button class="btn primary" disabled=${busy || text.trim().length < 6} onClick=${add}><${Icon} name="plus" size="s" />학습 노트로 추가</button>
          <div class="hint">직접 쓴 지시는 가장 높은 우선순위로 매번 전달됩니다.</div>
        </div>
      </div>
    </div>

    ${published.length
      ? html`<div class="card"><div class="card-head"><h2>예측과 실제</h2><span class="muted small">기획 때 예상한 시청 지속률 vs 실제</span></div>
          <div class="scroll-x"><table class="t"><thead><tr><th>영상</th><th>품질</th><th>예상 지속률</th><th>실제 지속률</th><th>조회수</th></tr></thead><tbody>
            ${published.slice(0, 12).map(({ p, m }) => html`<tr><td><a href=${`#/projects/${p.id}`} style="font-weight:650">${p.title}</a>${m.simulated ? html` <span class="chip line">시뮬레이션</span>` : null}</td><td class="num">${p.qa?.score ?? "-"}</td><td class="num muted">${p.predicted ? `${p.predicted.retention_pct}%` : "-"}</td><td class="num">${m.avgViewPct != null ? `${m.avgViewPct}%` : "집계 전"}</td><td class="num">${compact(m.views)}</td></tr>`)}
          </tbody></table></div></div>`
      : null}

    <div class="col">
      <div class="section-title row wrap"><h2>학습 노트</h2><${Seg} value=${filter} onChange=${setFilter} options=${[{ value: "learned", label: "배운 것", count: lessons.filter((l) => l.source !== "seed").length }, { value: "seed", label: "기본 원칙", count: lessons.filter((l) => l.source === "seed").length }, { value: "off", label: "꺼둔 것", count: lessons.filter((l) => l.status !== "active").length }, { value: "all", label: "전체" }]} /></div>
      <div class="card">
        ${!data
          ? html`<div class="empty">불러오는 중…</div>`
          : shown.length
            ? shown.map((l) => html`<${Lesson} l=${l} state=${state} onChange=${reload} key=${l.id} />`)
            : html`<${Empty} icon="brain" title=${filter === "learned" ? "아직 새로 배운 것이 없습니다" : "해당하는 노트가 없습니다"}>결재할 때 수정 요청이나 한마디를 남기면, 그 내용이 가장 먼저 학습 노트가 됩니다.<//>`}
      </div>
    </div>
  </div>`;
}
