// Shared building blocks used across the dashboard views.
import { html, useState, useEffect, useRef, Icon, STATUS, api, useAction, toast, timeAgo } from "/lib.js";

export function StatusBadge({ project }) {
  const s = STATUS[project.status] || STATUS.active;
  return html`<span class=${`badge ${s.cls}`}><${Icon} name=${s.icon} size="s" />${s.label}</span>`;
}

export function FormatChip({ format }) {
  if (!format) return null;
  const tall = format.canvas.height > format.canvas.width;
  return html`<span class="chip"><span class="ratio" style=${`width:${tall ? 8 : 14}px;height:${tall ? 13 : 9}px;border-width:1.5px`}></span>${format.name}</span>`;
}

export function StyleChip({ style }) {
  if (!style) return null;
  const p = style.palette || {};
  return html`<span class="chip"><span style=${`width:10px;height:10px;border-radius:50%;background:${p.accent};box-shadow:0 0 0 1.5px ${p.bg}`}></span>${style.name}</span>`;
}

/** Where a project currently is, in words. */
export function currentStage(project, pipeline) {
  const defs = pipeline.stages;
  const pick = (status) => defs.filter((d) => project.stages[d.id]?.status === status);
  const failed = pick("failed")[0];
  if (failed) return { def: failed, st: project.stages[failed.id], text: `${failed.title} 오류` };
  const running = pick("running");
  if (running.length) {
    const lead = running[0];
    const st = project.stages[lead.id];
    return { def: lead, st, running, text: running.map((d) => d.title).join(" · "), label: st.label };
  }
  const waiting = pick("waiting")[0];
  if (waiting) return { def: waiting, st: project.stages[waiting.id], text: `${waiting.title} 대기` };
  if (project.status === "done") return { text: "모든 단계 완료" };
  if (project.status === "canceled") return { text: project.endedReason || "중단됨" };
  const next = defs.find((d) => (project.stages[d.id]?.status || "pending") === "pending");
  return { def: next, text: next ? `${next.title} 대기열` : "" };
}

/** The pipeline as a row of nodes — circles are AI stages, diamonds are approval gates. `fluid` stretches it and labels the phases. */
export function Track({ project, pipeline, fluid }) {
  const byPhase = pipeline.phases.map((ph) => ({ name: ph, defs: pipeline.stages.filter((s) => s.phase === ph) })).filter((g) => g.defs.length);
  return html`<div class=${`track ${fluid ? "fluid" : ""}`} role="img" aria-label="제작 단계 진행 상황">
    ${byPhase.map((group, gi) => {
      const states = group.defs.map((d) => project.stages[d.id]?.status || "pending");
      const phaseState = states.every((x) => x === "done") ? "done" : states.some((x) => x === "running" || x === "waiting" || x === "failed") ? "now" : "";
      return html`<div class="seg-phase" style=${fluid ? `flex:${group.defs.length} 1 0` : ""}>
        ${gi > 0 ? html`<span class="link gap"></span>` : null}
        <div class="phase-col">
          <div class="nodes">
            ${group.defs.map((def, i) => {
              const st = states[i];
              return html`${i > 0 ? html`<span class=${`link ${st === "done" ? "done" : ""}`}></span>` : null}<span class=${`node ${st} ${def.gate ? "gate" : ""}`} title=${`${def.title} — ${STAGE_WORD[st] || st}`}></span>`;
            })}
          </div>
          ${fluid ? html`<div class=${`phase-tag ${phaseState}`}>${group.name}</div>` : null}
        </div>
      </div>`;
    })}
  </div>`;
}
export const STAGE_WORD = { pending: "대기", running: "진행 중", done: "완료", waiting: "결재 대기", failed: "오류" };

export function progressOf(project, pipeline) {
  const defs = pipeline.stages;
  const done = defs.reduce((n, d) => {
    const st = project.stages[d.id];
    return n + (st?.status === "done" ? 1 : st?.status === "running" ? st.progress * 0.9 : 0);
  }, 0);
  return done / defs.length;
}

