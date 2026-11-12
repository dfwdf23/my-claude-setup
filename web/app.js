// App shell: navigation, routing, and the "새 영상 만들기" dialog.
import { html, render, useState, useEffect, Icon, api, go, connect, useStore, useRoute, useAction, PLATFORM } from "/lib.js";
import { Modal } from "/ui.js";
import { Dashboard, pendingGates } from "/views/dashboard.js";
import { Approvals, ApprovalDetail } from "/views/approvals.js";
import { Projects, ProjectDetail } from "/views/projects.js";
import { Team } from "/views/team.js";
import { Library } from "/views/library.js";
import { Learning } from "/views/learning.js";
import { Settings } from "/views/settings.js";

const NAV = [
  { id: "", label: "관제실", icon: "gauge" },
  { id: "approvals", label: "결재함", icon: "inbox" },
  { id: "projects", label: "프로젝트", icon: "film" },
  { id: "team", label: "AI 팀", icon: "users" },
  { id: "library", label: "라이브러리", icon: "palette" },
  { id: "learning", label: "학습", icon: "brain" },
  { id: "settings", label: "설정", icon: "settings" },
];
const MORE = { id: "more", label: "더보기", icon: "more" };
const TABS = ["", "approvals", "projects", "team"];

function NewVideo({ state, onClose }) {
  const [topic, setTopic] = useState("");
  const [format, setFormat] = useState(state.channels[0]?.default_format || state.formats[0].id);
  const [style, setStyle] = useState("auto");
  const [channel, setChannel] = useState(state.channels[0]?.id);
  const [notes, setNotes] = useState("");
  const [cross, setCross] = useState(false);
  const [busy, run] = useAction();
  const fmt = state.formats.find((f) => f.id === format);
  const tall = fmt.canvas.height > fmt.canvas.width;
  const other = tall ? ["youtube_shorts", "instagram_reels"].find((p) => !fmt.platforms.includes(p)) : null;

  const create = () =>
    run(async () => {
      const targets = cross && other ? [...fmt.platforms, other] : fmt.platforms;
      const { id } = await api("/api/projects", { method: "POST", body: { topic, format, style, channel, notes, targets } });
      onClose();
      go(`projects/${id}`);
    }, "접수했습니다 — 리서치부터 시작합니다");

  return html`<${Modal} title="새 영상 만들기" onClose=${onClose} foot=${html`<span class="hint grow">기획안이 완성되면 결재함에 올라옵니다</span><button class="btn" onClick=${onClose}>취소</button><button class="btn primary" disabled=${busy} onClick=${create}><${Icon} name="play" size="s" />제작 시작</button>`}>
    <div class="field">
      <label>주제</label>
      <input class="input" autofocus placeholder="비워 두면 AI가 채널 주제 안에서 직접 고릅니다" value=${topic} onInput=${(e) => setTopic(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && !busy && create()} />
    </div>
    <div class="field">
      <label>포맷</label>
      <div class="grid g3" style="gap:8px">
        ${state.formats.map((f) => {
          const t = f.canvas.height > f.canvas.width;
          return html`<button class=${`pick ${f.id === format ? "on" : ""}`} onClick=${() => (setFormat(f.id), setCross(false))}>
            <span class="ratio" style=${`width:${t ? 14 : 26}px;height:${t ? 24 : 15}px`}></span>
            <span class="grow"><div style="font-weight:700">${f.name}</div><div class="muted tiny">${f.duration.min >= 60 ? `${Math.round(f.duration.min / 60)}~${Math.round(f.duration.max / 60)}분` : `${f.duration.min}~${f.duration.max}초`}</div></span>
          </button>`;
        })}
      </div>
      ${other ? html`<label class="row gap-s small" style="margin-top:4px;cursor:pointer"><input type="checkbox" checked=${cross} onChange=${(e) => setCross(e.target.checked)} />같은 영상을 ${PLATFORM[other]}에도 올리기</label>` : null}
    </div>
    <div class="field">
      <label>그림체</label>
      <div class="row gap-s wrap">
        <button class=${`pick ${style === "auto" ? "on" : ""}`} style="padding:7px 12px" onClick=${() => setStyle("auto")}><${Icon} name="sparkles" size="s" />AI가 주제에 맞게</button>
        ${state.styles.map((s) => html`<button class=${`pick ${style === s.id ? "on" : ""}`} style="padding:7px 12px" onClick=${() => setStyle(s.id)}><span style=${`width:12px;height:12px;border-radius:50%;background:${s.palette.accent};box-shadow:0 0 0 2px ${s.palette.bg}`}></span>${s.name}</button>`)}
      </div>
    </div>
    ${state.channels.length > 1 ? html`<div class="field"><label>채널</label><select class="select" value=${channel} onChange=${(e) => setChannel(e.target.value)}>${state.channels.map((c) => html`<option value=${c.id}>${c.name}</option>`)}</select></div>` : null}
    <div class="field"><label>요청 메모 (선택)</label><textarea class="textarea" rows="2" placeholder="예: 20대 직장인 대상, 마지막에 반전 하나" value=${notes} onInput=${(e) => setNotes(e.target.value)}></textarea></div>
  <//>`;
}

