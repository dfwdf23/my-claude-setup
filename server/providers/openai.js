import fs from "node:fs/promises";
import { secret, hasSecret } from "../secrets.js";
import { httpJson } from "../util.js";
import { priceOf } from "./registry.js";

/**
 * OpenAI — backup provider for images and voice.
 * Verified 2026-11-12: POST /v1/images/generations returns b64_json for GPT image
 * models; POST /v1/audio/speech returns binary audio (that endpoint is scheduled
 * for shutdown on 2027-01-06, so it sits last in the voice routing).
 */

const headers = () => ({ Authorization: `Bearer ${secret("OPENAI_API_KEY")}` });

export default {
  id: "openai",
  name: "OpenAI",
  keys: ["OPENAI_API_KEY"],
  available: () => hasSecret("OPENAI_API_KEY"),

  capabilities: {
    async image({ model, options, prompt, width, height, outFile }) {
      const size = width === height ? "1024x1024" : width > height ? "1536x1024" : "1024x1536";
      const data = await httpJson("https://api.openai.com/v1/images/generations", {
        method: "POST",
        headers: headers(),
        body: { model, prompt, size, quality: options.quality || "high", output_format: "png", n: 1 },
        timeoutMs: 300_000,
        label: `OpenAI 이미지(${model})`,
      });
      const b64 = data.data?.[0]?.b64_json;
      if (!b64) throw new Error("OpenAI: 응답에 이미지가 없습니다");
      await fs.writeFile(outFile, Buffer.from(b64, "base64"));
      return { file: outFile, costUsd: priceOf(`openai/${model}`, "per_image") };
    },

    /** No timestamps from this endpoint — the voice stage estimates word timing from the audio length. */
    async tts({ model, options, text, voice, outFile }) {
      const res = await fetch("https://api.openai.com/v1/audio/speech", {
        method: "POST",
        headers: { ...headers(), "content-type": "application/json" },
        body: JSON.stringify({
          model,
          input: text.slice(0, 4096),
          voice: options.voice || "marin",
          instructions: voice?.persona ? `Speak in Korean as: ${voice.persona}` : undefined,
          response_format: "mp3",
        }),
        signal: AbortSignal.timeout(300_000),
      });
      if (!res.ok) throw Object.assign(new Error(`OpenAI 음성 합성 실패 (HTTP ${res.status}): ${(await res.text()).slice(0, 240)}`), { status: res.status });
      await fs.writeFile(outFile, Buffer.from(await res.arrayBuffer()));
      return { file: outFile, words: null, costUsd: 0 };
    },
  },
};