export function Switch({ on, onChange, label, disabled }) {
  return html`<button type="button" class=${`switch ${on ? "on" : ""}`} role="switch" aria-checked=${on} aria-label=${label} disabled=${disabled} onClick=${() => onChange(!on)}></button>`;
}

export function Seg({ value, options, onChange }) {
  return html`<div class="seg" role="tablist">
    ${options.map((o) => html`<button type="button" role="tab" aria-selected=${o.value === value} class=${o.value === value ? "on" : ""} onClick=${() => onChange(o.value)}>${o.icon ? html`<${Icon} name=${o.icon} size="s" />` : null}${o.label}${o.count != null ? html`<span class="muted num">${o.count}</span>` : null}</button>`)}
  </div>`;
}

export function Modal({ title, onClose, children, wide, foot }) {
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, []);
  return html`<div class="scrim" onMouseDown=${(e) => e.target === e.currentTarget && onClose()}>
    <div class=${`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label=${title}>
      <div class="modal-head"><h2>${title}</h2><button class="btn ghost icon" aria-label="닫기" onClick=${onClose}><${Icon} name="x" /></button></div>
      <div class="modal-body">${children}</div>
      ${foot ? html`<div class="modal-foot">${foot}</div>` : null}
    </div>
  </div>`;
}

/** Edit any file under studio/ in place. Saving applies immediately (the server hot-reloads). */
export function ConfigEditor({ path, title, onClose, template }) {
  const [text, setText] = useState(template ?? null);
  const [busy, run] = useAction();
  useEffect(() => {
    if (template != null) return;
    api(`/api/config/file?path=${encodeURIComponent(path)}`).then((r) => setText(r.content)).catch((e) => {
      toast(e.message, "bad");
      onClose();
    });
  }, [path]);
  const save = () =>
    run(async () => {
      await api("/api/config/file", { method: "PUT", body: { path, content: text } });
      onClose();
    });
  return html`<${Modal} title=${title || path} onClose=${onClose} wide foot=${html`<span class="hint grow">저장하면 바로 다음 작업부터 적용됩니다 · <code class="k">studio/${path}</code></span><button class="btn" onClick=${onClose}>취소</button><button class="btn primary" disabled=${busy || text == null} onClick=${save}>저장</button>`}>
    ${text == null ? html`<div class="empty">불러오는 중…</div>` : html`<textarea class="textarea code" spellcheck="false" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>`}
  <//>`;
}

export function Meter({ value, max = 100, low }) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return html`<div class=${`meter ${low ? "low" : ""}`} role="meter" aria-valuenow=${value} aria-valuemin="0" aria-valuemax=${max}><i style=${`width:${pct}%`}></i></div>`;
}

/**
 * Single-series line chart with a hover crosshair. One series → no legend;
 * the card title says what is plotted. `refLine` draws a labelled threshold.
 */
