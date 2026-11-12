// AI 팀 — who does what, and which AI model each job is currently wired to.
import { html, useState, Icon, api, useAction, timeAgo, CAPABILITY, safeUrl } from "/lib.js";
import { ConfigEditor } from "/ui.js";

function RouteLine({ cap, route }) {
  const name = CAPABILITY[cap] || cap;
  if (!route) return html`<div class="route"><span class="dot"></span><span class="muted">${name}</span><span class="muted">— 이 컴퓨터에서 처리</span></div>`;
  return html`<div class="route" title=${route.use}>
    <span class=${`dot ${route.local ? "sim" : "live"}`}></span><span class="muted nowrap">${name}</span>
    ${route.local ? html`<span class="ink2">로컬 엔진</span>` : html`<code class="clip">${route.providerName} · ${route.model.split("/").slice(-2).join("/")}</code>`}
  </div>`;
}

function AgentCard({ agent, state, onEdit }) {
  const jobs = state.working[agent.id] || [];
  const job = jobs[0] && state.projects.find((p) => p.id === jobs[0].projectId);
  return html`<div class="card pad col">
    <div class="row top">
      <div class=${`agent-ico ${jobs.length ? "work" : ""}`}><${Icon} name=${agent.icon} size="l" /></div>
      <div class="grow">
        <div class="row gap-s between"><h3>${agent.name}</h3>${jobs.length ? html`<span class="badge run"><${Icon} name="loader" size="s" />작업 중</span>` : html`<span class="badge idle">대기</span>`}</div>
        <div class="ink2 small" style="margin-top:2px">${agent.summary}</div>
      </div>
    </div>
    ${job ? html`<a class="small clip" href=${`#/projects/${job.id}`} style="color:var(--accent-ink);font-weight:650">▸ ${job.title}</a>` : null}
    <div class="col gap-s">
      ${agent.uses.filter((u) => state.routing[u] || u === "llm").map((u) => html`<${RouteLine} cap=${u} route=${agent.routes[u] && u === "llm" && agent.model && !agent.routes[u].local ? { ...agent.routes[u], model: agent.model } : agent.routes[u]} />`)}
    </div>
    <div class="row between" style="margin-top:auto">
      <span class="muted tiny">${agent.stages.length ? `담당: ${agent.stages.join(" · ")}` : "상시 지원"}</span>
      <button class="btn ghost sm" onClick=${() => onEdit(agent)}><${Icon} name="edit" size="s" />지시문</button>
    </div>
  </div>`;
}

function Candidate({ c, index }) {
  const status = c.active ? "사용 중" : c.connected ? "대기 (예비)" : c.local ? "대체 엔진" : `키 필요`;
  return html`<div class=${`cand ${c.active ? "active" : ""} ${!c.connected && !c.active ? "off" : ""}`} title=${c.health?.lastError || c.use}>
    <span class="muted num tiny">${index + 1}</span>
    <span class=${`dot ${c.active ? (c.local ? "sim" : "live") : ""}`}></span>
    <div class="grow" style="min-width:0">
      <div class="clip" style="font-weight:650">${c.local ? "로컬 엔진" : c.providerName} <span class="muted" style="font-weight:500">${c.model.split("/").slice(-3).join("/")}</span></div>
      <div class="muted tiny clip">${status}${!c.connected && !c.local ? ` · ${c.needs.join(", ")}` : ""}${c.when ? ` · 조건: ${Object.entries(c.when).map(([k, v]) => `${k}=${v.join("|")}`).join(", ")}` : ""}${c.health ? ` · 성공 ${c.health.ok} / 실패 ${c.health.fail}${c.health.avgMs ? ` · 평균 ${(c.health.avgMs / 1000).toFixed(1)}초` : ""}` : ""}</div>
      ${c.health?.lastError ? html`<div class="tiny clip" style="color:var(--critical-ink)">${c.health.lastError}</div>` : null}
    </div>
  </div>`;
}

const ROUTE_ORDER = ["llm", "search", "image", "video", "tts", "music", "motion", "edit", "publish.youtube", "publish.instagram", "analytics.youtube", "analytics.instagram"];

