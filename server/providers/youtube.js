import fs from "node:fs";
import fsp from "node:fs/promises";
import { secret, hasSecret, setSecret } from "../secrets.js";
import { httpJson } from "../util.js";

/**
 * YouTube — upload (long-form and Shorts), thumbnail, analytics, benchmark lookup.
 *
 * Verified against Google's docs on 2026-11-12:
 *  - resumable upload: POST /upload/youtube/v3/videos?uploadType=resumable → PUT to the Location URL
 *  - a Short is not a flag: a vertical/square video up to 3 minutes is classified as one automatically
 *  - status.containsSyntheticMedia is the AI-content disclosure field
 *  - impressions / CTR are not available in the Analytics API v2
 */

export const SCOPES = [
  "https://www.googleapis.com/auth/youtube.upload",
  "https://www.googleapis.com/auth/youtube.readonly",
  "https://www.googleapis.com/auth/yt-analytics.readonly",
];

let token = { value: null, expiresAt: 0 };

async function accessToken() {
  if (token.value && Date.now() < token.expiresAt - 60_000) return token.value;
  const body = new URLSearchParams({
    client_id: secret("YOUTUBE_CLIENT_ID"),
    client_secret: secret("YOUTUBE_CLIENT_SECRET"),
    refresh_token: secret("YOUTUBE_REFRESH_TOKEN"),
    grant_type: "refresh_token",
  });
  const data = await httpJson("https://oauth2.googleapis.com/token", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: body.toString(), label: "YouTube 인증 갱신" });
  token = { value: data.access_token, expiresAt: Date.now() + (data.expires_in || 3000) * 1000 };
  return token.value;
}

const bearer = async () => ({ Authorization: `Bearer ${await accessToken()}` });

// ── OAuth connect flow (driven from the dashboard) ───────────────────────

export function authUrl(redirectUri, state) {
  const q = new URLSearchParams({
    client_id: secret("YOUTUBE_CLIENT_ID"),
    redirect_uri: redirectUri,
    response_type: "code",
    scope: SCOPES.join(" "),
    access_type: "offline",
    prompt: "consent",
    state,
  });
  return `https://accounts.google.com/o/oauth2/v2/auth?${q}`;
}

export async function exchangeCode(code, redirectUri) {
  const body = new URLSearchParams({
    code,
    client_id: secret("YOUTUBE_CLIENT_ID"),
    client_secret: secret("YOUTUBE_CLIENT_SECRET"),
    redirect_uri: redirectUri,
    grant_type: "authorization_code",
  });
  const data = await httpJson("https://oauth2.googleapis.com/token", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: body.toString(), label: "YouTube 계정 연결" });
  if (!data.refresh_token) throw new Error("Google이 갱신 토큰을 주지 않았습니다 — Google 계정의 앱 액세스에서 이 앱을 제거한 뒤 다시 연결해 주세요.");
  setSecret("YOUTUBE_REFRESH_TOKEN", data.refresh_token);
  token = { value: data.access_token, expiresAt: Date.now() + (data.expires_in || 3000) * 1000 };
}

export const connected = () => hasSecret("YOUTUBE_CLIENT_ID") && hasSecret("YOUTUBE_CLIENT_SECRET") && hasSecret("YOUTUBE_REFRESH_TOKEN");

// ── Benchmarks: what is already getting 100K+ views on this topic ────────

export async function benchmarks(query, { days = 120, minViews = 100_000, max = 12, language = "ko" } = {}) {
  const key = secret("YOUTUBE_API_KEY");
  if (!key) return [];
  const after = new Date(Date.now() - days * 86_400_000).toISOString();
  const search = new URLSearchParams({ part: "snippet", q: query, type: "video", order: "viewCount", publishedAfter: after, maxResults: "25", relevanceLanguage: language, key });
  const found = await httpJson(`https://www.googleapis.com/youtube/v3/search?${search}`, { label: "YouTube 벤치마크 검색" });
  const ids = (found.items || []).map((i) => i.id?.videoId).filter(Boolean);
  if (!ids.length) return [];
  const details = await httpJson(`https://www.googleapis.com/youtube/v3/videos?${new URLSearchParams({ part: "snippet,statistics,contentDetails", id: ids.join(","), key })}`, { label: "YouTube 벤치마크 상세" });
  return (details.items || [])
    .map((v) => ({
      title: v.snippet?.title,
      channel: v.snippet?.channelTitle,
      publishedAt: v.snippet?.publishedAt,
      views: Number(v.statistics?.viewCount || 0),
      likes: Number(v.statistics?.likeCount || 0),
      comments: Number(v.statistics?.commentCount || 0),
      duration: v.contentDetails?.duration, // ISO 8601, e.g. PT58S
      url: `https://www.youtube.com/watch?v=${v.id}`,
    }))
    .filter((v) => v.views >= minViews)
    .sort((a, b) => b.views - a.views)
    .slice(0, max);
}

