import fsp from "node:fs/promises";
import path from "node:path";
import { OUTBOX_DIR } from "../paths.js";
import { config } from "../config.js";
import { sleep, speechUnits } from "../util.js";
import { simBrain } from "../sim/brain.js";
import { renderStill, renderOverlay } from "../media/render.js";
import { kenBurns, silence } from "../media/ffmpeg.js";
import { synthMusic } from "../media/synth.js";

/**
 * The local engine: everything the studio can do on this machine without an
 * external AI service.
 *
 *  - Real work done locally: motion graphics & captions (Chromium), camera
 *    moves on stills and all editing (ffmpeg).
 *  - Stand-ins, used only when no provider is connected for that job: the
 *    template "brain", abstract scene art, silent narration, synth music,
 *    a dry-run uploader and simulated analytics.
 *
 * Each function here is also the reference contract for its capability.
 */

const pause = () => sleep(config.studio.simulation?.step_delay_ms ?? 0);

export default {
  id: "local",
  name: "로컬 엔진",
  keys: [],
  available: () => true,

  /** Capabilities whose local version is only a placeholder for a real AI. */
  placeholder: { llm: true, search: true, image: true, video: true, tts: true, music: true, "publish.youtube": true, "publish.instagram": true, "analytics.youtube": true, "analytics.instagram": true },

  capabilities: {
    async llm({ task, input, schema }) {
      await pause();
      return { data: schema.parse(simBrain(task, input)), costUsd: 0 };
    },

    async search() {
      return { text: "", sources: [], costUsd: 0 };
    },

    /** { prompt, width, height, style, seedKey, outFile } → abstract art in the style's palette */
    async image({ width, height, style, seedKey, outFile }) {
      await renderStill({ width, height, duration: 0, style, layers: [], background: { art: style?.local_art || "bokeh", seed: seedKey || outFile } }, outFile);
      return { file: outFile, costUsd: 0 };
    },

    /** { imageFile, duration, width, height, fps, camera, outFile } → camera move over the still */
    async video({ imageFile, duration, width, height, fps, camera, outFile }) {
      await kenBurns({ imageFile, outFile, width, height, fps, duration, camera });
      return { file: outFile, costUsd: 0 };
    },

    /** { text, voice, outFile } → silence of the right length + estimated word timing */
    async tts({ text, voice, outFile }) {
      const speed = voice?.speed || 1;
      const words = text.trim().split(/\s+/).filter(Boolean);
      const rate = 6.6 * speed; // spoken syllables per second
      let t = 0;
      const timed = words.map((w) => {
        const d = Math.max(0.16, speechUnits(w) / rate);
        const item = { w, start: t, end: t + d * 0.92 };
        t += d;
        return item;
      });
      await silence(outFile, Math.max(0.6, t));
      return { file: outFile, words: timed, estimated: true, costUsd: 0 };
    },

    async music({ duration, bpm, mood, outFile }) {
      await synthMusic({ outFile, duration, bpm, mood });
      return { file: outFile, costUsd: 0 };
    },

    /** { comp, outFile } → transparent overlay video */
    async motion({ comp, outFile, onProgress }) {
      const r = await renderOverlay(comp, outFile, { onProgress });
      return { ...r, costUsd: 0 };
    },

    async edit() {
      return { costUsd: 0 }; // the edit stage drives ffmpeg directly; listed so it appears in the routing table
    },

    /** Dry-run upload: writes exactly what would be published to data/outbox. */
    async "publish.youtube"(args, ctx) {
      return outbox("youtube", args, ctx);
    },
    async "publish.instagram"(args, ctx) {
      return outbox("instagram", args, ctx);
    },

    async "analytics.youtube"(args) {
      return simulatedMetrics(args);
    },
    async "analytics.instagram"(args) {
      return simulatedMetrics(args);
    },
  },
};

async function outbox(platform, args, ctx) {
  await pause();
  const dir = path.join(OUTBOX_DIR, ctx.projectId || "manual", platform);
  await fsp.mkdir(dir, { recursive: true });
  if (args.file) await fsp.copyFile(args.file, path.join(dir, path.basename(args.file)));
  if (args.thumbnailFile) await fsp.copyFile(args.thumbnailFile, path.join(dir, path.basename(args.thumbnailFile)));
  const { file, thumbnailFile, options, model, ...meta } = args;
  await fsp.writeFile(path.join(dir, "upload.json"), JSON.stringify({ ...meta, preparedAt: new Date().toISOString() }, null, 1));
  return { id: `dryrun-${Date.now().toString(36)}`, url: "", dryRun: true, outboxDir: dir, warnings: [] };
}

/** Plausible numbers derived from the QA score, clearly flagged as simulated. */
function simulatedMetrics({ qaScore = 80, hoursSincePublish = 24, seed = "x" }) {
  let s = 0;
  for (const ch of String(seed)) s = (s * 31 + ch.charCodeAt(0)) >>> 0;
  const noise = ((s % 1000) / 1000) * 0.8 + 0.6;
  const quality = Math.max(0.2, (qaScore - 55) / 40);
  const growth = Math.log10(1 + hoursSincePublish) / Math.log10(169);
  const views = Math.round(1800 * Math.pow(quality, 3) * noise * (0.25 + growth) * 12);
  const avgViewPct = Math.round((38 + quality * 34 * (0.85 + noise * 0.2)) * 10) / 10;
  return { views, likes: Math.round(views * 0.045), comments: Math.round(views * 0.004), shares: Math.round(views * 0.006), subscribersGained: Math.round(views * 0.002), avgViewPct, avgViewSec: null, retention: null, simulated: true, costUsd: 0 };
}
