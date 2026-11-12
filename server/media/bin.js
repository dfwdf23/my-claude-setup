import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";

/**
 * Where ffmpeg / ffprobe live. Order: FFMPEG_PATH / FFPROBE_PATH env vars,
 * then the copy bundled through npm (no separate install needed),
 * then whatever is on the PATH.
 */
const require = createRequire(import.meta.url);

function bundled(name) {
  try {
    if (name === "ffmpeg") {
      const p = require("ffmpeg-static");
      return typeof p === "string" ? p : p?.default || null;
    }
    return require("@ffprobe-installer/ffprobe").path;
  } catch {
    return null;
  }
}

function works(bin) {
  if (!bin) return false;
  try {
    return spawnSync(bin, ["-version"], { stdio: "ignore", timeout: 15_000, windowsHide: true }).status === 0;
  } catch {
    return false;
  }
}

function locate(name, envKey) {
  const candidates = [process.env[envKey], bundled(name), name];
  return candidates.find(works) || name;
}

export const FFMPEG = locate("ffmpeg", "FFMPEG_PATH");
export const FFPROBE = locate("ffprobe", "FFPROBE_PATH");
