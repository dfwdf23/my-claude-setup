// Shared plumbing for the dashboard: rendering, data, live updates, formatting, icons.
// No build step — this file is served exactly as written.
import { html, render, useState, useEffect, useMemo, useRef, useCallback } from "/vendor/htm.js";
export { html, render, useState, useEffect, useMemo, useRef, useCallback };

// ── API ──────────────────────────────────────────────────────────────────

export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, { method, headers: body ? { "content-type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("로그인이 필요합니다");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `요청 실패 (${res.status})`);
  return data;
}

// ── Store: one snapshot of studio state, refreshed whenever the server says something changed ──

const listeners = new Set();
export const store = { state: null, activity: [], connected: false, tick: 0 };

function publish() {
  store.tick++;
  for (const fn of listeners) fn(store.tick);
}

let refreshTimer = null;
let refreshing = false;
let again = false;
export async function refresh() {
  if (refreshing) {
    again = true;
    return;
  }
  refreshing = true;
  try {
    store.state = await api("/api/state");
    store.activity = store.state.activity;
    publish();
  } catch (err) {
    console.error(err);
  } finally {
    refreshing = false;
    if (again) {
      again = false;
      refresh();
    }
  }
}
const refreshSoon = () => {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 180);
};

/** Events for one project, so an open detail page can refetch itself. */
const projectListeners = new Set();
export function onProjectEvent(fn) {
  projectListeners.add(fn);
  return () => projectListeners.delete(fn);
}

export function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => {
    store.connected = true;
    refresh();
  };
  es.onerror = () => {
    store.connected = false;
    publish();
  };
  es.onmessage = (msg) => {
    const e = JSON.parse(msg.data);
    if (e.type === "activity") {
      store.activity = [...store.activity.slice(-120), e.payload];
      publish();
      return;
    }
    if (e.type === "project") for (const fn of projectListeners) fn(e.payload.id);
    if (e.type === "config" && e.payload.files?.length && !e.payload.files.includes("secrets") && !e.payload.files.includes("scout")) {
      toast(e.payload.errors?.length ? `설정 오류 — ${e.payload.errors[0].file}: ${e.payload.errors[0].message}` : `설정 반영됨 · ${e.payload.files.slice(0, 3).join(", ")}`, e.payload.errors?.length ? "bad" : "");
    }
    refreshSoon();
  };
}

export function useStore() {
  const [, set] = useState(0);
  useEffect(() => {
    listeners.add(set);
    return () => listeners.delete(set);
  }, []);
  return store;
}

// ── Router (hash based) ──────────────────────────────────────────────────

