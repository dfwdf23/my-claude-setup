/**
 * Offline stand-in for the LLM. It answers every task in the same schema the
 * real model uses, from templates — so the whole studio (pipeline, approvals,
 * rendering, learning loop, dashboard) can be exercised with no API key and
 * no cost. Its writing is placeholder quality by design; the dashboard labels
 * anything it produced as "시뮬레이션".
 */

function rng(seed) {
  let s = 0;
  for (const ch of String(seed)) s = (s * 31 + ch.charCodeAt(0)) >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let r = Math.imul(s ^ (s >>> 15), 1 | s);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}
const pick = (r, list) => list[Math.floor(r() * list.length)];
const pad = (n) => String(n).padStart(2, "0");

const AUTO_TOPICS = [
  "잠을 줄이면 뇌에서 벌어지는 일",
  "미루는 습관을 끊는 2분 규칙",
  "커피를 오후에 마시면 생기는 일",
  "복리가 진짜 힘을 내기 시작하는 시점",
  "기억력이 좋은 사람들의 공통 습관",
];

/** A hand-written demo so the default simulation reads like a real short. */
const SLEEP_DEMO = {
  match: /잠|수면/,
  hook: "17시간 깨어 있으면, 뇌는 술 취한 것처럼 움직입니다.",
  promise: "잠이 부족할 때 뇌에서 실제로 벌어지는 세 가지를 40초 안에 이해한다",
  lines: [
    ["훅", "17시간 깨어 있으면, 뇌는 술 취한 것처럼 움직입니다.", "", "새벽 책상 앞에서 꾸벅꾸벅 조는 사람", ["17시간", "술"], null],
    ["전개", "연구에서는 혈중알코올농도 0.05% 수준과 비슷했습니다.", "", "음주 측정기와 뇌 일러스트가 나란히", ["0.05%"], { type: "stat", text: "혈중알코올농도 수준의 판단력 저하", sub: "17~19시간 깨어 있을 때", value: "0.05%", items: [] }],
    ["전개", "잠이 부족하면 뇌에서 세 가지가 무너집니다.", "잠이 부족하면 무너지는 3가지", "금이 가는 뇌 모형", ["세", "가지"], { type: "title", text: "잠이 부족하면 무너지는 3가지", sub: "", value: "", items: [] }],
    ["전개", "첫째, 기억입니다. 낮에 배운 건 자는 동안 저장됩니다.", "", "책장에 책이 정리되는 모습", ["기억"], { type: "badge", text: "① 기억", sub: "", value: "", items: [] }],
    ["전개", "둘째, 청소입니다. 깊은 잠에서 뇌의 노폐물이 씻겨 나갑니다.", "", "물결이 뇌를 씻어내는 장면", ["청소"], { type: "badge", text: "② 청소", sub: "", value: "", items: [] }],
    ["전개", "셋째, 감정입니다. 작은 일에도 쉽게 예민해집니다.", "", "사소한 일에 폭발하는 직장인", ["감정"], { type: "badge", text: "③ 감정", sub: "", value: "", items: [] }],
    ["반전", "문제는, 정작 본인은 멀쩡하다고 느낀다는 겁니다.", "", "거울 앞에서 엄지를 드는 피곤한 얼굴", ["멀쩡하다고"], null],
    ["마무리", "오늘 밤은 30분만 일찍 누워 보세요.", "", "불 꺼진 방, 침대 옆 휴대폰을 내려놓는 손", ["30분만"], { type: "list", text: "오늘 밤 할 일", sub: "", value: "", items: ["30분 일찍 눕기", "휴대폰은 침대 밖에", "같은 시간에 일어나기"] }],
    ["루프", "왜냐하면, 17시간 깨어 있으면", "", "첫 장면으로 돌아가는 시계", [], { type: "cta", text: "잠 얘기 더 듣고 싶다면", sub: "구독", value: "", items: [] }],
  ],
};

