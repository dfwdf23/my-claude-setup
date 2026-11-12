import { z } from "zod";

/**
 * The contracts between pipeline stages. Every AI team member answers in one
 * of these shapes, so the real model and the offline simulator are
 * interchangeable and a stage can always trust its input.
 *
 * All fields are required on purpose (structured outputs work best that way);
 * "nothing" is an empty string or empty array.
 */

const str = (d) => z.string().describe(d);
const num = (d) => z.number().describe(d);

export const Research = z.object({
  summary: str("이 주제의 현재 상황과 기회를 3문장 이내로"),
  angles: z
    .array(
      z.object({
        title: str("영상 각도를 한 문장으로"),
        why_now: str("왜 지금 이 각도가 먹히는가"),
        audience: str("누가 클릭하는가"),
        evidence: z.array(z.string()).describe("근거 (확인한 사실·사례). 추측이면 '추측:'으로 시작"),
        potential: z.enum(["high", "medium", "low"]),
      }),
    )
    .describe("3~5개, 가장 강한 것부터"),
  benchmark_patterns: z.array(z.string()).describe("조회수 높은 영상들의 공통 패턴 (제목·길이·훅·썸네일)"),
  sources: z.array(z.object({ title: z.string(), url: z.string() })),
});

export const Plan = z.object({
  working_title: str("기획 제목"),
  title_options: z.array(z.string()).describe("업로드 제목 후보 3~5개, 서로 다른 심리를 사용"),
  logline: str("이 영상을 한 문장으로"),
  angle_reason: str("이 각도를 고른 이유 한 줄"),
  target_audience: z.string(),
  viewer_promise: str("시청자가 얻는 것 한 문장"),
  hook: z.object({
    line: str("실제 첫 문장"),
    visual: str("실제 첫 화면"),
    technique: str("사용한 훅 기법 (예: 반전, 숫자, 질문, 손실회피)"),
  }),
  style_id: str("스타일 목록의 id 중 하나"),
  style_reason: z.string(),
  duration_sec: num("목표 길이(초)"),
  structure: z.array(z.object({ beat: z.string(), purpose: z.string(), seconds: z.number() })).describe("구성 비트"),
  key_points: z.array(z.string()).describe("영상에 반드시 들어갈 내용"),
  thumbnail_concept: z.object({ text: z.string(), visual: z.string() }),
  cta: z.string(),
  risks: z.array(z.string()).describe("사실 확인이 필요한 점, 저작권·정책 위험. 없으면 빈 배열"),
  predicted: z.object({
    retention_pct: num("예상 평균 시청 지속률 %"),
    ctr_pct: num("예상 클릭률 %. 쇼츠·릴스는 0"),
    confidence: z.enum(["low", "medium", "high"]),
    rationale: z.string(),
  }),
  changes_from_feedback: str("수정 요청을 어떻게 반영했는지. 첫 기획이면 빈 문자열"),
});

export const Script = z.object({
  title: z.string(),
  scenes: z.array(
    z.object({
      id: str("s01, s02 … 순서대로"),
      beat: str("구성상 역할 (훅, 전개, 반전, 마무리 등)"),
      narration: str("성우가 읽을 문장"),
      on_screen_text: str("화면에 크게 띄울 문구. 없으면 빈 문자열"),
      visual_idea: str("이 장면에 보여야 할 것 한 줄"),
      emphasis_words: z.array(z.string()).describe("자막에서 강조할 단어 0~2개 (narration에 실제로 있는 단어)"),
    }),
  ),
  hook_alternatives: z.array(z.string()).describe("다른 첫 문장 후보 0~2개"),
  notes: str("작가의 말 — 팀에 전달할 연출 의도. 없으면 빈 문자열"),
});

export const Graphic = z.object({
  type: z.enum(["title", "stat", "list", "lower_third", "quote", "chapter", "badge", "cta"]),
  text: str("주 문구. stat이면 라벨, list면 목록 제목"),
  sub: str("보조 문구. 없으면 빈 문자열"),
  value: str("stat일 때 숫자와 단위 (예: 73%, 1,200만). 아니면 빈 문자열"),
  items: z.array(z.string()).describe("list일 때 항목들. 아니면 빈 배열"),
});

