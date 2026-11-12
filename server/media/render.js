import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { NODE_MODULES, DATA_DIR } from "../paths.js";
import { FFMPEG } from "./bin.js";

/**
 * Renders motion-graphics compositions with headless Chromium.
 *   renderOverlay → transparent video (captions, titles, numbers …) to lay over footage
 *   renderStill   → a single image (stand-in scene art, thumbnails)
 *
 * The page is served from a virtual origin by request interception: fonts come
 * from node_modules, images from the data folder, and nothing reaches the network.
 */

const here = path.dirname(fileURLToPath(import.meta.url));
const ORIGIN = "http://studio.render";

/** Font family → stylesheet(s). Add a family here after `npm i @fontsource/<name>`. */
const FONT_SHEETS = {
  "Black Han Sans": ["@fontsource/black-han-sans/400.css"],
  "Do Hyeon": ["@fontsource/do-hyeon/400.css"],
  Jua: ["@fontsource/jua/400.css"],
  Gaegu: ["@fontsource/gaegu/400.css", "@fontsource/gaegu/700.css"],
  "Gowun Batang": ["@fontsource/gowun-batang/400.css", "@fontsource/gowun-batang/700.css"],
  "Bagel Fat One": ["@fontsource/bagel-fat-one/400.css"],
  Orbit: ["@fontsource/orbit/400.css"],
};
export const FONT_FAMILIES = ["Pretendard", ...Object.keys(FONT_SHEETS)];

const MIME = { ".css": "text/css", ".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".js": "text/javascript" };

let browserPromise;

async function launch() {
  const args = ["--font-render-hinting=none", "--disable-dev-shm-usage"];
  if (process.getuid?.() === 0) args.push("--no-sandbox"); // Chromium refuses to sandbox as root (containers)
  const attempts = [];
  if (process.env.CHROMIUM_PATH) attempts.push({ executablePath: process.env.CHROMIUM_PATH });
  if (fs.existsSync("/opt/pw-browsers/chromium")) attempts.push({ executablePath: "/opt/pw-browsers/chromium" });
  attempts.push({}, { channel: "chrome" }, { channel: "msedge" });
  let last;
  for (const opt of attempts) {
    try {
      return await chromium.launch({ headless: true, args, ...opt });
    } catch (err) {
      last = err;
    }
  }
  throw new Error(`모션그래픽 렌더러용 브라우저를 찾지 못했습니다. Chrome을 설치하거나 'npx playwright-core install chromium' 을 실행하세요. (${String(last?.message).split("\n")[0]})`);
}

export async function getBrowser() {
  if (!browserPromise) {
    browserPromise = launch().then((b) => {
      b.on("disconnected", () => (browserPromise = null));
      return b;
    });
    browserPromise.catch(() => (browserPromise = null));
  }
  return browserPromise;
}

export async function closeBrowser() {
  const b = await browserPromise?.catch(() => null);
  browserPromise = null;
  await b?.close().catch(() => {});
}

/** Turn a local file into a URL the composition page can load. */
export const fileUrl = (abs) => `${ORIGIN}/file/${Buffer.from(abs).toString("base64url")}`;

function pageHtml(families) {
  const links = families.flatMap((f) => FONT_SHEETS[f] || []).map((sheet) => `<link rel="stylesheet" href="/fonts/${sheet}">`);
  return `<!doctype html><html><head><meta charset="utf-8">
${links.join("\n")}
<style>
@font-face{font-family:"Pretendard";font-weight:45 920;font-style:normal;src:url("/fonts/pretendard/dist/web/variable/woff2/PretendardVariable.woff2") format("woff2-variations")}
*{box-sizing:border-box;font-synthesis:none}
html,body{margin:0;padding:0;background:transparent;overflow:hidden}
</style></head><body><div id="root"></div><script src="/runtime.js"></script></body></html>`;
}

async function openPage(comp) {
  const browser = await getBrowser();
  const page = await browser.newPage({ viewport: { width: comp.width, height: comp.height }, deviceScaleFactor: 1 });
  const families = [...new Set([comp.style?.fonts?.display, comp.style?.fonts?.body].filter((f) => FONT_SHEETS[f]))];

  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.abort();
    try {
      if (url.pathname === "/comp.html") return route.fulfill({ contentType: "text/html; charset=utf-8", body: pageHtml(families) });
      if (url.pathname === "/runtime.js") return route.fulfill({ contentType: "text/javascript", path: path.join(here, "motion-runtime.js") });
      if (url.pathname.startsWith("/fonts/")) {
        const rel = decodeURIComponent(url.pathname.slice(7));
        const abs = path.resolve(NODE_MODULES, rel);
        if (!abs.startsWith(path.join(NODE_MODULES, "pretendard")) && !abs.startsWith(path.join(NODE_MODULES, "@fontsource"))) return route.abort();
        return route.fulfill({ contentType: MIME[path.extname(abs)] || "application/octet-stream", path: abs });
      }
      if (url.pathname.startsWith("/file/")) {
        const abs = path.resolve(Buffer.from(url.pathname.slice(6), "base64url").toString());
        if (!abs.startsWith(DATA_DIR + path.sep)) return route.abort();
        return route.fulfill({ contentType: MIME[path.extname(abs).toLowerCase()] || "application/octet-stream", path: abs });
      }
    } catch {
      /* fall through */
    }
    return route.abort();
  });

  await page.goto(`${ORIGIN}/comp.html`);
  await page.evaluate(async (c) => {
    window.__setup(c);
    // Pull in every glyph the composition will ever show (typed-on text, counting numbers)
    // before the first frame, so no frame is captured with a fallback font.
    const text = JSON.stringify(c.layers) + "0123456789,.%+-×~";
    const fams = new Set(["Pretendard", c.style?.fonts?.display, c.style?.fonts?.body].filter(Boolean));
    await Promise.all([...fams].flatMap((f) => [400, 700, 800].map((w) => document.fonts.load(`${w} 40px "${f}"`, text).catch(() => {}))));
    await document.fonts.ready;
    if (c.background?.image) {
      await new Promise((resolve) => {
        const img = new Image();
        img.onload = img.onerror = resolve;
        img.src = c.background.image;
      });
    }
  }, comp);
  return page;
}

