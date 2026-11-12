import { spawn } from "node:child_process";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
export const round = (v, d = 2) => Math.round(v * 10 ** d) / 10 ** d;

/** Run a command, collect output, reject with stderr tail on non-zero exit. */
export function run(cmd, args, { input, timeoutMs = 20 * 60_000, cwd } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { cwd, stdio: [input === undefined ? "ignore" : "pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`${cmd} 실행이 ${Math.round(timeoutMs / 1000)}초를 넘어 중단했습니다`));
    }, timeoutMs);
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => {
      stderr += d;
      if (stderr.length > 200_000) stderr = stderr.slice(-100_000);
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err.code === "ENOENT" ? new Error(`${cmd} 이(가) 설치되어 있지 않습니다`) : err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`${cmd} 실패 (exit ${code}): ${stderr.trim().split("\n").slice(-6).join(" | ")}`));
    });
    if (input !== undefined) child.stdin.end(input);
  });
}

/** Run `worker` over `items` with at most `limit` in flight; preserves order. */
export async function mapLimit(items, limit, worker) {
  const results = new Array(items.length);
  let next = 0;
  const lanes = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, async () => {
    while (next < items.length) {
      const i = next++;
      results[i] = await worker(items[i], i);
    }
  });
  await Promise.all(lanes);
  return results;
}

/** fetch + JSON with readable errors (provider adapters use this). */
export async function httpJson(url, { method = "GET", headers = {}, body, timeoutMs = 120_000, label = "요청" } = {}) {
  const res = await fetch(url, {
    method,
    headers: body && !(body instanceof Uint8Array) && typeof body !== "string" ? { "content-type": "application/json", ...headers } : headers,
    body: body && !(body instanceof Uint8Array) && typeof body !== "string" ? JSON.stringify(body) : body,
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    const detail = data?.error?.message || data?.detail?.message || data?.detail || data?.message || data?.raw || text;
    const err = new Error(`${label} 실패 (HTTP ${res.status}): ${typeof detail === "string" ? detail.slice(0, 300) : JSON.stringify(detail).slice(0, 300)}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export async function downloadTo(url, file, { headers = {}, timeoutMs = 300_000 } = {}) {
  const { writeFile } = await import("node:fs/promises");
  const res = await fetch(url, { headers, signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`다운로드 실패 (HTTP ${res.status})`);
  await writeFile(file, Buffer.from(await res.arrayBuffer()));
  return file;
}

/** Count Hangul syllables + other word characters — a decent proxy for spoken length. */
export function speechUnits(text) {
  let n = 0;
  for (const ch of text) {
    if (/[가-힣]/.test(ch)) n += 1;
    else if (/[a-zA-Z]/.test(ch)) n += 0.45;
    else if (/[0-9]/.test(ch)) n += 1.6;
    else if (/[,，、]/.test(ch)) n += 1.2;
    else if (/[.!?。…]/.test(ch)) n += 2.2;
  }
  return n;
}