// ── Upload ───────────────────────────────────────────────────────────────

async function uploadVideo({ file, title, description, tags, categoryId, privacy, publishAt, madeForKids, disclose, language }) {
  const size = (await fsp.stat(file)).size;
  const status = { privacyStatus: publishAt ? "private" : privacy, selfDeclaredMadeForKids: Boolean(madeForKids), containsSyntheticMedia: Boolean(disclose) };
  if (publishAt) status.publishAt = new Date(publishAt).toISOString(); // scheduling requires private until then
  const init = await fetch("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status", {
    method: "POST",
    headers: { ...(await bearer()), "Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Length": String(size), "X-Upload-Content-Type": "video/*" },
    body: JSON.stringify({ snippet: { title: title.slice(0, 100), description: description.slice(0, 4900), tags, categoryId: String(categoryId || "22"), defaultLanguage: language }, status }),
  });
  if (!init.ok) throw Object.assign(new Error(`YouTube 업로드 시작 실패 (HTTP ${init.status}): ${(await init.text()).slice(0, 300)}`), { status: init.status });
  const session = init.headers.get("location");
  if (!session) throw new Error("YouTube: 업로드 세션 주소를 받지 못했습니다");

  const put = await fetch(session, {
    method: "PUT",
    headers: { ...(await bearer()), "Content-Length": String(size), "Content-Type": "video/*" },
    body: fs.createReadStream(file),
    duplex: "half",
    signal: AbortSignal.timeout(60 * 60_000),
  });
  const text = await put.text();
  if (!put.ok) throw Object.assign(new Error(`YouTube 업로드 실패 (HTTP ${put.status}): ${text.slice(0, 300)}`), { status: put.status });
  return JSON.parse(text);
}

async function setThumbnail(videoId, file) {
  const res = await fetch(`https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=${videoId}`, {
    method: "POST",
    headers: { ...(await bearer()), "Content-Type": file.endsWith(".png") ? "image/png" : "image/jpeg" },
    body: await fsp.readFile(file),
  });
  if (!res.ok) throw new Error(`썸네일 설정 실패 (HTTP ${res.status}): ${(await res.text()).slice(0, 200)}`);
}

function parseReport(report) {
  const names = (report.columnHeaders || []).map((h) => h.name);
  return (report.rows || []).map((row) => Object.fromEntries(row.map((v, i) => [names[i], v])));
}

export default {
  id: "youtube",
  name: "YouTube",
  keys: ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"],
  available: connected,

  capabilities: {
    /** → { id, url, warnings } */
    async "publish.youtube"(args) {
      const video = await uploadVideo(args);
      const warnings = [];
      if (args.thumbnailFile) {
        // For Shorts the thumbnail can only be set once processing has finished; never fail the upload over it.
        await setThumbnail(video.id, args.thumbnailFile).catch((err) => warnings.push(err.message));
      }
      return { id: video.id, url: args.isShort ? `https://www.youtube.com/shorts/${video.id}` : `https://www.youtube.com/watch?v=${video.id}`, warnings };
    },

    /** { videoId, publishedAt } → metrics (null fields when YouTube has no data yet) */
    async "analytics.youtube"({ videoId, publishedAt }) {
      const headers = await bearer();
      const stats = await httpJson(`https://www.googleapis.com/youtube/v3/videos?part=statistics&id=${videoId}`, { headers, label: "YouTube 통계" });
      const s = stats.items?.[0]?.statistics || {};
      const out = { views: Number(s.viewCount || 0), likes: Number(s.likeCount || 0), comments: Number(s.commentCount || 0), avgViewPct: null, avgViewSec: null, shares: null, subscribersGained: null, retention: null };

      const start = new Date(publishedAt || Date.now()).toISOString().slice(0, 10);
      const end = new Date().toISOString().slice(0, 10);
      const base = `https://youtubeanalytics.googleapis.com/v2/reports?ids=channel==MINE&startDate=${start}&endDate=${end}&filters=video==${videoId}`;
      // Analytics lags 48–72h; an empty report is normal early on.
      const summary = await httpJson(`${base}&metrics=views,averageViewDuration,averageViewPercentage,shares,subscribersGained`, { headers, label: "YouTube 분석" }).catch(() => null);
      const row = summary && parseReport(summary)[0];
      if (row) {
        out.avgViewPct = row.averageViewPercentage ?? null;
        out.avgViewSec = row.averageViewDuration ?? null;
        out.shares = row.shares ?? null;
        out.subscribersGained = row.subscribersGained ?? null;
      }
      const retention = await httpJson(`${base}&dimensions=elapsedVideoTimeRatio&metrics=audienceWatchRatio`, { headers, label: "YouTube 시청 지속 곡선" }).catch(() => null);
      if (retention?.rows?.length) out.retention = retention.rows.map(([t, r]) => ({ t, r }));
      return out;
    },
  },
};
