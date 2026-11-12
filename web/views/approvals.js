// 결재함 — the only two places a person decides: the plan, and the finished video.
import { html, useState, useEffect, Icon, api, go, useAction, onProjectEvent, duration, PLATFORM, dateTime, safeUrl } from "/lib.js";
import { FormatChip, StyleChip, Meter, Empty, Switch } from "/ui.js";
import { pendingGates, ApprovalCard } from "/views/dashboard.js";

export function useProject(id) {
  const [project, setProject] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let alive = true;
    let timer;
    const load = () =>
      api(`/api/projects/${id}`)
        .then((p) => alive && (setProject(p), setError(null)))
        .catch((e) => alive && setError(e.message));
    setProject(null);
    load();
    const off = onProjectEvent((pid) => {
      if (pid !== id) return;
      clearTimeout(timer);
      timer = setTimeout(load, 250);
    });
    return () => {
      alive = false;
      clearTimeout(timer);
      off();
    };
  }, [id]);
  return [project, error];
}

export function Approvals({ state }) {
  const gates = pendingGates(state);
  return html`<div class="page">
    <div class="page-head"><div><h1>결재함</h1><p>AI 팀은 기획과 업로드, 두 지점에서만 멈춰 결재를 기다립니다.</p></div></div>
    ${gates.length
      ? html`<div class="col">${gates.map((item) => html`<${ApprovalCard} item=${item} state=${state} />`)}</div>`
      : html`<div class="card"><${Empty} icon="inbox" title="결재할 것이 없습니다">새 기획안이나 완성본이 올라오면 여기에 표시됩니다.<//></div>`}
  </div>`;
}

/** Shared decision bar: approve / request changes / reject. */
function DecisionBar({ project, gate, approveLabel, options, note }) {
  const [mode, setMode] = useState(null); // null | "revise" | "reject"
  const [comment, setComment] = useState("");
  const [busy, run] = useAction();
  const send = (decision) =>
    run(async () => {
      await api(`/api/projects/${project.id}/gates/${gate}`, { method: "POST", body: { decision, comment, options: decision === "approve" ? options : {} } });
      go(decision === "approve" ? `projects/${project.id}` : "approvals");
    }, decision === "approve" ? "승인했습니다 — AI 팀이 이어서 진행합니다" : decision === "revise" ? "수정 요청을 전달했습니다" : "반려했습니다");

  if (mode) {
    return html`<div class="sticky-actions" style="flex-direction:column;align-items:stretch">
      <div class="field">
        <label>${mode === "revise" ? "무엇을 바꾸면 될까요? (이 내용은 팀의 학습 노트에도 반영됩니다)" : "반려 사유 (선택 — 적어 주면 팀이 다음 기획에 반영합니다)"}</label>
        <textarea class="textarea" rows="3" autofocus placeholder=${mode === "revise" ? "예: 제목이 너무 평범해요. 숫자를 넣고 더 구체적으로." : "예: 이 주제는 채널과 맞지 않아요."} value=${comment} onInput=${(e) => setComment(e.target.value)}></textarea>
      </div>
      <div class="row" style="justify-content:flex-end">
        <button class="btn" onClick=${() => setMode(null)}>취소</button>
        <button class=${`btn ${mode === "reject" ? "danger" : "primary"}`} disabled=${busy || (mode === "revise" && !comment.trim())} onClick=${() => send(mode)}>${mode === "revise" ? "수정 요청 보내기" : "반려 확정"}</button>
      </div>
    </div>`;
  }
  return html`<div class="sticky-actions">
    <div class="grow small ink2">${note}</div>
    <button class="btn danger" disabled=${busy} onClick=${() => setMode("reject")}><${Icon} name="x" size="s" />반려</button>
    <button class="btn" disabled=${busy} onClick=${() => setMode("revise")}><${Icon} name="edit" size="s" />수정 요청</button>
    <button class="btn good lg" disabled=${busy} onClick=${() => send("approve")}><${Icon} name="check" />${approveLabel}</button>
  </div>`;
}

function Section({ title, children, right }) {
  return html`<div class="card"><div class="card-head"><h2>${title}</h2>${right}</div><div class="card-body col">${children}</div></div>`;
}

function PlanApproval({ project, state }) {
  const plan = project.artifacts.plan;
  const research = project.artifacts.research;
  const format = state.formats.find((f) => f.id === project.brief.format);
  const [title, setTitle] = useState(plan.title_options[0] || plan.working_title);
  const [styleId, setStyleId] = useState(plan.style_id);
  const [note, setNote] = useState("");
  const style = state.styles.find((s) => s.id === styleId);
  const total = plan.structure.reduce((n, b) => n + b.seconds, 0) || 1;
  const feedback = project.gatesFull.plan?.history?.filter((h) => h.decision === "revise").at(-1);

  return html`
    ${plan.changes_from_feedback ? html`<div class="banner info"><${Icon} name="refresh" /><div><b>수정 요청 반영</b> — ${plan.changes_from_feedback}${feedback ? html`<div class="small" style="opacity:.8;margin-top:2px">요청: “${feedback.comment}”</div>` : null}</div></div>` : null}
    ${project.stages.plan.simulated ? html`<div class="banner warn"><${Icon} name="alert" /><div>이 기획안은 <b>시뮬레이션 두뇌</b>가 템플릿으로 쓴 것입니다. 실제 리서치와 판단은 Anthropic 키를 연결하면 동작합니다.</div></div>` : null}

    <div class="split">
      <div class="col gap-l">
        <${Section} title="한 줄 기획">
          <div style="font-size:18px;font-weight:700;letter-spacing:-0.02em;line-height:1.4">${plan.logline}</div>
          <dl class="kv">
            <dt>시청자 약속</dt><dd>${plan.viewer_promise}</dd>
            <dt>타깃</dt><dd>${plan.target_audience}</dd>
            <dt>각도 선택 이유</dt><dd>${plan.angle_reason}</dd>
            <dt>길이</dt><dd class="num">${duration(plan.duration_sec)} · ${format?.name}</dd>
            <dt>CTA</dt><dd>${plan.cta}</dd>
          </dl>
        <//>

        <${Section} title="첫 3초 (훅)" right=${html`<span class="chip">${plan.hook.technique}</span>`}>
          <div style="font-size:17px;font-weight:700;line-height:1.45">“${plan.hook.line}”</div>
          <div class="ink2"><${Icon} name="eye" size="s" /> ${plan.hook.visual}</div>
        <//>

        <${Section} title="제목 후보 — 하나를 고르세요">
          <div class="col gap-s">
            ${plan.title_options.map(
              (t) => html`<button class=${`pick ${t === title ? "on" : ""}`} onClick=${() => setTitle(t)}>
                <span class="dot" style=${t === title ? "background:var(--accent)" : ""}></span><span class="grow" style="font-weight:650">${t}</span><span class="muted tiny num">${t.length}자</span>
              </button>`,
            )}
          </div>
          <div class="hint">패키징 디자이너가 완성된 대본을 보고 한 번 더 다듬습니다. 최종 제목은 업로드 결재에서 직접 고칠 수 있습니다.</div>
        <//>

        <${Section} title="구성" right=${html`<span class="muted small num">${plan.structure.length}비트 · ${duration(total)}</span>`}>
          <div class="col gap-s">
            ${plan.structure.map(
              (b) => html`<div class="row top">
                <span class="chip num" style="min-width:48px;justify-content:center">${b.seconds}초</span>
                <div class="grow"><b>${b.beat}</b> <span class="ink2">— ${b.purpose}</span><div class="meter" style="margin-top:6px;height:4px"><i style=${`width:${(b.seconds / total) * 100}%`}></i></div></div>
              </div>`,
            )}
          </div>
          <hr class="divider" />
          <div class="label">반드시 들어갈 내용</div>
          <ul style="margin:0;padding-left:18px" class="col gap-s">${plan.key_points.map((k) => html`<li>${k}</li>`)}</ul>
        <//>
      </div>

      <div class="col gap-l">
        <${Section} title="그림체">
          <div class="style-preview" style=${`border-radius:12px;background:${style.palette.bg};color:${style.palette.text}`}>
            <div class="t" style=${`font-family:'${style.fonts.display}','Pretendard'`}>${plan.thumbnail_concept.text}</div>
            <div class="c" style=${`background:${style.palette.accent};color:${style.palette.bg}`}>${style.name}</div>
          </div>
          <div class="ink2 small">${styleId === plan.style_id ? plan.style_reason : style.summary}</div>
          <div class="field"><label>다른 그림체로 바꾸기</label>
            <select class="select" value=${styleId} onChange=${(e) => setStyleId(e.target.value)}>${state.styles.map((s) => html`<option value=${s.id}>${s.name}${s.id === plan.style_id ? " (프로듀서 추천)" : ""}</option>`)}</select>
          </div>
        <//>

        <${Section} title="예상 성과" right=${html`<span class="chip">확신도 ${{ low: "낮음", medium: "보통", high: "높음" }[plan.predicted.confidence]}</span>`}>
          <div class="grid g2">
            <div><div class="label">평균 시청 지속률</div><div class="tile" style="padding:0"><div class="v num">${plan.predicted.retention_pct}<small>%</small></div></div></div>
            ${plan.predicted.ctr_pct ? html`<div><div class="label">클릭률</div><div class="tile" style="padding:0"><div class="v num">${plan.predicted.ctr_pct}<small>%</small></div></div></div>` : null}
          </div>
          <div class="ink2 small">${plan.predicted.rationale}</div>
        <//>

        <${Section} title="썸네일 방향">
          <dl class="kv"><dt>문구</dt><dd style="font-weight:700">${plan.thumbnail_concept.text}</dd><dt>화면</dt><dd>${plan.thumbnail_concept.visual}</dd></dl>
        <//>

        ${plan.risks.length ? html`<${Section} title="확인이 필요한 점"><ul style="margin:0;padding-left:18px" class="col gap-s">${plan.risks.map((r) => html`<li>${r}</li>`)}</ul><//>` : null}

        <${Section} title="리서치 근거">
          <div class="ink2 small">${research.summary}</div>
          ${research.angles.map((a) => html`<div class="col gap-s"><div class="row gap-s"><span class=${`badge ${a.potential === "high" ? "done" : "idle"}`}>${{ high: "유망", medium: "보통", low: "낮음" }[a.potential]}</span><b class="grow">${a.title}</b></div><div class="muted small">${a.why_now}</div></div>`)}
          ${research.benchmarks?.length ? html`<hr class="divider" /><div class="label">벤치마크 (조회수 10만 이상)</div>${research.benchmarks.slice(0, 5).map((b) => html`<a class="row small" href=${safeUrl(b.url)} target="_blank" rel="noreferrer"><span class="chip num">${Math.round(b.views / 10000)}만</span><span class="grow clip">${b.title}</span><${Icon} name="external" size="s" /></a>`)}` : null}
          ${research.sources?.length ? html`<hr class="divider" /><div class="label">출처</div>${research.sources.slice(0, 6).map((s) => html`<a class="row small" href=${safeUrl(s.url)} target="_blank" rel="noreferrer"><span class="grow clip">${s.title}</span><${Icon} name="external" size="s" /></a>`)}` : null}
        <//>

        <div class="card pad field"><label>팀에 전할 말 (선택)</label><textarea class="textarea" rows="2" placeholder="예: 말투는 조금 더 가볍게, 전문용어는 풀어서" value=${note} onInput=${(e) => setNote(e.target.value)}></textarea></div>
      </div>
    </div>

    <${DecisionBar} project=${project} gate="plan" approveLabel="승인하고 제작 시작" options=${{ title, style: styleId, note }} note="승인하면 대본부터 편집·검수까지 자동으로 진행되고, 완성본이 업로드 결재로 올라옵니다." />
  `;
}

export function QaReport({ qa }) {
  const round = qa.rounds.at(-1);
  return html`<div class="col">
    <div class="row top gap-l wrap">
      <div>
        <div class="score num"><b>${qa.score}</b><span class="muted">/ 100</span></div>
        <div class="row gap-s" style="margin-top:6px">${qa.passed ? html`<span class="badge done"><${Icon} name="check" size="s" />합격 (기준 ${qa.passScore})</span>` : html`<span class="badge fail"><${Icon} name="alert" size="s" />기준 미달 (${qa.passScore}점)</span>`}${qa.rounds.length > 1 ? html`<span class="chip">재작업 ${qa.rounds.length - 1}회 거침</span>` : null}</div>
      </div>
      <div class="grow ink2" style="min-width:220px">${round.report.summary}</div>
    </div>
    <div class="col gap-s">
      ${round.scores.map(
        (s) => html`<div class="col" style="gap:4px">
          <div class="row between small"><span style="font-weight:650">${s.name} <span class="muted tiny">가중치 ${s.weight}</span></span><span class="num" style="font-weight:700">${s.score}${s.score < qa.passScore ? html` <span class="badge fail" style="height:18px;padding:0 6px;font-size:11px">미달</span>` : null}</span></div>
          <${Meter} value=${s.score} low=${s.score < qa.passScore} />
          <div class="muted tiny">${s.note}</div>
        </div>`,
      )}
    </div>
    ${round.report.strengths.length ? html`<div><div class="label" style="margin-bottom:4px">잘된 점</div><ul style="margin:0;padding-left:18px" class="small col gap-s">${round.report.strengths.map((x) => html`<li>${x}</li>`)}</ul></div>` : null}
    ${round.report.issues.length ? html`<div><div class="label" style="margin-bottom:4px">남은 지적</div><ul style="margin:0;padding-left:18px" class="small col gap-s">${round.report.issues.map((x) => html`<li><b>${x.problem}</b> → ${x.fix}</li>`)}</ul></div>` : null}
    ${round.tech.problems.length ? html`<div class="banner bad"><${Icon} name="alert" /><div>${round.tech.problems.join(" / ")}</div></div>` : html`<div class="muted tiny num">실측 규격 — ${round.tech.width}×${round.tech.height} · ${round.tech.duration}초 · ${round.tech.fps}fps${round.tech.loudness != null ? ` · ${round.tech.loudness} LUFS` : ""}</div>`}
  </div>`;
}

function UploadApproval({ project, state }) {
  const a = project.artifacts;
  const pkg = a.package;
  const format = state.formats.find((f) => f.id === project.brief.format);
  const tall = format.canvas.height > format.canvas.width;
  const live = state.studio.publish.live === true && state.studio.mode !== "simulation";
  const [title, setTitle] = useState(pkg.title);
  const [description, setDescription] = useState(pkg.description);
  const [targets, setTargets] = useState(project.brief.targets);
  const [privacy, setPrivacy] = useState(state.studio.publish.default_privacy || "private");
  const [publishAt, setPublishAt] = useState("");
  const [disclose, setDisclose] = useState(state.studio.publish.disclose_ai !== false);
  const allTargets = tall ? ["youtube_shorts", "instagram_reels"] : ["youtube"];
  const toggle = (t) => setTargets((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));
  const options = { title, description, targets, privacy, publishAt: publishAt ? new Date(publishAt).toISOString() : null, disclose };

  return html`
    ${project.placeholders.length ? html`<div class="banner warn"><${Icon} name="alert" /><div><b>대체 엔진으로 만든 구간이 있습니다: ${project.placeholders.join(", ")}.</b> 해당 AI가 연결되지 않아 자리만 채운 결과물입니다. 흐름 확인용으로만 쓰고, 실제 업로드는 자동으로 막힙니다.</div></div>` : null}
    ${a.qa.belowBar ? html`<div class="banner bad"><${Icon} name="alert" /><div><b>품질 기준(${a.qa.passScore}점)에 못 미쳤습니다.</b> 자동 재작업 한도를 다 써서 결재로 올렸습니다. 아래 지적을 보고 수정 요청하거나 그대로 승인할 수 있습니다.</div></div>` : null}

    <div class="split" style=${tall ? "grid-template-columns:minmax(260px,340px) minmax(0,1fr)" : ""}>
      <div class="col gap-l">
        <div class=${`player ${tall ? "v" : ""}`}><video src=${a.edit.url} poster=${a.edit.posterUrl} controls playsinline preload="metadata"></video></div>
        <div class="card pad col">
          <div class="label">썸네일</div>
          <img src=${pkg.thumbnailUrl} alt="썸네일" style=${`width:${tall ? "150px" : "100%"};border-radius:10px;display:block`} />
          <div class="muted small">“${pkg.thumbnail.headline}”${pkg.thumbnail.sub ? ` · ${pkg.thumbnail.sub}` : ""}</div>
        </div>
      </div>

      <div class="col gap-l">
        <${Section} title="품질 검수 리포트"><${QaReport} qa=${a.qa} /><//>

        <${Section} title="게시 정보">
          <div class="field"><label>제목 <span class="muted num">${title.length}${format.metadata?.title_max ? `/${format.metadata.title_max}` : ""}자</span></label><input class="input" value=${title} onInput=${(e) => setTitle(e.target.value)} /></div>
          <div class="field"><label>설명</label><textarea class="textarea" rows="5" value=${description} onInput=${(e) => setDescription(e.target.value)}></textarea></div>
          <div class="row gap-s wrap">${pkg.hashtags.map((h) => html`<span class="chip">#${h}</span>`)}${pkg.tags.slice(0, 6).map((t) => html`<span class="chip line">${t}</span>`)}</div>
          ${pkg.instagram_caption && targets.includes("instagram_reels") ? html`<div class="field"><label>인스타그램 캡션</label><div class="card pad small pre" style="background:var(--surface-2)">${pkg.instagram_caption}</div></div>` : null}
        <//>

        <${Section} title="업로드 설정">
          <div class="field"><label>올릴 곳</label>
            <div class="row wrap">${allTargets.map((t) => html`<button class=${`pick ${targets.includes(t) ? "on" : ""}`} onClick=${() => toggle(t)}><${Icon} name=${targets.includes(t) ? "check" : "plus"} size="s" />${PLATFORM[t]}</button>`)}</div>
          </div>
          <div class="grid g2">
            <div class="field"><label>공개 범위 (유튜브)</label><select class="select" value=${privacy} onChange=${(e) => setPrivacy(e.target.value)}><option value="private">비공개</option><option value="unlisted">일부 공개</option><option value="public">공개</option></select></div>
            <div class="field"><label>예약 공개 (선택)</label><input class="input" type="datetime-local" value=${publishAt} onInput=${(e) => setPublishAt(e.target.value)} /></div>
          </div>
          <div class="row between"><div><div style="font-weight:650">AI 생성 콘텐츠로 표시</div><div class="hint">유튜브·인스타그램의 AI 콘텐츠 공개 항목에 표시합니다</div></div><${Switch} on=${disclose} onChange=${setDisclose} label="AI 생성 콘텐츠 표시" /></div>
          ${live
            ? html`<div class="banner info"><${Icon} name="upload" /><div>승인하면 <b>실제로 업로드</b>됩니다.</div></div>`
            : html`<div class="banner info"><${Icon} name="shield" /><div><b>실제 업로드는 꺼져 있습니다.</b> 승인하면 영상·썸네일·게시 정보를 묶은 업로드 패키지만 <code class="k">data/outbox</code>에 저장합니다. 켜려면 <a href="#/settings" style="text-decoration:underline">설정</a>에서 계정을 연결하고 ‘실제 업로드’를 켜세요.</div></div>`}
        <//>
      </div>
    </div>

    <${DecisionBar} project=${project} gate="upload" approveLabel=${live ? "승인하고 업로드" : "승인 (패키지 저장)"} options=${options} note=${targets.length ? `${targets.map((t) => PLATFORM[t]).join(" · ")}${publishAt ? ` · ${dateTime(new Date(publishAt).getTime())} 예약` : ""}` : "올릴 곳을 하나 이상 고르세요"} />
  `;
}

export function ApprovalDetail({ id, state }) {
  const [project, error] = useProject(id);
  if (error) return html`<div class="page"><div class="card"><${Empty} icon="alert" title="불러오지 못했습니다">${error}<//></div></div>`;
  if (!project) return html`<div class="page"><div class="empty">불러오는 중…</div></div>`;
  const gate = Object.entries(project.gates).find(([, g]) => g.status === "pending")?.[0];
  const format = state.formats.find((f) => f.id === project.brief.format);
  const style = state.styles.find((s) => s.id === project.styleId);
  return html`<div class="page">
    <div class="col gap-s">
      <a class="row gap-s muted small" href="#/approvals"><${Icon} name="back" size="s" />결재함</a>
      <div class="row gap-s wrap"><span class="badge wait"><${Icon} name="inbox" size="s" />${gate === "plan" ? "기획 결재" : gate === "upload" ? "업로드 결재" : "결재 없음"}</span><${FormatChip} format=${format} /><${StyleChip} style=${style} /></div>
      <h1>${project.title}</h1>
    </div>
    ${gate === "plan" ? html`<${PlanApproval} project=${project} state=${state} key=${project.gates.plan.rounds} />` : gate === "upload" ? html`<${UploadApproval} project=${project} state=${state} key=${project.artifacts.edit?.version} />` : html`<div class="card"><${Empty} icon="check" title="이 영상은 지금 결재할 것이 없습니다"><a class="btn" href=${`#/projects/${project.id}`}>프로젝트 보기</a><//></div>`}
  </div>`;
}