/** Render one image. Opaque when the composition has a background, otherwise transparent PNG. */
export async function renderStill(comp, outFile, { t = comp.duration ?? 0 } = {}) {
  const page = await openPage(comp);
  try {
    await page.evaluate((time) => window.__frame(time), t);
    const jpeg = /\.jpe?g$/i.test(outFile);
    await page.screenshot({ path: outFile, type: jpeg ? "jpeg" : "png", quality: jpeg ? 92 : undefined, omitBackground: !comp.background });
    return outFile;
  } finally {
    await page.close().catch(() => {});
  }
}

/**
 * Render a transparent overlay video (QuickTime Animation, RGBA).
 * Frames whose state token did not change are not re-captured.
 */
export async function renderOverlay(comp, outFile, { onProgress } = {}) {
  const fps = comp.fps || 30;
  const total = Math.max(1, Math.round(comp.duration * fps));
  await fsp.mkdir(path.dirname(outFile), { recursive: true });

  const page = await openPage({ ...comp, background: null });
  const ff = spawn(FFMPEG, ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(fps), "-c:v", "png", "-i", "-", "-c:v", "qtrle", "-pix_fmt", "argb", "-r", String(fps), outFile], { stdio: ["pipe", "ignore", "pipe"], windowsHide: true });
  let ffErr = "";
  ff.stderr.on("data", (d) => (ffErr += d));
  ff.stdin.on("error", () => {}); // surfaced through the exit code below
  const closed = once(ff, "close");

  let captured = 0;
  try {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Emulation.setDefaultBackgroundColorOverride", { color: { r: 0, g: 0, b: 0, a: 0 } });
    let lastToken = null;
    let buf = null;
    for (let f = 0; f < total; f++) {
      const token = await page.evaluate((time) => window.__frame(time), f / fps);
      if (token !== lastToken || !buf) {
        const shot = await cdp.send("Page.captureScreenshot", { format: "png", optimizeForSpeed: true });
        buf = Buffer.from(shot.data, "base64");
        lastToken = token;
        captured++;
      }
      if (ff.exitCode !== null) break;
      if (!ff.stdin.write(buf)) await once(ff.stdin, "drain");
      if (onProgress && f % 30 === 0) onProgress(f / total);
    }
  } finally {
    ff.stdin.end();
    await page.close().catch(() => {});
  }
  const [code] = await closed;
  if (code !== 0) throw new Error(`오버레이 인코딩 실패: ${ffErr.trim().split("\n").slice(-3).join(" | ")}`);
  return { file: outFile, frames: total, captured };
}
