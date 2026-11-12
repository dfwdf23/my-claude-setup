// 라이브러리 — art styles, formats and channel profiles. All of it is plain files you can add to or edit.
import { html, useState, Icon, api, useAction, duration, PLATFORM } from "/lib.js";
import { ConfigEditor, Switch, Modal } from "/ui.js";

function StyleCard({ style, onEdit }) {
  const p = style.palette || {};
  return html`<div class="card col" style="gap:0;overflow:hidden">
    <div class="style-preview" style=${`background:${p.bg};color:${p.text}`}>
      <div style=${`position:absolute;inset:0;background:radial-gradient(circle at 78% 22%, ${p.accent2}55, transparent 46%),radial-gradient(circle at 16% 84%, ${p.accent}44, transparent 42%)`}></div>
      <div class="t" style=${`position:relative;font-family:'${style.fonts?.display}','Pretendard'`}>세상에 없던 이야기</div>
      <div class="c" style=${`position:relative;background:${style.captions?.box ? p.accent : "transparent"};color:${style.captions?.box ? p.bg : p.accent};font-family:'${style.fonts?.display}','Pretendard'`}>자막은 이렇게</div>
    </div>
    <div class="col pad" style="padding:14px 16px">
      <div class="row between"><h3>${style.name}</h3><div class="swatches">${["bg", "surface", "accent", "accent2", "text"].map((k) => html`<i style=${`background:${p[k]}`}></i>`)}</div></div>
      <div class="ink2 small">${style.summary}</div>
      <div class="row gap-s wrap">${(style.best_for || []).slice(0, 5).map((b) => html`<span class="chip">${b}</span>`)}</div>
      <div class="row between" style="margin-top:4px"><span class="muted tiny clip">${style.fonts?.display} · ${style.music?.genre}</span><button class="btn ghost sm" onClick=${() => onEdit(style)}><${Icon} name="edit" size="s" />편집</button></div>
    </div>
  </div>`;
}

function FormatCard({ format, onEdit }) {
  const tall = format.canvas.height > format.canvas.width;
  const mix = format.visual_mix || {};
  return html`<div class="card pad col">
    <div class="row top">
      <div class="ratio" style=${`width:${tall ? 27 : 54}px;height:${tall ? 48 : 30}px`}></div>
      <div class="grow"><h3>${format.name}</h3><div class="ink2 small">${format.summary}</div></div>
    </div>
    <dl class="kv small">
      <dt>화면</dt><dd class="num">${format.canvas.width}×${format.canvas.height} · ${format.canvas.fps}fps</dd>
      <dt>길이</dt><dd class="num">${duration(format.duration.min)} ~ ${duration(format.duration.max)}</dd>
      <dt>컷</dt><dd class="num">평균 ${format.shot.target_sec}초</dd>
      <dt>올리는 곳</dt><dd>${format.platforms.map((p) => PLATFORM[p] || p).join(", ")}</dd>
      <dt>장면 구성</dt><dd>영상 ${Math.round((mix.video || 0) * 100)}% · 이미지 ${Math.round((mix.image || 0) * 100)}% · 모션 ${Math.round((mix.motion || 0) * 100)}%</dd>
    </dl>
    <div class="row" style="justify-content:flex-end;margin-top:auto"><button class="btn ghost sm" onClick=${() => onEdit(format)}><${Icon} name="edit" size="s" />편집</button></div>
  </div>`;
}

function ChannelCard({ channel, state, onEdit }) {
  const [busy, run] = useAction();
  const auto = channel.autopilot || {};
  const set = (autopilot) => run(() => api("/api/settings", { method: "POST", body: { autopilot: { channel: channel.id, ...autopilot } } }));
  return html`<div class="card pad col">
    <div class="row between"><h3>${channel.name}</h3><button class="btn ghost sm" onClick=${() => onEdit(channel)}><${Icon} name="edit" size="s" />편집</button></div>
    <dl class="kv small"><dt>주제</dt><dd>${channel.niche}</dd><dt>시청자</dt><dd>${channel.audience}</dd><dt>말투</dt><dd>${channel.tone}</dd>${channel.avoid?.length ? html`<dt>피할 것</dt><dd>${channel.avoid.join(" / ")}</dd>` : null}</dl>
    <hr class="divider" />
    <div class="row between">
      <div><div style="font-weight:700">오토파일럿</div><div class="hint">켜 두면 AI가 매일 주제를 찾아 기획안을 결재함에 올립니다</div></div>
      <${Switch} on=${Boolean(auto.enabled)} disabled=${busy} onChange=${(v) => set({ enabled: v })} label="오토파일럿" />
    </div>
    <div class="row wrap">
      ${state.formats.map(
        (f) => html`<label class="row gap-s small"><span class="ink2">${f.name}</span>
          <select class="select" style="width:104px;min-height:32px;padding:4px 8px" value=${String(auto.daily?.[f.id] || 0)} disabled=${busy} onChange=${(e) => set({ daily: { [f.id]: Number(e.target.value) } })}>${[0, 1, 2, 3, 5].map((n) => html`<option value=${String(n)}>하루 ${n}편</option>`)}</select>
        </label>`,
      )}
    </div>
  </div>`;
}

