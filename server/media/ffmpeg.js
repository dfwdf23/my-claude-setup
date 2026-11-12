import fsp from "node:fs/promises";
import path from "node:path";
import { run } from "../util.js";
import { FFMPEG, FFPROBE } from "./bin.js";

/** ffmpeg / ffprobe building blocks used by the local video engine, the editor and QA. */

const FF = ["-y", "-hide_banner", "-loglevel", "error"];
const X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p"];

export async function probe(file) {
  const { stdout } = await run(FFPROBE, ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", file]);
  const info = JSON.parse(stdout);
  const v = info.streams.find((s) => s.codec_type === "video");
  const a = info.streams.find((s) => s.codec_type === "audio");
  const [n, d] = (v?.avg_frame_rate || "0/1").split("/").map(Number);
  return {
    duration: Number(info.format?.duration || v?.duration || a?.duration || 0),
    size: Number(info.format?.size || 0),
    width: v?.width || 0,
    height: v?.height || 0,
    fps: d ? n / d : 0,
    videoCodec: v?.codec_name || null,
    audioCodec: a?.codec_name || null,
    hasAudio: Boolean(a),
  };
}

export const durationOf = async (file) => (await probe(file)).duration;

/** Camera moves for a still image. Rendered at 2× then scaled down so the motion is smooth. */
const MOVES = {
  push_in: (N) => ({ z: `1+0.14*on/${N}`, x: "iw/2-(iw/zoom/2)", y: "ih/2-(ih/zoom/2)" }),
  pull_out: (N) => ({ z: `1.14-0.14*on/${N}`, x: "iw/2-(iw/zoom/2)", y: "ih/2-(ih/zoom/2)" }),
  pan_left: (N) => ({ z: "1.12", x: `(iw-iw/zoom)*(1-on/${N})`, y: "ih/2-(ih/zoom/2)" }),
  pan_right: (N) => ({ z: "1.12", x: `(iw-iw/zoom)*on/${N}`, y: "ih/2-(ih/zoom/2)" }),
  tilt_up: (N) => ({ z: "1.12", x: "iw/2-(iw/zoom/2)", y: `(ih-ih/zoom)*(1-on/${N})` }),
  tilt_down: (N) => ({ z: "1.12", x: "iw/2-(iw/zoom/2)", y: `(ih-ih/zoom)*on/${N}` }),
  static: (N) => ({ z: `1.03+0.02*on/${N}`, x: "iw/2-(iw/zoom/2)", y: "ih/2-(ih/zoom/2)" }),
};

/** Still image → moving clip (Ken Burns). */
export async function kenBurns({ imageFile, outFile, width, height, fps = 30, duration, camera = "push_in" }) {
  const N = Math.max(2, Math.round(duration * fps));
  const m = (MOVES[camera] || MOVES.push_in)(N);
  const vf = [
    `scale=${width * 2}:${height * 2}:force_original_aspect_ratio=increase:flags=bicubic`,
    `crop=${width * 2}:${height * 2}`,
    `zoompan=z='${m.z}':x='${m.x}':y='${m.y}':d=1:s=${width}x${height}:fps=${fps}`,
  ].join(",");
  await run(FFMPEG, [...FF, "-framerate", String(fps), "-loop", "1", "-i", imageFile, "-vf", vf, "-frames:v", String(N), "-r", String(fps), ...X264, "-an", outFile]);
  return outFile;
}

/** Fit a generated clip to the canvas and to the exact scene length (trim, or slow slightly then hold the last frame). */
export async function fitClip({ inFile, outFile, width, height, fps = 30, duration }) {
  const have = await durationOf(inFile);
  const stretch = have > 0 && have < duration ? Math.min(1.2, duration / have) : 1;
  const vf = [
    `scale=${width}:${height}:force_original_aspect_ratio=increase`,
    `crop=${width}:${height}`,
    stretch > 1 ? `setpts=${stretch.toFixed(4)}*PTS` : null,
    `fps=${fps}`,
    `tpad=stop_mode=clone:stop_duration=${Math.max(0, duration - have * stretch + 0.2).toFixed(3)}`,
  ].filter(Boolean).join(",");
  await run(FFMPEG, [...FF, "-i", inFile, "-vf", vf, "-t", duration.toFixed(3), "-r", String(fps), ...X264, "-an", outFile]);
  return outFile;
}

/** Lay a transparent overlay over a scene clip. */
export async function composite({ baseFile, overlayFile, outFile, fps = 30, duration }) {
  const args = [...FF, "-i", baseFile];
  if (overlayFile) args.push("-i", overlayFile, "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto,format=yuv420p[v]", "-map", "[v]");
  args.push("-t", duration.toFixed(3), "-r", String(fps), ...X264, "-video_track_timescale", "30000", "-an", outFile);
  await run(FFMPEG, args);
  return outFile;
}

/** Place a scene's voice clip inside a silent slot of exactly the scene length. */
export async function voiceSlot({ voiceFile, outFile, duration, lead = 0.12 }) {
  const ms = Math.round(lead * 1000);
  if (voiceFile) {
    await run(FFMPEG, [...FF, "-i", voiceFile, "-af", `adelay=${ms}|${ms},apad`, "-t", duration.toFixed(3), "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", outFile]);
  } else {
    await run(FFMPEG, [...FF, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", duration.toFixed(3), "-c:a", "pcm_s16le", outFile]);
  }
  return outFile;
}

export async function silence(outFile, duration) {
  return voiceSlot({ voiceFile: null, outFile, duration });
}

async function concatList(files, listFile) {
  // Forward slashes work on every OS and avoid backslash-escaping surprises in the concat list (Windows).
  await fsp.writeFile(listFile, files.map((f) => `file '${f.split(path.sep).join("/").replace(/'/g, "'\\''")}'`).join("\n"));
  return listFile;
}

export async function concatVideo(files, outFile) {
  const list = await concatList(files, outFile + ".txt");
  await run(FFMPEG, [...FF, "-f", "concat", "-safe", "0", "-i", list, "-c", "copy", outFile]);
  await fsp.rm(list, { force: true });
  return outFile;
}

export async function concatAudio(files, outFile) {
  const list = await concatList(files, outFile + ".txt");
  await run(FFMPEG, [...FF, "-f", "concat", "-safe", "0", "-i", list, "-c:a", "pcm_s16le", outFile]);
  await fsp.rm(list, { force: true });
  return outFile;
}

/**
 * Final mix + mux: narration over music, music ducks under the voice, then
 * loudness-normalised to the streaming target (-14 LUFS).
 */
export async function master({ videoFile, voiceFile, musicFile, outFile, duration, hasVoice = true, musicGain = 0.3 }) {
  const args = [...FF, "-i", videoFile, "-i", voiceFile];
  let filter;
  if (musicFile) {
    args.push("-stream_loop", "-1", "-i", musicFile);
    const fadeOut = Math.max(0, duration - 1.2).toFixed(2);
    const bed = `[2:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:${duration.toFixed(3)},afade=t=in:d=0.4,afade=t=out:st=${fadeOut}:d=1.2,volume=${hasVoice ? musicGain : 0.8}[bed]`;
    filter = hasVoice
      ? `${bed};[1:a]asplit=2[vo][key];[bed][key]sidechaincompress=threshold=0.02:ratio=9:attack=15:release=350[duck];[duck][vo]amix=inputs=2:normalize=0:duration=longest,loudnorm=I=-14:TP=-1.5:LRA=11[a]`
      : `${bed};[bed]loudnorm=I=-14:TP=-1.5:LRA=11[a]`;
  } else {
    filter = hasVoice ? "[1:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]" : "[1:a]anull[a]";
  }
  args.push("-filter_complex", filter, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", duration.toFixed(3), "-movflags", "+faststart", outFile);
  await run(FFMPEG, args);
  return outFile;
}

export async function grabFrame(videoFile, t, outFile, { width = 0 } = {}) {
  const args = [...FF, "-ss", Math.max(0, t).toFixed(3), "-i", videoFile, "-frames:v", "1"];
  if (width) args.push("-vf", `scale=${width}:-2`);
  args.push("-q:v", "4", outFile);
  await run(FFMPEG, args);
  return outFile;
}

/** Objective checks for QA: loudness, silent stretches, black frames. */
export async function measure(videoFile) {
  const info = await probe(videoFile);
  const out = { ...info, loudness: null, silences: [], blackSeconds: 0 };
  if (info.hasAudio) {
    const { stderr } = await run(FFMPEG, ["-hide_banner", "-nostats", "-i", videoFile, "-vn", "-af", "loudnorm=I=-14:print_format=json,silencedetect=noise=-45dB:d=2.5", "-f", "null", "-"]);
    const m = /"input_i"\s*:\s*"(-?[\d.]+|-inf)"/.exec(stderr);
    if (m) out.loudness = m[1] === "-inf" ? -99 : Number(m[1]);
    for (const s of stderr.matchAll(/silence_duration:\s*([\d.]+)/g)) out.silences.push(Number(s[1]));
  }
  const black = await run(FFMPEG, ["-hide_banner", "-nostats", "-i", videoFile, "-an", "-vf", "blackdetect=d=0.5:pix_th=0.08", "-f", "null", "-"]);
  for (const b of black.stderr.matchAll(/black_duration:\s*([\d.]+)/g)) out.blackSeconds += Number(b[1]);
  return out;
}

export const rel = (from, file) => path.relative(from, file).split(path.sep).join("/");
