import fs from "node:fs/promises";
import { secret, hasSecret } from "../secrets.js";
import { httpJson } from "../util.js";
import { priceOf } from "./registry.js";

/**
 * ElevenLabs — voice (with per-character timestamps for caption sync), music, sound effects.
 *
 * Endpoints verified against ElevenLabs' OpenAPI spec on 2026-11-12:
 *   POST /v1/text-to-speech/{voice_id}/with-timestamps?output_format=…
 *   GET  /v2/voices
 *   POST /v1/music            (binary audio)
 *   POST /v1/sound-generation (binary audio)
 */

const BASE = "https://api.elevenlabs.io";
const headers = () => ({ "xi-api-key": secret("ELEVENLABS_API_KEY") });

let voiceCache = { at: 0, voices: [] };
async function listVoices() {
  if (Date.now() - voiceCache.at < 10 * 60_000) return voiceCache.voices;
  const data = await httpJson(`${BASE}/v2/voices?page_size=100`, { headers: headers(), label: "ElevenLabs 목소리 목록" });
  voiceCache = { at: Date.now(), voices: data.voices || [] };
  return voiceCache.voices;
}

/** Pick a voice: an explicit id wins; otherwise prefer one verified for the language. */
async function pickVoice(voice, language) {
  if (voice?.id) return voice.id;
  const voices = await listVoices();
  if (!voices.length) throw new Error("ElevenLabs 계정에 사용할 수 있는 목소리가 없습니다");
  const byName = voice?.name && voices.find((v) => v.name?.toLowerCase().includes(voice.name.toLowerCase()));
  if (byName) return byName.voice_id;
  const native = voices.find((v) => (v.verified_languages || []).some((l) => l.language === language));
  return (native || voices[0]).voice_id;
}

/** Character timestamps → word timestamps. */
export function wordsFromAlignment(alignment) {
  const chars = alignment?.characters || [];
  const starts = alignment?.character_start_times_seconds || [];
  const ends = alignment?.character_end_times_seconds || [];
  const words = [];
  let cur = null;
  for (let i = 0; i < chars.length; i++) {
    if (/\s/.test(chars[i])) {
      if (cur) words.push(cur);
      cur = null;
    } else if (!cur) cur = { w: chars[i], start: starts[i], end: ends[i] };
    else {
      cur.w += chars[i];
      cur.end = ends[i];
    }
  }
  if (cur) words.push(cur);
  return words;
}

async function binary(url, body, label) {
  const res = await fetch(url, { method: "POST", headers: { ...headers(), "content-type": "application/json" }, body: JSON.stringify(body), signal: AbortSignal.timeout(600_000) });
  if (!res.ok) {
    const text = await res.text();
    throw Object.assign(new Error(`${label} 실패 (HTTP ${res.status}): ${text.slice(0, 240)}`), { status: res.status });
  }
  return Buffer.from(await res.arrayBuffer());
}

export default {
  id: "elevenlabs",
  name: "ElevenLabs",
  keys: ["ELEVENLABS_API_KEY"],
  available: () => hasSecret("ELEVENLABS_API_KEY"),
  listVoices,

  capabilities: {
    /** { text, voice, language, previousText, nextText, outFile } → { file, words } */
    async tts({ model, options, text, voice, language = "ko", previousText, nextText, outFile }) {
      const voiceId = await pickVoice(voice, language);
      const body = { text, model_id: model };
      // eleven_multilingual_v2 detects the language itself and rejects language_code.
      if (model !== "eleven_multilingual_v2") body.language_code = language;
      if (options.voice_settings !== false) {
        body.voice_settings = { stability: 0.45, similarity_boost: 0.8, ...(options.voice_settings || {}) };
        // Only models with a speed control accept it.
        if (options.speed_control && voice?.speed) body.voice_settings.speed = Math.min(1.2, Math.max(0.7, voice.speed));
      }
      if (previousText) body.previous_text = previousText;
      if (nextText) body.next_text = nextText;

      const format = options.output_format || "mp3_44100_128";
      const data = await httpJson(`${BASE}/v1/text-to-speech/${voiceId}/with-timestamps?output_format=${format}`, {
        method: "POST",
        headers: headers(),
        body,
        timeoutMs: 300_000,
        label: `ElevenLabs 음성 합성(${model})`,
      });
      if (!data.audio_base64) throw new Error("ElevenLabs: 응답에 오디오가 없습니다");
      await fs.writeFile(outFile, Buffer.from(data.audio_base64, "base64"));
      // Prefer the alignment for the text as written — captions use the original words.
      const words = wordsFromAlignment(data.alignment || data.normalized_alignment);
      return { file: outFile, words, costUsd: priceOf(`elevenlabs/${model}`, "per_1k_chars") * (text.length / 1000) };
    },

    /** { prompt, duration, outFile } → { file } */
    async music({ model, prompt, duration, outFile }) {
      const ms = Math.min(600_000, Math.max(3000, Math.round(duration * 1000)));
      const audio = await binary(`${BASE}/v1/music?output_format=auto`, { prompt: prompt.slice(0, 4000), music_length_ms: ms, model_id: model, force_instrumental: true }, "ElevenLabs 음악 생성");
      await fs.writeFile(outFile, audio);
      return { file: outFile, costUsd: priceOf(`elevenlabs/${model}`, "per_minute") * Math.ceil(duration / 60) };
    },

    /** { text, duration, outFile } → { file } */
    async sfx({ model, text, duration, outFile }) {
      const body = { text: text.slice(0, 450), prompt_influence: 0.4, model_id: model };
      if (duration) body.duration_seconds = Math.min(30, Math.max(0.5, duration));
      await fs.writeFile(outFile, await binary(`${BASE}/v1/sound-generation?output_format=mp3_44100_128`, body, "ElevenLabs 효과음 생성"));
      return { file: outFile, costUsd: 0 };
    },
  },
};