export function Team({ state }) {
  const [editing, setEditing] = useState(null);
  const [busy, run] = useAction();
  const scout = state.scout;
  const scouting = Boolean(state.working["tech-scout"]);
  const caps = [...ROUTE_ORDER.filter((c) => state.routing[c]), ...Object.keys(state.routing).filter((c) => !ROUTE_ORDER.includes(c))];
  // Show the team in the order the work flows through them.
  const stageIndex = (a) => {
    const i = state.pipeline.stages.findIndex((d) => d.agent === a.id);
    return i < 0 ? 999 : i;
  };
  const ordered = [...state.agents].sort((a, b) => stageIndex(a) - stageIndex(b));
  const realCount = caps.filter((c) => state.routing[c].some((x) => x.active && !x.local)).length;

  return html`<div class="page">
    <div class="page-head">
      <div><h1>AI 팀</h1><p>${state.agents.length}명의 담당 AI가 단계별로 일합니다. 지시문과 연결 모델은 언제든 바꿀 수 있고, 저장 즉시 적용됩니다.</p></div>
    </div>

    <div class="grid auto-fill">${ordered.map((agent) => html`<${AgentCard} agent=${agent} state=${state} onEdit=${setEditing} />`)}</div>

    <div class="card">
      <div class="card-head">
        <div><h2>분야별 AI 연결표</h2><div class="muted small">위에서부터 순서대로 시도하고, 키가 없거나 실패하면 다음 후보로 자동 전환합니다 · 실제 AI 연결 ${realCount}/${caps.length} 분야</div></div>
        <button class="btn sm" onClick=${() => setEditing({ file: "routing.yaml", name: "AI 연결표" })}><${Icon} name="edit" size="s" />연결표 편집</button>
      </div>
      <div class="card-body col gap-l">
        ${caps.map(
          (cap) => html`<div class="col gap-s">
            <div class="row gap-s"><b>${CAPABILITY[cap] || cap}</b><code class="k">${cap}</code></div>
            <div class="grid auto-fill" style="grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px">${state.routing[cap].map((c, i) => html`<${Candidate} c=${c} index=${i} />`)}</div>
          </div>`,
        )}
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <div><h2>AI 기술 스카우트</h2><div class="muted small">새로 나온 영상·이미지·음성 모델을 조사해 연결표에 넣을 후보를 제안합니다</div></div>
        <button class="btn sm primary" disabled=${busy || scouting} onClick=${() => run(() => api("/api/scout", { method: "POST" }), "조사를 시작했습니다")}><${Icon} name="telescope" size="s" />${scouting ? "조사 중…" : "최신 모델 조사"}</button>
      </div>
      <div class="card-body col">
        ${!scout
          ? html`<div class="muted small">아직 조사한 적이 없습니다.</div>`
          : scout.error
            ? html`<div class="banner bad"><${Icon} name="alert" /><div>${scout.error}</div></div>`
            : html`<div class="ink2">${scout.summary} <span class="muted tiny">· ${timeAgo(scout.at)}</span></div>
                ${scout.proposals.map(
                  (p) => html`<div class="cand"><span class="chip">${CAPABILITY[p.capability] || p.capability}</span><div class="grow" style="min-width:0"><code class="k">${p.candidate}</code><div class="small ink2" style="margin-top:3px">${p.why}</div></div>${safeUrl(p.source_url) ? html`<a class="btn ghost sm icon" href=${safeUrl(p.source_url)} target="_blank" rel="noreferrer" aria-label="출처"><${Icon} name="external" size="s" /></a>` : null}</div>`,
                )}
                ${scout.proposals.length ? html`<div class="hint">마음에 드는 후보는 ‘연결표 편집’에서 해당 분야 맨 위에 한 줄로 추가하면 됩니다.</div>` : null}`}
      </div>
    </div>

    ${editing ? html`<${ConfigEditor} path=${editing.file} title=${`${editing.name} — ${editing.file}`} onClose=${() => setEditing(null)} />` : null}
  </div>`;
}