function NewStyle({ state, onClose, onCreated }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [base, setBase] = useState(state.styles[0].id);
  const [busy, run] = useAction();
  const valid = /^[a-z0-9][a-z0-9-]{1,30}$/.test(id) && name.trim() && !state.styles.some((s) => s.id === id);
  const create = () =>
    run(async () => {
      const src = await api(`/api/config/file?path=${encodeURIComponent(`styles/${base}.yaml`)}`);
      const baseStyle = state.styles.find((s) => s.id === base);
      const content = src.content.replace(/^id:.*$/m, `id: ${id}`).replace(/^name:.*$/m, `name: ${name.trim()}`).replace(/^summary:.*$/m, `summary: ${baseStyle.name}에서 출발한 새 그림체 — 이 설명을 고쳐 주세요`);
      await api("/api/config/file", { method: "PUT", body: { path: `styles/${id}.yaml`, content } });
      onCreated(`styles/${id}.yaml`);
    });
  return html`<${Modal} title="새 그림체 추가" onClose=${onClose} foot=${html`<button class="btn" onClick=${onClose}>취소</button><button class="btn primary" disabled=${!valid || busy} onClick=${create}>만들고 편집하기</button>`}>
    <div class="field"><label>이름</label><input class="input" placeholder="예: 레트로 픽셀" value=${name} onInput=${(e) => setName(e.target.value)} /></div>
    <div class="field"><label>파일 id (영문 소문자·숫자·하이픈)</label><input class="input" placeholder="retro-pixel" value=${id} onInput=${(e) => setId(e.target.value.toLowerCase())} /></div>
    <div class="field"><label>어느 그림체에서 출발할까요?</label><select class="select" value=${base} onChange=${(e) => setBase(e.target.value)}>${state.styles.map((s) => html`<option value=${s.id}>${s.name}</option>`)}</select></div>
    <div class="hint">만든 뒤 프롬프트·색·글꼴·음악 분위기를 고치면 다음 영상부터 바로 고를 수 있습니다. 사용할 수 있는 글꼴: ${state.fonts.join(", ")}</div>
  <//>`;
}

export function Library({ state }) {
  const [editing, setEditing] = useState(null);
  const [adding, setAdding] = useState(false);
  return html`<div class="page">
    <div class="page-head"><div><h1>라이브러리</h1><p>그림체·포맷·채널은 모두 파일 한 개씩입니다. 추가하거나 고치면 즉시 반영됩니다.</p></div></div>

    <div class="section-title"><h2>그림체 <span class="muted num">${state.styles.length}</span></h2><button class="btn sm" onClick=${() => setAdding(true)}><${Icon} name="plus" size="s" />새 그림체</button></div>
    <div class="grid auto-fill">${state.styles.map((s) => html`<${StyleCard} style=${s} onEdit=${(x) => setEditing({ path: x.file, title: x.name })} />`)}</div>

    <div class="section-title"><h2>포맷 <span class="muted num">${state.formats.length}</span></h2></div>
    <div class="grid g3">${state.formats.map((f) => html`<${FormatCard} format=${f} onEdit=${(x) => setEditing({ path: x.file, title: x.name })} />`)}</div>

    <div class="section-title"><h2>채널 <span class="muted num">${state.channels.length}</span></h2></div>
    <div class="grid g2">${state.channels.map((c) => html`<${ChannelCard} channel=${c} state=${state} onEdit=${(x) => setEditing({ path: x.file, title: x.name })} />`)}</div>

    ${adding ? html`<${NewStyle} state=${state} onClose=${() => setAdding(false)} onCreated=${(path) => (setAdding(false), setEditing({ path, title: "새 그림체" }))} />` : null}
    ${editing ? html`<${ConfigEditor} path=${editing.path} title=${`${editing.title} — ${editing.path}`} onClose=${() => setEditing(null)} />` : null}
  </div>`;
}