const parseHash = () => (location.hash.replace(/^#\/?/, "") || "").split("/").filter(Boolean);
export function useRoute() {
  const [parts, set] = useState(parseHash());
  useEffect(() => {
    const on = () => {
      set(parseHash());
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return parts;
}
export const go = (path) => (location.hash = "#/" + path.replace(/^\//, ""));

// ── Toast ────────────────────────────────────────────────────────────────

let toastEl;
let toastTimer;
export function toast(text, kind = "") {
  toastEl?.remove();
  toastEl = document.createElement("div");
  toastEl.className = "toast " + kind;
  toastEl.textContent = text;
  toastEl.setAttribute("role", "status");
  document.body.appendChild(toastEl);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl?.remove(), kind === "bad" ? 6000 : 2800);
}

/** Run an action, surface failures as a toast, and report busy state. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const run = useCallback(async (fn, okText) => {
    setBusy(true);
    try {
      const out = await fn();
      if (okText) toast(okText);
      return out;
    } catch (err) {
      toast(err.message, "bad");
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return [busy, run];
}

// ── Formatting ───────────────────────────────────────────────────────────

export function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, (Date.now() - ts) / 1000);
  if (s < 45) return "방금";
  if (s < 3600) return `${Math.round(s / 60)}분 전`;
  if (s < 86400) return `${Math.round(s / 3600)}시간 전`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)}일 전`;
  return new Date(ts).toLocaleDateString("ko-KR", { month: "short", day: "numeric" });
}
export const clock = (ts) => new Date(ts).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
export const dateTime = (ts) => new Date(ts).toLocaleString("ko-KR", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
export function duration(sec) {
  if (sec == null) return "-";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return m ? `${m}분 ${String(s).padStart(2, "0")}초` : `${s}초`;
}
export function compact(n) {
  if (n == null) return "-";
  if (n >= 100_000_000) return `${(n / 100_000_000).toFixed(1).replace(/\.0$/, "")}억`;
  if (n >= 10_000) return `${(n / 10_000).toFixed(n >= 100_000 ? 0 : 1).replace(/\.0$/, "")}만`;
  return n.toLocaleString("ko-KR");
}
/** Links that come from a model or the web are only followed when they are plain http(s). */
export const safeUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u : undefined);
export const money = (usd) => `$${(usd || 0).toFixed(usd >= 10 ? 1 : 2)}`;

export const STATUS = {
  active: { label: "제작 중", cls: "run", icon: "loader" },
  waiting: { label: "결재 대기", cls: "wait", icon: "inbox" },
  paused: { label: "일시정지", cls: "wait", icon: "pause" },
  failed: { label: "오류", cls: "fail", icon: "alert" },
  done: { label: "완료", cls: "done", icon: "check" },
  canceled: { label: "중단", cls: "idle", icon: "x" },
};
export const PLATFORM = { youtube: "유튜브", youtube_shorts: "쇼츠", instagram_reels: "릴스" };
export const CAPABILITY = { llm: "두뇌", search: "웹 검색", image: "이미지", video: "영상", tts: "성우", music: "음악", motion: "모션그래픽", edit: "편집", vision: "영상 판독", publish: "업로드", analytics: "성과 조회", "publish.youtube": "유튜브 업로드", "publish.instagram": "인스타 업로드", "analytics.youtube": "유튜브 성과", "analytics.instagram": "인스타 성과" };
export const SOURCE = { seed: "기본 원칙", feedback: "결재 피드백", qa: "품질 검수", analytics: "실제 성과", manual: "직접 지시" };

// ── Icons (24×24, stroke) ────────────────────────────────────────────────

const P = {
  gauge: "M12 14l4-4M3.3 17a9.5 9.5 0 1 1 17.4 0",
  inbox: "M3 13l3-8h12l3 8v5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1zM3 13h5l1 2h6l1-2h5",
  film: "M4 4h16v16H4zM8 4v16M16 4v16M4 9h4M4 15h4M16 9h4M16 15h4",
  users: "M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20M10 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM20 20v-1.5a3.5 3.5 0 0 0-2.5-3.3M15.5 4.2a3.5 3.5 0 0 1 0 6.6",
  palette: "M12 3a9 9 0 1 0 0 18c1.2 0 2-.9 2-2 0-.5-.2-1-.5-1.300-.3-.4-.5-.8-.5-1.300 0-1.100.9-2 2-2h2.300A3.700 3.700 0 0 0 21 11.700C21 6.900 17 3 12 3zM7.500 11.500h.010M10.500 7.500h.010M15 8.500h.010",
  brain: "M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 0V7a3 3 0 0 0-3-3zM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 0",
  settings: "M12 15.500a3.500 3.500 0 1 0 0-7 3.500 3.500 0 0 0 0 7zM19 12a7 7 0 0 0-.100-1.200l2-1.500-2-3.400-2.300.900a7 7 0 0 0-2-1.200L14.200 3h-4.400l-.400 2.600a7 7 0 0 0-2 1.200l-2.300-.900-2 3.400 2 1.500a7 7 0 0 0 0 2.400l-2 1.500 2 3.400 2.300-.900a7 7 0 0 0 2 1.200l.400 2.600h4.400l.400-2.600a7 7 0 0 0 2-1.200l2.300.900 2-3.400-2-1.500c.100-.400.100-.800.100-1.200z",
  plus: "M12 5v14M5 12h14",
  check: "M5 12.500l4.500 4.500L19 7.500",
  x: "M6 6l12 12M18 6L6 18",
  alert: "M12 8.500v4.500M12 16.500h.010M10.300 3.900L2.600 17.500a2 2 0 0 0 1.700 3h15.400a2 2 0 0 0 1.700-3L13.700 3.900a2 2 0 0 0-3.400 0z",
  play: "M7 4.500v15l12-7.500z",
  pause: "M8 5v14M16 5v14",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  loader: "M12 3v3M12 18v3M4.200 7.500l2.600 1.500M17.200 15l2.600 1.500M4.200 16.500L6.800 15M17.200 9l2.600-1.500",
  radar: "M12 12l6-6M12 21a9 9 0 1 1 6.400-2.600M12 16a4 4 0 1 1 2.800-1.200",
  clapperboard: "M4 10h16v10H4zM4 10l-.800-3.500 15.500-3.300.800 3.500zM8.500 5.300l2 3.700M13.500 4.200l2 3.700",
  pen: "M4 20l1-4.500L16.500 4a2.100 2.100 0 0 1 3 3L8 18.500zM14.500 6l3 3",
  stethoscope: "M6 3v6a4 4 0 0 0 8 0V3M10 13v2.500a4.500 4.500 0 0 0 9 0V13M19 13a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
  mic: "M12 15a3.500 3.500 0 0 0 3.500-3.500v-5a3.500 3.500 0 0 0-7 0v5A3.500 3.500 0 0 0 12 15zM5.500 11.500a6.500 6.500 0 0 0 13 0M12 18v3",
  image: "M4 5h16v14H4zM4 15l4.500-4.500 4 4 2.500-2.500L20 17M9 9.500h.010",
  sparkles: "M11 4l1.700 4.800L17.500 10.500l-4.800 1.700L11 17l-1.700-4.800L4.500 10.500l4.800-1.700zM18.500 15l.700 2 2 .700-2 .700-.700 2-.700-2-2-.700 2-.700z",
  music: "M9 18V5.500l10-2V16M9 18a2.500 2.500 0 1 1-5 0 2.500 2.500 0 0 1 5 0zM19 16a2.500 2.500 0 1 1-5 0 2.500 2.500 0 0 1 5 0z",
  scissors: "M7 9.500a2.500 2.500 0 1 0 0-5 2.500 2.500 0 0 0 0 5zM7 19.500a2.500 2.500 0 1 0 0-5 2.500 2.500 0 0 0 0 5zM9 8.500L20 18M9 15.500L20 6",
  tag: "M3.500 12.500V4.500h8L20.500 13.500l-7 7zM8 8.500h.010",
  shield: "M12 3l7.500 2.500v6c0 4.500-3.200 8-7.500 9.500-4.300-1.500-7.500-5-7.500-9.500v-6zM9 12l2.200 2.200L15.200 10",
  upload: "M12 16V4.500M7.500 9L12 4.500 16.500 9M4.500 15.500v3a1 1 0 0 0 1 1h13a1 1 0 0 0 1-1v-3",
  chart: "M4 20V4M4 20h16M8 16v-4M12 16V8M16 16v-6",
  telescope: "M4.500 13.500l11-5 2 4.500-11 5zM15.500 8.500l3.500-1.500 2 4.500-3.500 1.500M10 16l-2.500 5M12 15.500l3 5.500",
  chevron: "M9 6l6 6-6 6",
  back: "M15 6l-6 6 6 6",
  refresh: "M20 11.500a8 8 0 0 0-14.500-4L4 9M4 4.500V9h4.500M4 12.500a8 8 0 0 0 14.500 4L20 15M20 19.500V15h-4.500",
  external: "M14 4.500h5.500V10M19.500 4.500L11 13M17.500 13.500v5a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1h5",
  edit: "M4 20h4L19.500 8.500a2.100 2.100 0 0 0-3-3L5 17zM14.500 7.500l3 3",
  trash: "M5 7h14M10 7V4.500h4V7M7 7l.800 12.500h8.400L17 7M10 11v5M14 11v5",
  sun: "M12 16.500a4.500 4.500 0 1 0 0-9 4.500 4.500 0 0 0 0 9zM12 2.500v2M12 19.500v2M4.600 4.600l1.400 1.400M18 18l1.400 1.400M2.500 12h2M19.500 12h2M4.600 19.400L6 18M18 6l1.400-1.400",
  moon: "M20 14.500A8.500 8.500 0 0 1 9.500 4a8.500 8.500 0 1 0 10.500 10.500z",
  bolt: "M13 3L5 13.500h6L10 21l8-10.500h-6z",
  link: "M10 14a4.500 4.500 0 0 0 6.400 0l3-3a4.500 4.500 0 0 0-6.400-6.400L12 5.600M14 10a4.500 4.500 0 0 0-6.400 0l-3 3a4.500 4.500 0 0 0 6.400 6.400L12 18.400",
  key: "M15 9.500a4.500 4.500 0 1 1-4.200 2.900L3.500 19.500V21H6l1.500-1.500V18H9l2.600-2.600M16 8.500h.010",
  eye: "M2.500 12S6 5.500 12 5.500 21.500 12 21.500 12 18 18.500 12 18.500 2.500 12 2.500 12zM12 14.500a2.500 2.500 0 1 0 0-5 2.500 2.500 0 0 0 0 5z",
  list: "M8 6.500h12M8 12h12M8 17.500h12M4 6.500h.010M4 12h.010M4 17.500h.010",
  more: "M5 12h.010M12 12h.010M19 12h.010",
  robot: "M12 7V4M8 4h8M5 7h14v11H5zM9 12h.010M15 12h.010M9.500 15.500h5M2.500 11v4M21.500 11v4",
  flag: "M5 21V4M5 4.500h11l-2 4 2 4H5",
};

export function Icon({ name, size = "", cls = "" }) {
  const d = P[name] || P.more;
  return html`<svg class=${`i ${size} ${cls}`} viewBox="0 0 24 24" aria-hidden="true">${d.split("M").filter(Boolean).map((seg) => html`<path d=${"M" + seg} />`)}</svg>`;
}