export function LineChart({ points, yMin = 50, yMax = 100, refLine, height = 190, format = (v) => v }) {
  const wrap = useRef(null);
  const [hover, setHover] = useState(null);
  const [W, setW] = useState(640);
  // Draw at the container's real width so labels stay a readable size on phones.
  useEffect(() => {
    if (!wrap.current) return undefined;
    const ro = new ResizeObserver(([entry]) => setW(Math.max(260, Math.round(entry.contentRect.width))));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, [points.length > 0]);
  const H = height;
  const pad = { l: 34, r: 16, t: 12, b: 24 };
  const iw = W - pad.l - pad.r;
  const ih = H - pad.t - pad.b;
  const n = points.length;
  const x = (i) => pad.l + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v) => pad.t + (1 - (Math.max(yMin, Math.min(yMax, v)) - yMin) / (yMax - yMin)) * ih;
  const ticks = [];
  const step = (yMax - yMin) / 5;
  for (let v = yMin; v <= yMax + 0.001; v += step) ticks.push(Math.round(v));
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.y).toFixed(1)}`).join(" ");
  const area = n > 1 ? `${line} L${x(n - 1).toFixed(1)},${pad.t + ih} L${x(0).toFixed(1)},${pad.t + ih} Z` : "";

  const onMove = (e) => {
    const box = wrap.current.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * W;
    let best = 0;
    for (let i = 1; i < n; i++) if (Math.abs(x(i) - px) < Math.abs(x(best) - px)) best = i;
    setHover(best);
  };
  if (!n) return html`<div class="empty small">아직 표시할 데이터가 없습니다</div>`;
  const hp = hover != null ? points[hover] : null;
  return html`<div class="chart" ref=${wrap} onMouseMove=${onMove} onMouseLeave=${() => setHover(null)} onTouchStart=${(e) => onMove(e.touches[0])}>
    <svg viewBox=${`0 0 ${W} ${H}`} role="img" aria-label="추이 그래프">
      ${ticks.map((t) => html`<line class="grid-line" x1=${pad.l} x2=${W - pad.r} y1=${y(t)} y2=${y(t)} /><text class="axis" x=${pad.l - 8} y=${y(t) + 4} text-anchor="end">${t}</text>`)}
      ${refLine ? html`<line class="ref" x1=${pad.l} x2=${W - pad.r} y1=${y(refLine.value)} y2=${y(refLine.value)} /><text class="axis" x=${W - pad.r} y=${y(refLine.value) - 5} text-anchor="end">${refLine.label}</text>` : null}
      ${area ? html`<path class="area" d=${area} />` : null}
      <path class="line" d=${line} />
      ${hp ? html`<line class="cross" x1=${x(hover)} x2=${x(hover)} y1=${pad.t} y2=${pad.t + ih} />` : null}
      ${points.map((p, i) => (i === n - 1 || i === hover || n <= 12 ? html`<circle class="pt" cx=${x(i)} cy=${y(p.y)} r=${i === hover ? 5.5 : 4} />` : null))}
      <text class="axis" x=${x(n - 1)} y=${y(points[n - 1].y) - 10} text-anchor="end" style="fill:var(--ink);font-weight:700;font-size:12px">${format(points[n - 1].y)}</text>
    </svg>
    ${hp ? html`<div class="tip" style=${`left:${(x(hover) / W) * 100}%;top:${(y(hp.y) / H) * 100}%`}><b>${format(hp.y)}</b> · ${hp.label}${hp.sub ? html`<br /><span style="opacity:.75">${hp.sub}</span>` : null}</div>` : null}
  </div>`;
}

export function Poster({ project, format }) {
  const tall = format ? format.canvas.height > format.canvas.width : true;
  const src = project.poster || project.thumbnail;
  return html`<div class=${`thumb ${tall ? "v" : "h"}`} style=${src ? `background-image:url("${src}")` : ""}></div>`;
}

export function Empty({ icon = "film", title, children }) {
  return html`<div class="empty col" style="align-items:center"><${Icon} name=${icon} size="l" /><h3>${title}</h3><div class="small">${children}</div></div>`;
}

export function Ago({ ts }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 30_000);
    return () => clearInterval(t);
  }, []);
  return html`<span class="muted tiny nowrap">${timeAgo(ts)}</span>`;
}

/** Agent icon name for an activity entry. */
export function activityIcon(a, agents) {
  if (a.kind === "gate") return "inbox";
  if (a.kind === "config") return "settings";
  if (a.kind === "learning") return "brain";
  if (a.kind === "metrics") return "chart";
  if (a.kind === "budget") return "alert";
  if (a.kind === "provider") return "link";
  if (a.kind === "project") return "plus";
  return agents.find((g) => g.id === a.agentId)?.icon || "robot";
}