function ThemeToggle() {
  const current = () => document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const [theme, setTheme] = useState(current());
  const flip = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("studio-theme", next);
    } catch {}
    setTheme(next);
  };
  return html`<button class="btn ghost sm" onClick=${flip} aria-label="밝게/어둡게 전환"><${Icon} name=${theme === "dark" ? "sun" : "moon"} size="s" />${theme === "dark" ? "밝게" : "어둡게"}</button>`;
}

function App() {
  const { state, activity, connected, tick } = useStore();
  const route = useRoute();
  const [creating, setCreating] = useState(false);
  useEffect(connect, []);

  if (!state) return html`<div class="boot">스튜디오를 불러오는 중…</div>`;
  const section = route[0] || "";
  const gates = pendingGates(state).length;
  const onNew = () => setCreating(true);
  const working = Object.keys(state.working).length;

  let view;
  if (section === "") view = html`<${Dashboard} state=${state} activity=${activity} onNew=${onNew} />`;
  else if (section === "approvals") view = route[1] ? html`<${ApprovalDetail} id=${route[1]} state=${state} key=${route[1]} />` : html`<${Approvals} state=${state} />`;
  else if (section === "projects") view = route[1] ? html`<${ProjectDetail} id=${route[1]} state=${state} key=${route[1]} />` : html`<${Projects} state=${state} onNew=${onNew} />`;
  else if (section === "team") view = html`<${Team} state=${state} />`;
  else if (section === "library") view = html`<${Library} state=${state} />`;
  else if (section === "learning") view = html`<${Learning} state=${state} tick=${tick} />`;
  else if (section === "settings") view = html`<${Settings} state=${state} />`;
  else if (section === "more") view = html`<div class="page"><div class="page-head"><h1>더보기</h1></div><div class="card">${NAV.filter((n) => !TABS.includes(n.id)).map((n) => html`<a class="more-link" href=${`#/${n.id}`}><${Icon} name=${n.icon} /><span class="grow">${n.label}</span><${Icon} name="chevron" size="s" /></a>`)}</div></div>`;
  else view = html`<div class="page"><div class="empty">없는 화면입니다. <a href="#/" style="text-decoration:underline">관제실로</a></div></div>`;

  const link = (n, cls = "") => html`<a href=${`#/${n.id}`} class=${`${section === n.id ? "on" : ""} ${cls}`} aria-current=${section === n.id ? "page" : undefined}><${Icon} name=${n.icon} />${n.label}${n.id === "approvals" && gates ? html`<span class="count num">${gates}</span>` : null}</a>`;

  return html`<div class="app">
    <aside class="side">
      <div class="brand"><div class="brand-mark"><${Icon} name="play" size="s" /></div><div><div class="brand-name">${state.studio.name}</div><div class="muted tiny">AI 영상 제작 관제실</div></div></div>
      <nav class="nav col" style="gap:2px">${NAV.map((n) => link(n))}</nav>
      <div class="side-foot">
        <button class="btn primary" onClick=${onNew}><${Icon} name="plus" size="s" />새 영상 만들기</button>
        <div class="mode"><span class=${`dot ${!connected ? "" : working ? "work" : state.brainSimulated ? "sim" : "live"}`}></span><span class="grow clip">${!connected ? "서버 연결 끊김" : state.brainSimulated ? "시뮬레이션 모드" : working ? `${working}명 작업 중` : "가동 중"}</span></div>
        <${ThemeToggle} />
      </div>
    </aside>
    <div style="min-width:0">
      <div class="topbar"><div class="brand-mark" style="width:28px;height:28px"><${Icon} name="play" size="s" /></div><b class="grow clip">${state.studio.name}</b><${ThemeToggle} /><button class="btn primary sm" onClick=${onNew}><${Icon} name="plus" size="s" />새 영상</button></div>
      <main class="main">${view}</main>
    </div>
    <nav class="tabbar">${[...NAV.filter((n) => TABS.includes(n.id)), MORE].map((n) => link(n, n.id === "more" && ["library", "learning", "settings"].includes(section) ? "on" : ""))}</nav>
    ${creating ? html`<${NewVideo} state=${state} onClose=${() => setCreating(false)} />` : null}
  </div>`;
}

render(html`<${App} />`, document.getElementById("app"));
