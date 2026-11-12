// 설정 — operating mode, safety switches, API keys, account connections, raw config files.
import { html, useState, useEffect, Icon, api, useAction } from "/lib.js";
import { Seg, Switch, ConfigEditor } from "/ui.js";

function KeyRow({ k }) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [busy, run] = useAction();
  const save = (v) =>
    run(async () => {
      await api("/api/secrets", { method: "POST", body: { key: k.key, value: v } });
      setValue("");
      setOpen(false);
    }, v ? "저장했습니다 — 다음 작업부터 사용합니다" : "삭제했습니다");
  return html`<div class="list-row" style="flex-wrap:wrap">
    <span class=${`dot ${k.set ? "live" : ""}`}></span>
    <div class="grow" style="min-width:200px">
      <div style="font-weight:650">${k.label}</div>
      <div class="muted tiny"><code class="k">${k.key}</code> · ${k.set ? (k.source === "env" ? "환경변수(.env)로 설정됨" : "이 화면에서 저장됨") : "미설정"}</div>
    </div>
    ${open
      ? html`<input class="input" style="flex:1 1 220px;max-width:340px" type="password" autocomplete="off" placeholder="키 붙여넣기" value=${value} onInput=${(e) => setValue(e.target.value)} />
          <button class="btn primary sm" disabled=${busy || !value.trim()} onClick=${() => save(value)}>저장</button><button class="btn sm" onClick=${() => setOpen(false)}>취소</button>`
      : html`<a class="btn ghost sm" href=${k.url} target="_blank" rel="noreferrer">발급<${Icon} name="external" size="s" /></a>
          ${k.set && k.source !== "env" ? html`<button class="btn ghost sm danger" disabled=${busy} onClick=${() => confirm(`${k.key} 를 삭제할까요?`) && save("")}>삭제</button>` : null}
          ${k.source !== "env" ? html`<button class="btn sm" onClick=${() => setOpen(true)}>${k.set ? "변경" : "입력"}</button>` : null}`}
  </div>`;
}