function genericLines(topic, long) {
  const t = topic.replace(/[.?!]$/, "");
  const base = [
    ["훅", `${t}. 대부분은 거꾸로 알고 있습니다.`, "", `${t}을(를) 상징하는 장면, 강한 대비`, ["거꾸로"], null],
    ["전개", "핵심은 딱 세 가지입니다.", `${t} 핵심 3가지`, "숫자 3이 크게 떠오르는 화면", ["세", "가지"], { type: "title", text: `${t} 핵심 3가지`, sub: "", value: "", items: [] }],
    ["전개", "첫째, 시작은 생각보다 훨씬 빠릅니다.", "", "출발선에서 튀어 나가는 순간", ["첫째"], { type: "badge", text: "① 시작", sub: "", value: "", items: [] }],
    ["전개", "둘째, 작은 차이가 쌓이면 결과가 완전히 달라집니다.", "", "조금씩 벌어지는 두 개의 선", ["작은", "차이"], { type: "badge", text: "② 누적", sub: "", value: "", items: [] }],
    ["전개", "셋째, 그래서 순서만 바꿔도 충분합니다.", "", "블록의 순서를 바꾸는 손", ["순서만"], { type: "badge", text: "③ 순서", sub: "", value: "", items: [] }],
    ["반전", "어려운 방법이 필요한 게 아니었습니다.", "", "복잡한 설계도를 내려놓는 사람", ["어려운"], null],
    ["마무리", "오늘 하나만 바꿔 보세요.", "", "달력에 동그라미를 치는 손", ["하나만"], { type: "list", text: "오늘 할 일", sub: "", value: "", items: ["하나만 고르기", "지금 바로 시작", "내일 다시 확인"] }],
    ["루프", `그러면 ${t}, 다르게 보일 겁니다.`, "", "첫 장면과 같은 구도, 밝아진 분위기", ["다르게"], { type: "cta", text: "도움이 됐다면", sub: "구독", value: "", items: [] }],
  ];
  if (!long) return base;
  // Long-form simulation: a few chapters of the same shape, enough to exercise chapters and pacing.
  const out = [...base.slice(0, 2)];
  const chapters = ["왜 이런 일이 생길까", "실제로는 어떻게 다를까", "그래서 무엇을 하면 될까"];
  chapters.forEach((title, c) => {
    out.push(["챕터", `${c + 1}장. ${title}.`, "", `${title}을(를) 암시하는 넓은 풍경`, [], { type: "chapter", text: title, sub: `${c + 1}장`, value: "", items: [] }]);
    out.push(["전개", `먼저 ${t}의 배경부터 짚어 보겠습니다.`, "", "오래된 자료를 넘겨보는 손", ["배경"], null]);
    out.push(["전개", "여기서 대부분이 놓치는 지점이 하나 있습니다.", "", "돋보기로 한 점을 확대하는 장면", ["놓치는"], null]);
    out.push(["전개", "숫자로 보면 차이가 더 분명해집니다.", "", "막대가 자라나는 그래프", ["숫자"], { type: "stat", text: "체감 차이 (시뮬레이션 수치)", sub: "", value: `${(c + 2) * 12}%`, items: [] }]);
    out.push(["전개", "다음 장에서는 이걸 뒤집는 사례를 봅니다.", "", "문이 열리며 빛이 들어오는 장면", ["뒤집는"], null]);
  });
  return [...out, ...base.slice(5)];
}

