import fs from "node:fs";
import path from "node:path";
import { ROOT } from "./paths.js";
import { readJson, writeJson } from "./store.js";

/**
 * API keys and tokens. Looked up in this order:
 *   1. environment variables (including a .env file in the project root)
 *   2. data/secrets.json — written when a key is entered in the dashboard or an account is connected
 * Values never leave the server; the dashboard only learns whether a key is set.
 */

export const KNOWN_KEYS = [
  { key: "ANTHROPIC_API_KEY", label: "Anthropic (Claude) — 기획·대본·연출·검수 두뇌", url: "https://console.anthropic.com/settings/keys" },
  { key: "FAL_KEY", label: "fal.ai — 이미지·영상·음악 생성 모델 모음", url: "https://fal.ai/dashboard/keys" },
  { key: "ELEVENLABS_API_KEY", label: "ElevenLabs — 성우·음악·효과음", url: "https://elevenlabs.io/app/settings/api-keys" },
  { key: "OPENAI_API_KEY", label: "OpenAI — 이미지·음성 (예비)", url: "https://platform.openai.com/api-keys" },
  { key: "YOUTUBE_API_KEY", label: "YouTube Data API 키 — 벤치마크 영상 조회 (선택)", url: "https://console.cloud.google.com/apis/credentials" },
  { key: "YOUTUBE_CLIENT_ID", label: "YouTube OAuth 클라이언트 ID — 업로드·성과 조회", url: "https://console.cloud.google.com/apis/credentials" },
  { key: "YOUTUBE_CLIENT_SECRET", label: "YouTube OAuth 클라이언트 보안 비밀", url: "https://console.cloud.google.com/apis/credentials" },
  { key: "INSTAGRAM_ACCESS_TOKEN", label: "Instagram 액세스 토큰 — 릴스 업로드", url: "https://developers.facebook.com/apps" },
  { key: "INSTAGRAM_USER_ID", label: "Instagram 계정 ID", url: "https://developers.facebook.com/apps" },
];

const FILE = "secrets.json";

export function loadDotEnv() {
  const file = path.join(ROOT, ".env");
  if (!fs.existsSync(file)) return;
  for (const raw of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    if (value && process.env[key] === undefined) process.env[key] = value;
  }
}

export function secret(key) {
  const fromEnv = process.env[key];
  if (fromEnv) return fromEnv;
  return readJson(FILE, {})[key] || "";
}

export const hasSecret = (key) => Boolean(secret(key));

export function setSecret(key, value) {
  const all = readJson(FILE, {});
  if (value) all[key] = value;
  else delete all[key];
  writeJson(FILE, all, { secret: true });
}

/** What the dashboard is allowed to know. */
export function secretStatus() {
  const stored = readJson(FILE, {});
  return KNOWN_KEYS.map(({ key, label, url }) => ({
    key,
    label,
    url,
    set: hasSecret(key),
    source: process.env[key] ? "env" : stored[key] ? "dashboard" : null,
  }));
}
