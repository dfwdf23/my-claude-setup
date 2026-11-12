import fs from "node:fs/promises";
import path from "node:path";
import { secret, hasSecret } from "../secrets.js";
import { httpJson, downloadTo, sleep } from "../util.js";
import { priceOf } from "./registry.js";

/**
 * fal.ai — one key, many of the strongest image / video / music models.
 *
 * Models on fal do not share field names (image_url vs start_image_url, int vs
 * string durations …), so each routing entry carries a small `options` recipe
 * describing how to shape the request. Adding a new model is a routing.yaml
 * edit, not a code change. Recipes for the models we ship are in routing.yaml.
 *
 * API facts verified against fal's queue OpenAPI spec on 2026-11-12:
 *   submit  POST https://queue.fal.run/{model}   Authorization: Key <key>
 *   poll    GET  <status_url from the submit response>
 *   result  GET  <response_url from the submit response>
 */

const auth = () => ({ Authorization: `Key ${secret("FAL_KEY")}` });

const MIME = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav" };

/** Upload a local file to fal storage and get a URL models can read. */
async function upload(file) {
  const contentType = MIME[path.extname(file).toLowerCase()] || "application/octet-stream";
  const init = await httpJson("https://rest.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3", {
    method: "POST",
    headers: auth(),
    body: { file_name: path.basename(file), content_type: contentType },
    label: "fal 파일 업로드 준비",
  });
  const put = await fetch(init.upload_url, { method: "PUT", headers: { "Content-Type": contentType }, body: await fs.readFile(file) });
  if (!put.ok) throw new Error(`fal 파일 업로드 실패 (HTTP ${put.status})`);
  return init.file_url;
}

/** Submit to the queue and wait for the result. */
async function runQueue(model, input, { timeoutMs = 15 * 60_000 } = {}) {
  const submitted = await httpJson(`https://queue.fal.run/${model}`, { method: "POST", headers: auth(), body: input, label: `fal ${model}` });
  // Use the URLs fal returns — they point at the resolved internal app, not the public model id.
  const { status_url: statusUrl, response_url: responseUrl } = submitted;
  if (!statusUrl || !responseUrl) throw new Error(`fal ${model}: 요청 접수 응답이 예상과 다릅니다`);

  const deadline = Date.now() + timeoutMs;
  let wait = 1500;
  while (Date.now() < deadline) {
    await sleep(wait);
    wait = Math.min(wait * 1.3, 8000);
    const res = await fetch(statusUrl, { headers: auth() });
    const status = await res.json().catch(() => ({}));
    if (status.status === "COMPLETED") {
      // A failed run also reports COMPLETED — the error fields tell them apart.
      if (status.error) throw new Error(`fal ${model}: ${String(status.error).slice(0, 240)}`);
      return httpJson(responseUrl, { headers: auth(), label: `fal ${model} 결과` });
    }
    if (!res.ok && res.status !== 202) throw new Error(`fal ${model}: 상태 조회 실패 (HTTP ${res.status})`);
  }
  throw new Error(`fal ${model}: ${Math.round(timeoutMs / 60000)}분 안에 끝나지 않았습니다`);
}

function aspectOf(width, height) {
  const r = width / height;
  const table = [["9:16", 9 / 16], ["16:9", 16 / 9], ["1:1", 1], ["4:5", 4 / 5], ["3:4", 3 / 4], ["4:3", 4 / 3]];
  return table.reduce((best, cur) => (Math.abs(cur[1] - r) < Math.abs(best[1] - r) ? cur : best))[0];
}

const firstUrl = (...candidates) => candidates.flat().find((c) => c?.url)?.url;

export default {
  id: "fal",
  name: "fal.ai",
  keys: ["FAL_KEY"],
  available: () => hasSecret("FAL_KEY"),

  capabilities: {
    /** { prompt, negative, width, height, refFiles, outFile } → { file } */
    async image({ model, options, prompt, negative, width, height, refFiles = [], outFile }) {
      const input = { prompt, ...(options.extra || {}) };
      if (options.size === "image_size") input.image_size = { width, height };
      else input.aspect_ratio = aspectOf(width, height);
      if (negative && options.negative_field) input[options.negative_field] = negative;

      // Reference images (character / style consistency) go through the model's edit endpoint.
      let endpoint = model;
      if (refFiles.length && options.edit_model) {
        endpoint = options.edit_model;
        input.image_urls = await Promise.all(refFiles.slice(0, options.max_refs || 4).map(upload));
      }
      const out = await runQueue(endpoint, input);
      const url = firstUrl(out.images, out.image);
      if (!url) throw new Error(`fal ${endpoint}: 결과에 이미지가 없습니다`);
      await downloadTo(url, outFile);
      return { file: outFile, costUsd: priceOf(`fal/${model}`, "per_image") };
    },

    /** { imageFile, prompt, duration, width, height, outFile } → { file, hasAudio } */
    async video({ model, options, imageFile, prompt, duration, width, height, outFile }) {
      const min = options.duration_min ?? 3;
      const max = options.duration_max ?? 10;
      const seconds = Math.min(max, Math.max(min, Math.ceil(duration)));
      const input = { prompt, ...(options.extra || {}) };
      input[options.image_field || "image_url"] = await upload(imageFile);
      input.duration = options.duration_type === "string" ? String(seconds) : seconds;
      if (options.aspect_param) input.aspect_ratio = aspectOf(width, height);
      if (options.audio_field) input[options.audio_field] = false; // narration and music are ours

      const out = await runQueue(model, input, { timeoutMs: 25 * 60_000 });
      const url = firstUrl(out.video, out.videos);
      if (!url) throw new Error(`fal ${model}: 결과에 영상이 없습니다`);
      await downloadTo(url, outFile);
      return { file: outFile, costUsd: priceOf(`fal/${model}`, "per_second") * seconds };
    },

    /** { prompt, duration, outFile } → { file } */
    async music({ model, options, prompt, duration, outFile }) {
      const input = { prompt, ...(options.extra || {}) };
      if (options.length_field) input[options.length_field] = options.length_unit === "ms" ? Math.round(duration * 1000) : Math.ceil(duration);
      const out = await runQueue(model, input);
      const url = firstUrl(out.audio, out.audio_file);
      if (!url) throw new Error(`fal ${model}: 결과에 오디오가 없습니다`);
      await downloadTo(url, outFile);
      return { file: outFile, costUsd: priceOf(`fal/${model}`, "per_minute") * Math.ceil(duration / 60) + priceOf(`fal/${model}`, "per_track") };
    },
  },
};