const TASKS = {
  research({ brief, channel }) {
    const r = rng(brief.topic || brief.id || "auto");
    const topic = brief.topic || pick(r, AUTO_TOPICS);
    return {
      summary: `[시뮬레이션] '${topic}'은(는) ${channel?.niche || "이 채널"} 시청자가 반복해서 찾는 주제입니다. 실제 AI를 연결하면 웹 검색과 벤치마크 영상 분석 결과가 여기에 들어갑니다.`,
      angles: [
        { title: `${topic} — 대부분이 거꾸로 알고 있는 사실`, why_now: "통념을 뒤집는 각도는 계절과 무관하게 꾸준히 반응이 좋습니다.", audience: channel?.audience || "가볍게 배우고 싶은 20~40대", evidence: ["추측: 시뮬레이션 모드에서는 실제 근거를 수집하지 않습니다."], potential: "high" },
        { title: `${topic}, 숫자 세 개로 끝내기`, why_now: "숫자로 요약하는 형식은 저장과 공유가 잘 일어납니다.", audience: "핵심만 빨리 알고 싶은 사람", evidence: ["추측: 시뮬레이션"], potential: "medium" },
        { title: `내가 ${topic}을(를) 직접 해 본 결과`, why_now: "체험형 서사는 시청 지속률이 높습니다.", audience: "실제 후기를 원하는 사람", evidence: ["추측: 시뮬레이션"], potential: "medium" },
      ],
      benchmark_patterns: ["제목에 구체적인 숫자 또는 반전 단어", "첫 문장이 결론으로 시작", "한 영상에 메시지 하나"],
      sources: [],
      _topic: topic,
    };
  },

  plan({ brief, research, styles, format, feedback }) {
    const topic = brief.topic || research?._topic || research?.angles?.[0]?.title?.split(" — ")[0] || "오늘의 주제";
    const r = rng(topic + (feedback || ""));
    const demo = SLEEP_DEMO.match.test(topic) ? SLEEP_DEMO : null;
    const style = brief.style && brief.style !== "auto"
      ? styles.find((s) => s.id === brief.style) || styles[0]
      : styles.find((s) => (s.best_for || []).some((k) => topic.includes(k))) || pick(r, styles);
    const long = format.id === "long";
    return {
      working_title: topic,
      title_options: [`${topic}, 대부분이 거꾸로 알고 있습니다`, `${topic} 3가지만 기억하세요`, `아무도 말해주지 않는 ${topic}`],
      logline: `${topic}에 대한 통념을 뒤집고, 오늘 바로 써먹을 한 가지를 준다.`,
      angle_reason: "통념을 뒤집는 각도가 첫 3초 이탈을 가장 잘 막기 때문입니다.",
      target_audience: "가볍게 보지만 하나는 배우고 싶은 20~40대",
      viewer_promise: demo?.promise || `${topic}의 핵심 세 가지를 ${long ? "8분" : "40초"} 안에 이해한다`,
      hook: { line: demo?.hook || `${topic}. 대부분은 거꾸로 알고 있습니다.`, visual: "강한 대비의 클로즈업 위로 핵심 문구가 한 단어씩 찍힌다", technique: "통념 뒤집기" },
      style_id: style.id,
      style_reason: `'${style.name}' — ${style.summary}`,
      duration_sec: long ? 90 : format.duration.target,
      structure: [
        { beat: "훅", purpose: "통념을 뒤집어 멈추게 한다", seconds: 3 },
        { beat: "약속", purpose: "세 가지를 알려준다고 예고", seconds: 4 },
        { beat: "본론", purpose: "세 가지를 하나씩, 화면 전환과 함께", seconds: long ? 60 : 20 },
        { beat: "반전", purpose: "생각보다 쉽다는 것을 보여준다", seconds: 5 },
        { beat: "마무리", purpose: "오늘 할 한 가지 + 루프", seconds: 6 },
      ],
      key_points: ["통념과 실제의 차이", "핵심 세 가지", "오늘 바로 할 수 있는 행동 하나"],
      thumbnail_concept: { text: "거꾸로 알고 있었다", visual: "놀란 표정의 인물 클로즈업과 뒤집힌 화살표" },
      cta: "도움이 됐다면 구독",
      risks: ["시뮬레이션 기획안입니다 — 사실 관계는 확인되지 않았습니다."],
      predicted: { retention_pct: long ? 42 : 68, ctr_pct: long ? 5.5 : 0, confidence: "low", rationale: "시뮬레이션 추정치입니다. 실제 AI 연결 시 벤치마크 근거와 함께 산출됩니다." },
      changes_from_feedback: feedback ? `수정 요청 반영: "${feedback}" — 제목 후보와 훅을 다시 썼습니다.` : "",
    };
  },

  script({ plan, format, revision }) {
    const topic = plan.working_title;
    const demo = SLEEP_DEMO.match.test(topic) ? SLEEP_DEMO.lines : genericLines(topic, format.id === "long");
    const scenes = demo.map(([beat, narration, on_screen_text, visual_idea, emphasis_words], i) => ({
      id: `s${pad(i + 1)}`,
      beat,
      narration: i === 0 && plan.hook?.line && !SLEEP_DEMO.match.test(topic) ? plan.hook.line : narration,
      on_screen_text,
      visual_idea,
      emphasis_words,
    }));
    return { title: plan.title_options?.[0] || topic, scenes, hook_alternatives: [`${topic}, 사실은 정반대입니다.`], notes: revision ? `재작업 지시 반영: ${revision}` : "[시뮬레이션 대본]" };
  },

  script_doctor({ draft }) {
    return { ...draft, notes: `${draft.notes} 첫 문장과 마지막 문장의 연결(루프)을 확인했습니다.`.trim() };
  },

  storyboard({ plan, script, style, format, mix }) {
    const topic = plan.working_title;
    const lines = SLEEP_DEMO.match.test(topic) ? SLEEP_DEMO.lines : genericLines(topic, format.id === "long");
    const r = rng(topic + style.id);
    const cams = ["push_in", "pan_right", "pull_out", "tilt_up", "pan_left", "push_in", "static"];
    const n = script.scenes.length;
    const nVideo = Math.round(n * (mix?.video ?? 0.5));
    const nMotion = Math.round(n * (mix?.motion ?? 0.1));
    const scenes = script.scenes.map((s, i) => {
      const g = lines[i]?.[5] || null;
      // Spend video generation on the hook and the turn; give graphic-heavy beats to motion.
      let mode = "image";
      if (i < Math.ceil(nVideo / 2) || (i >= n - 3 && i < n - 3 + Math.floor(nVideo / 2))) mode = "video";
      if (g && ["title", "stat", "list"].includes(g.type) && nMotion > 0 && i > 0) mode = "motion";
      return {
        id: s.id,
        mode,
        shot: pick(r, ["클로즈업", "미디엄 샷", "와이드 샷", "로우앵글", "탑뷰"]),
        image_prompt: `${s.visual_idea} — (simulation) subject centered, ${format.canvas.width > format.canvas.height ? "wide" : "vertical"} composition`,
        motion_prompt: "subject moves subtly, slow camera push",
        camera: cams[i % cams.length],
        graphics: g ? [g] : [],
        transition: i === 0 ? "cut" : g?.type === "title" ? "flash" : s.beat === "챕터" ? "fade" : "cut",
      };
    });
    return {
      visual_bible: { look: `${style.name} — ${style.summary}`, consistency_tokens: `${style.image_prompt?.prefix || ""} ${style.image_prompt?.suffix || ""}`.trim(), characters: "" },
      scenes,
      music_brief: { mood: style.music?.mood || "차분한", genre: style.music?.genre || "ambient", bpm: style.music?.bpm || 96, prompt: `${style.music?.genre || "ambient"} instrumental, ${style.music?.bpm || 96} bpm, background for narration` },
      voice_brief: { persona: style.voice?.persona || "담백한 내레이터", tone: "차분하지만 지루하지 않게", pace: "보통보다 약간 빠르게" },
    };
  },

  voice_direction({ scenes }) {
    return { scenes: scenes.map((s) => ({ id: s.id, tts_text: s.narration, delivery: s.beat === "훅" ? "단호하게, 첫 단어에 힘" : "자연스럽게" })) };
  },

  package({ plan, script, format, revision }) {
    const topic = plan.working_title;
    const improved = Boolean(revision);
    return {
      title: (improved ? `${topic}, 딱 3가지만 알면 됩니다` : script.title).slice(0, format.metadata?.title_max || 70),
      description: `${plan.viewer_promise}.\n\n${plan.key_points.map((k) => `• ${k}`).join("\n")}\n\n[시뮬레이션으로 작성된 설명입니다]`,
      tags: [topic, "지식", "꿀팁", "쇼츠"].slice(0, 8),
      hashtags: ["지식", "꿀팁", topic.split(" ")[0]].slice(0, format.metadata?.hashtags || 3),
      thumbnail: { headline: improved ? "3가지면 끝" : plan.thumbnail_concept.text, sub: improved ? topic.slice(0, 14) : "", image_prompt: `${plan.thumbnail_concept.visual} (simulation)`, layout: "left" },
      chapters: format.id === "long" ? script.scenes.filter((s) => s.beat === "챕터").map((s) => ({ scene_id: s.id, label: s.narration.replace(/^\d+장\.\s*/, "").replace(/\.$/, "") })) : [],
      instagram_caption: format.platforms?.includes("instagram_reels") ? `${plan.hook.line}\n\n저장해 두고 오늘 밤 해보세요.` : "",
      pinned_comment: "여러분은 몇 번째가 제일 와닿았나요?",
    };
  },

  qa({ rubric, tech, round, projectId, hasPlaceholders }) {
    const r = rng(projectId + ":" + round);
    // In simulation the creative scores are illustrative; the technical row uses real measurements.
    const weakFirstRound = round === 0 && rng(projectId)() < 0.6;
    const scores = rubric.map((c) => {
      let score = 80 + Math.floor(r() * 13);
      let note = "[시뮬레이션 채점] 기준을 충족하는 것으로 가정했습니다.";
      if (c.id === "packaging" && weakFirstRound) {
        score = 68;
        note = "[시뮬레이션 채점] 썸네일 문구가 제목과 같은 내용을 반복합니다.";
      }
      if (c.id === "technical") {
        score = tech.problems.length ? 60 : 96;
        note = tech.problems.length ? tech.problems.join(" / ") : `실측: ${tech.width}×${tech.height}, ${tech.duration.toFixed(1)}초, ${tech.loudness ?? "-"} LUFS`;
      }
      return { id: c.id, score, note };
    });
    const issues = [];
    if (weakFirstRound) issues.push({ stage: "package", scene_id: "", severity: "high", problem: "썸네일 문구가 제목을 반복합니다.", fix: "썸네일 문구는 제목과 다른 정보(숫자 또는 결과)로 바꾸고, 제목은 약속을 더 구체적으로 씁니다." });
    return {
      scores,
      strengths: ["첫 문장이 결론으로 시작합니다.", "장면 전환 간격이 포맷 기준 안에 있습니다."],
      issues,
      predicted: { retention_pct: 60 + Math.floor(r() * 12), ctr_pct: 4 + Math.floor(r() * 30) / 10 },
      summary: hasPlaceholders ? "[시뮬레이션] 대체 엔진으로 만든 구간이 있어 실제 품질 판단은 불가합니다. 흐름 점검용 결과입니다." : "[시뮬레이션] 기준표를 통과한 것으로 가정했습니다.",
    };
  },

  triage({ comment, stages }) {
    const rules = [
      [/썸네일|제목|설명|태그|해시/, "package"],
      [/목소리|성우|발음|읽/, "voice"],
      [/음악|배경음|소리|사운드/, "sound"],
      [/자막|글자|폰트|그래픽/, "motion"],
      [/그림|이미지|화면|영상미|색/, "storyboard"],
      [/대본|내용|훅|문장|길이|구성/, "script"],
    ];
    const hits = rules.filter(([re]) => re.test(comment)).map(([, stage]) => stage).filter((s) => stages.includes(s));
    const targets = (hits.length ? [...new Set(hits)] : ["script"]).map((stage) => ({ stage, instruction: comment }));
    return { targets };
  },

  retro({ trigger, feedback, qa, metrics, format, style }) {
    const lessons = [];
    if (feedback?.comment) {
      lessons.push({ agent: feedback.gate === "plan" ? "producer" : "", format: format || "", style: "", text: `결재자 요청: ${feedback.comment}`, evidence: `${feedback.gate === "plan" ? "기획" : "업로드"} 결재 피드백 원문`, confidence: 0.8 });
    }
    for (const issue of qa?.issues || []) {
      const agent = { package: "packager", script: "scriptwriter", storyboard: "art-director", voice: "voice-director", motion: "motion-designer", sound: "sound-designer" }[issue.stage] || "";
      lessons.push({ agent, format: "", style: "", text: issue.fix, evidence: `QA 지적: ${issue.problem}`, confidence: 0.5 });
    }
    if (metrics) {
      const good = (metrics.avgViewPct ?? 0) >= 60;
      lessons.push({ agent: "scriptwriter", format: format || "", style: style || "", text: good ? "이번 영상의 훅 구조(통념 뒤집기 → 숫자 약속)를 다음에도 사용한다." : "훅 다음 문장에서 바로 본론으로 들어간다 — 약속 문장을 한 문장으로 줄인다.", evidence: `[시뮬레이션 지표] 조회수 ${metrics.views}, 평균 시청 ${metrics.avgViewPct}%`, confidence: 0.4 });
    }
    return { summary: `[시뮬레이션] ${trigger} 기준으로 ${lessons.length}개의 교훈을 정리했습니다.`, lessons, retire: [] };
  },

  scout() {
    return { summary: "시뮬레이션 모드에서는 웹 검색을 하지 않습니다. Anthropic API 키를 연결하면 최신 모델을 실제로 조사합니다.", proposals: [] };
  },
};

export function simBrain(task, input) {
  const fn = TASKS[task];
  if (!fn) throw new Error(`시뮬레이터가 모르는 작업입니다: ${task}`);
  const out = fn(input);
  delete out._topic;
  return out;
}

/** The topic the simulator settles on when the brief leaves it open. */
export function simTopic(brief) {
  return brief.topic || pick(rng(brief.id || "auto"), AUTO_TOPICS);
}