export function Settings({ state }) {
  const [busy, run] = useAction();
  const [editing, setEditing] = useState(null);
  const [files, setFiles] = useState([]);
  useEffect(() => {
    api("/api/config/files").then((r) => setFiles(r.files)).catch(() => {});
  }, [state.studio.version]);
  const set = (body, ok) => run(() => api("/api/settings", { method: "POST", body }), ok);
  const s = state.studio;
  const live = s.publish.live === true;
  const ai = state.secrets.filter((k) => !k.key.startsWith("YOUTUBE_") && !k.key.startsWith("INSTAGRAM_"));
  const yt = state.secrets.filter((k) => k.key.startsWith("YOUTUBE_"));
  const ig = state.secrets.filter((k) => k.key.startsWith("INSTAGRAM_"));
  const group = (prefix) => files.filter((f) => (prefix ? f.startsWith(prefix) : !f.includes("/")));

  return html`<div class="page">
    <div class="page-head"><div><h1>설정</h1><p>여기서 바꾸는 값은 <code class="k">studio/</code> 폴더의 파일에 저장되고, 재시작 없이 바로 적용됩니다.</p></div></div>

    <div class="grid g2">
      <div class="card pad col">
        <h2>운영 모드</h2>
        <${Seg} value=${s.mode} onChange=${(mode) => set({ mode }, mode === "simulation" ? "시뮬레이션 모드 — 외부 AI를 호출하지 않습니다" : "자동 모드 — 연결된 AI를 사용합니다")} options=${[{ value: "auto", label: "자동 (연결된 AI 사용)", icon: "bolt" }, { value: "simulation", label: "시뮬레이션 (비용 0원)", icon: "shield" }]} />
        <div class="hint">자동: 키가 있는 분야는 실제 AI로, 없는 분야는 이 컴퓨터의 대체 엔진으로 만듭니다. 시뮬레이션: 어떤 외부 AI도 호출하지 않고 흐름만 점검합니다.</div>
        <div class="row gap-s"><span class=${`dot ${state.brainSimulated ? "sim" : "live"}`}></span><span class="small">현재 두뇌: <b>${state.brainSimulated ? "시뮬레이션 (템플릿)" : "Claude 연결됨"}</b></span></div>
      </div>

      <div class="card pad col">
        <h2>품질 기준</h2>
        <div class="row between"><div><div style="font-weight:650">QA 합격선</div><div class="hint">이 점수에 못 미치면 담당 AI에게 돌려보내 다시 만듭니다 (최대 ${s.quality.max_revision_rounds ?? 2}회)</div></div>
          <select class="select" style="width:96px" value=${String(s.quality.pass_score ?? 80)} disabled=${busy} onChange=${(e) => set({ passScore: Number(e.target.value) }, "합격선을 바꿨습니다")}>${[70, 75, 80, 85, 90].map((n) => html`<option value=${String(n)}>${n}점</option>`)}</select></div>
        <hr class="divider" />
        <div class="row between"><div><div style="font-weight:650">영상 1편 예산 상한</div><div class="hint">추정 비용이 넘으면 자동으로 멈추고 알려줍니다</div></div>
          <div class="small num ink2" style="text-align:right">${typeof s.budget.per_project_usd === "number" ? `$${s.budget.per_project_usd}` : Object.entries(s.budget.per_project_usd || {}).map(([k, v]) => html`<div>${state.formats.find((f) => f.id === k)?.name || k} $${v}</div>`)}</div></div>
        <button class="btn ghost sm" style="align-self:flex-start" onClick=${() => setEditing("studio.yaml")}><${Icon} name="edit" size="s" />세부 값 편집 (studio.yaml)</button>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><div><h2>업로드</h2><div class="muted small">업로드 결재에서 승인한 영상만 올라갑니다</div></div></div>
      <div class="card-body col">
        <div class="row between">
          <div><div style="font-weight:700">실제 업로드</div><div class="hint">꺼져 있으면 승인해도 업로드 패키지(영상·썸네일·게시 정보)만 <code class="k">data/outbox</code>에 저장합니다</div></div>
          <${Switch} on=${live} disabled=${busy} label="실제 업로드" onChange=${(v) => (!v || confirm("이제부터 업로드 결재를 승인하면 실제 채널에 올라갑니다. 켤까요?")) && set({ publishLive: v }, v ? "실제 업로드를 켰습니다" : "실제 업로드를 껐습니다")} />
        </div>
        ${live && !state.connections.youtube && !state.connections.instagram ? html`<div class="banner warn"><${Icon} name="alert" /><div>실제 업로드가 켜져 있지만 연결된 계정이 없습니다. 아래에서 계정을 연결하세요.</div></div>` : null}
        <hr class="divider" />
        <div class="row between wrap">
          <div class="row"><span class=${`dot ${state.connections.youtube ? "live" : ""}`}></span><div><div style="font-weight:650">YouTube ${state.connections.youtube ? "연결됨" : "미연결"}</div><div class="hint">OAuth 클라이언트 ID·보안 비밀을 입력한 뒤 계정을 연결합니다. 리디렉션 URI: <code class="k">${location.origin}/api/oauth/youtube/callback</code></div></div></div>
          <a class=${`btn sm ${state.connections.youtubeReady ? "primary" : ""}`} href=${state.connections.youtubeReady ? "/api/oauth/youtube/start" : undefined} aria-disabled=${!state.connections.youtubeReady} style=${state.connections.youtubeReady ? "" : "opacity:.5;pointer-events:none"}><${Icon} name="link" size="s" />${state.connections.youtube ? "다시 연결" : "YouTube 계정 연결"}</a>
        </div>
        ${yt.map((k) => html`<${KeyRow} k=${k} />`)}
        <hr class="divider" />
        <div class="row"><span class=${`dot ${state.connections.instagram ? "live" : ""}`}></span><div><div style="font-weight:650">Instagram ${state.connections.instagram ? "연결됨" : "미연결"}</div><div class="hint">비즈니스/크리에이터 계정의 액세스 토큰과 계정 ID가 필요합니다 (Facebook 로그인 토큰 권장 — 파일을 직접 올릴 수 있습니다)</div></div></div>
        ${ig.map((k) => html`<${KeyRow} k=${k} />`)}
      </div>
    </div>

    <div class="card">
      <div class="card-head"><div><h2>AI 연결 키</h2><div class="muted small">키를 넣는 순간 해당 분야가 대체 엔진에서 실제 AI로 바뀝니다. 키는 이 컴퓨터에만 저장되며 화면에 다시 표시되지 않습니다.</div></div><a class="btn ghost sm" href="#/team">연결 상태 보기<${Icon} name="chevron" size="s" /></a></div>
      ${ai.map((k) => html`<${KeyRow} k=${k} />`)}
    </div>

    <div class="card">
      <div class="card-head"><div><h2>설정 파일</h2><div class="muted small">스튜디오의 모든 동작은 이 파일들로 정해집니다. 고치고 저장하면 다음 작업부터 적용됩니다.</div></div></div>
      <div class="card-body col gap-l">
        ${[["", "전체 설정"], ["agents/", "AI 팀 지시문"], ["styles/", "그림체"], ["formats/", "포맷"], ["channels/", "채널"]].map(
          ([prefix, label]) => html`<div class="col gap-s"><div class="label">${label}</div><div class="row gap-s wrap">${group(prefix).map((f) => html`<button class="chip line" style="border:0;height:28px" onClick=${() => setEditing(f)}><${Icon} name="edit" size="s" />${f.replace(prefix, "")}</button>`)}</div></div>`,
        )}
      </div>
    </div>

    ${editing ? html`<${ConfigEditor} path=${editing} onClose=${() => setEditing(null)} />` : null}
  </div>`;
}
