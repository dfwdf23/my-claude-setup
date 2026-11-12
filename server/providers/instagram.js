import fsp from "node:fs/promises";
import { secret, hasSecret } from "../secrets.js";
import { httpJson, sleep } from "../util.js";

/**
 * Instagram Reels publishing.
 *
 * Verified against Meta's docs on 2026-11-12 (Graph API v27.0):
 *  - Facebook Login tokens (graph.facebook.com) can upload a local file directly
 *    (upload_type=resumable → rupload.facebook.com).
 *  - Instagram Login tokens (graph.instagram.com) can only pull from a public
 *    video_url, so that path needs the studio to be reachable from the internet
 *    (PUBLIC_BASE_URL).
 */

const loginType = () => (secret("INSTAGRAM_LOGIN_TYPE") || "facebook").toLowerCase();
const host = () => (loginType() === "instagram" ? "https://graph.instagram.com" : "https://graph.facebook.com");
const bearer = () => ({ Authorization: `Bearer ${secret("INSTAGRAM_ACCESS_TOKEN")}` });

async function waitForContainer(base, id) {
  // Meta recommends polling about once a minute; short videos are usually ready sooner.
  for (let i = 0; i < 40; i++) {
    await sleep(i < 6 ? 5000 : 15000);
    const s = await httpJson(`${base}/${id}?fields=status_code`, { headers: bearer(), label: "Instagram 처리 상태" });
    if (s.status_code === "FINISHED") return;
    if (s.status_code === "ERROR" || s.status_code === "EXPIRED") throw new Error(`Instagram이 영상을 처리하지 못했습니다 (${s.status_code}) — 규격(H.264/AAC, 3초~15분, 300MB 이하)을 확인하세요.`);
  }
  throw new Error("Instagram 영상 처리가 제한 시간 안에 끝나지 않았습니다");
}

export default {
  id: "instagram",
  name: "Instagram",
  keys: ["INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"],
  available: () => hasSecret("INSTAGRAM_ACCESS_TOKEN") && hasSecret("INSTAGRAM_USER_ID"),

  capabilities: {
    /** { file, caption, shareToFeed, thumbOffsetMs, disclose, publicUrl } → { id, url } */
    async "publish.instagram"({ options, file, caption, shareToFeed = true, thumbOffsetMs = 0, disclose = true, publicUrl }) {
      const version = options.api_version || "v27.0";
      const base = `${host()}/${version}`;
      const user = secret("INSTAGRAM_USER_ID");
      const common = { media_type: "REELS", caption: caption.slice(0, 2200), share_to_feed: shareToFeed, thumb_offset: thumbOffsetMs, is_ai_generated: disclose };

      let containerId;
      if (loginType() === "instagram") {
        if (!publicUrl) throw new Error("Instagram 로그인 방식은 공개 주소가 필요합니다 — PUBLIC_BASE_URL 을 설정하거나 Facebook 로그인 토큰을 사용하세요.");
        const c = await httpJson(`${base}/${user}/media`, { method: "POST", headers: bearer(), body: { ...common, video_url: publicUrl }, label: "Instagram 릴스 등록" });
        containerId = c.id;
      } else {
        const q = new URLSearchParams({ ...Object.fromEntries(Object.entries(common).map(([k, v]) => [k, String(v)])), upload_type: "resumable" });
        const c = await httpJson(`${base}/${user}/media?${q}`, { method: "POST", headers: bearer(), label: "Instagram 릴스 등록" });
        containerId = c.id;
        const bytes = await fsp.readFile(file);
        const up = await fetch(c.uri || `https://rupload.facebook.com/ig-api-upload/${version}/${containerId}`, {
          method: "POST",
          headers: { Authorization: `OAuth ${secret("INSTAGRAM_ACCESS_TOKEN")}`, offset: "0", file_size: String(bytes.length) },
          body: bytes,
          signal: AbortSignal.timeout(30 * 60_000),
        });
        if (!up.ok) throw new Error(`Instagram 영상 전송 실패 (HTTP ${up.status}): ${(await up.text()).slice(0, 240)}`);
      }

      await waitForContainer(base, containerId);
      const published = await httpJson(`${base}/${user}/media_publish`, { method: "POST", headers: bearer(), body: { creation_id: containerId }, label: "Instagram 게시" });
      const info = await httpJson(`${base}/${published.id}?fields=permalink`, { headers: bearer(), label: "Instagram 링크 조회" }).catch(() => ({}));
      return { id: published.id, url: info.permalink || "", warnings: [] };
    },

    async "analytics.instagram"({ options, videoId }) {
      const base = `${host()}/${options.api_version || "v27.0"}`;
      const data = await httpJson(`${base}/${videoId}/insights?metric=views,reach,likes,comments,shares,saved,ig_reels_avg_watch_time`, { headers: bearer(), label: "Instagram 인사이트" });
      const m = Object.fromEntries((data.data || []).map((d) => [d.name, d.values?.[0]?.value ?? null]));
      return { views: m.views ?? 0, likes: m.likes ?? 0, comments: m.comments ?? 0, shares: m.shares ?? null, saves: m.saved ?? null, reach: m.reach ?? null, avgViewPct: null, avgViewSec: null, avgWatchRaw: m.ig_reels_avg_watch_time ?? null, retention: null };
    },
  },
};