export const Storyboard = z.object({
  visual_bible: z.object({
    look: str("이 영상의 화풍·조명·색을 한국어로 설명"),
    consistency_tokens: str("모든 장면 프롬프트에 붙일 영어 고정 문구"),
    characters: str("반복 등장 인물·사물의 고정 외형 (영어). 없으면 빈 문자열"),
  }),
  scenes: z.array(
    z.object({
      id: str("대본의 장면 id와 동일"),
      mode: z.enum(["video", "image", "motion"]).describe("video=영상 생성, image=이미지+카메라무빙, motion=풀스크린 모션그래픽"),
      shot: str("샷 크기·앵글 (예: 클로즈업, 로우앵글)"),
      image_prompt: str("영어 이미지 프롬프트. mode가 motion이어도 배경 분위기를 적는다"),
      motion_prompt: str("영어. 피사체 동작 + 카메라 움직임"),
      camera: z.enum(["push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down", "static"]),
      graphics: z.array(Graphic).describe("이 장면 위에 올릴 모션그래픽. 없으면 빈 배열"),
      transition: z.enum(["cut", "fade", "flash", "slide"]).describe("이 장면으로 들어올 때의 전환"),
    }),
  ),
  music_brief: z.object({
    mood: z.string(),
    genre: z.string(),
    bpm: z.number(),
    prompt: str("음악 생성 AI에 넣을 영어 프롬프트. 연주곡(instrumental)"),
  }),
  voice_brief: z.object({ persona: z.string(), tone: z.string(), pace: z.string() }),
});

export const VoiceDirection = z.object({
  scenes: z.array(
    z.object({
      id: z.string(),
      tts_text: str("TTS가 읽을 텍스트 (숫자·기호·약어를 한글 발음으로)"),
      delivery: str("감정·속도 지시 한 줄"),
    }),
  ),
});

export const Package = z.object({
  title: z.string(),
  description: z.string(),
  tags: z.array(z.string()),
  hashtags: z.array(z.string()).describe("# 없이"),
  thumbnail: z.object({
    headline: str("3~5단어"),
    sub: str("작은 보조 문구. 없으면 빈 문자열"),
    image_prompt: str("영어. 감정이 드러나는 피사체 하나를 크게"),
    layout: z.enum(["left", "center", "right"]).describe("글자 위치"),
  }),
  chapters: z.array(z.object({ scene_id: z.string(), label: z.string() })).describe("롱폼만. 아니면 빈 배열"),
  instagram_caption: str("인스타그램 대상이 아니면 빈 문자열"),
  pinned_comment: str("고정 댓글. 없으면 빈 문자열"),
});

export const QaReport = z.object({
  scores: z.array(
    z.object({
      id: str("기준표의 항목 id"),
      score: num("0~100"),
      note: str("근거 — 구체적인 장면·문장을 짚어서"),
    }),
  ),
  strengths: z.array(z.string()),
  issues: z.array(
    z.object({
      stage: str("고쳐야 할 단계 id (script, storyboard, voice, visuals, motion, sound, package, edit 중 하나)"),
      scene_id: str("특정 장면이면 그 id, 전체면 빈 문자열"),
      severity: z.enum(["high", "medium", "low"]),
      problem: z.string(),
      fix: str("담당자가 바로 실행할 수 있는 지시"),
    }),
  ),
  predicted: z.object({ retention_pct: z.number(), ctr_pct: z.number() }),
  summary: str("결재자에게 보여줄 한두 문장 총평"),
});

export const Triage = z.object({
  targets: z
    .array(
      z.object({
        stage: str("다시 작업할 단계 id"),
        instruction: str("그 단계 담당자에게 줄 구체적 지시"),
      }),
    )
    .describe("꼭 필요한 단계만"),
});

export const Retro = z.object({
  summary: str("이번 작업에서 배운 것 2~3문장"),
  lessons: z.array(
    z.object({
      agent: str("적용할 담당 AI id. 팀 전체면 빈 문자열"),
      format: str("적용할 포맷 id. 전체면 빈 문자열"),
      style: str("적용할 스타일 id. 전체면 빈 문자열"),
      text: str("다음 작업에서 바로 실행할 수 있는 교훈 한 문장"),
      evidence: str("근거 (피드백 원문, QA 점수, 실제 지표)"),
      confidence: num("0~1"),
    }),
  ),
  retire: z.array(z.string()).describe("이번 결과로 틀렸다고 판단되는 기존 노트의 id"),
});

export const ScoutReport = z.object({
  summary: z.string(),
  proposals: z.array(
    z.object({
      capability: z.enum(["image", "video", "tts", "music", "llm"]),
      candidate: str("라우팅에 넣을 값 (제공사/모델). 모델 ID를 확인하지 못했으면 '확인 필요'"),
      why: z.string(),
      source_url: z.string(),
    }),
  ),
});
