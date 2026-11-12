// One-step launcher used by start-windows.bat / start-mac.command / start-linux.sh.
// Installs what is missing (npm packages, a headless browser for the motion-graphics
// renderer), starts the studio, and opens it in the default browser.
// Only Node built-ins are imported up front: packages may not be installed yet.
import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
process.chdir(ROOT);

const PORT = Number(process.env.PORT || 4400);
const URL_TO_OPEN = `http://localhost:${PORT}`;
const say = (msg) => console.log(`\n▶ ${msg}`);
const fail = (msg) => {
  console.error(`\n✗ ${msg}\n`);
  process.exit(1);
};

const major = Number(process.versions.node.split(".")[0]);
if (major < 20) fail(`Node.js 20 이상이 필요합니다 (현재 ${process.versions.node}).`);

/** npm's own JS entry next to this node binary — avoids spawning npm.cmd on Windows. */
function npmCli() {
  const dir = path.dirname(process.execPath);
  const candidates = [path.join(dir, "node_modules", "npm", "bin", "npm-cli.js"), path.join(dir, "..", "lib", "node_modules", "npm", "bin", "npm-cli.js")];
  return candidates.find((p) => fs.existsSync(p)) || null;
}

function runNpm(args) {
  const cli = npmCli();
  const r = cli
    ? spawnSync(process.execPath, [cli, ...args], { stdio: "inherit" })
    : spawnSync(process.platform === "win32" ? "npm.cmd" : "npm", args, { stdio: "inherit", shell: process.platform === "win32" });
  return r.status === 0;
}

function needsInstall() {
  const marker = path.join(ROOT, "node_modules", ".package-lock.json");
  if (!fs.existsSync(marker)) return true;
  const installed = fs.statSync(marker).mtimeMs;
  return ["package.json", "package-lock.json"].some((f) => fs.existsSync(f) && fs.statSync(f).mtimeMs > installed + 1000);
}

function ping() {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${PORT}/api/state`, { timeout: 1500 }, (res) => {
      res.resume();
      resolve(res.statusCode > 0);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

function openBrowser(url) {
  const [cmd, args] =
    process.platform === "win32" ? ["cmd", ["/c", "start", "", url]] : process.platform === "darwin" ? ["open", [url]] : ["xdg-open", [url]];
  try {
    const child = spawn(cmd, args, { stdio: "ignore", detached: true, windowsHide: true });
    child.on("error", () => {}); // no opener available (e.g. a server without a desktop): the URL is printed below
    child.unref();
  } catch {
    /* the URL is printed below anyway */
  }
  console.log(`\n  브라우저에서 열기: ${url}\n`);
}

// 0) Already running (e.g. the launcher was double-clicked twice)? Just open it.
if (await ping()) {
  say("스튜디오가 이미 켜져 있습니다.");
  openBrowser(URL_TO_OPEN);
  process.exit(0);
}

// 1) Packages (includes portable ffmpeg/ffprobe, so nothing else needs installing).
if (needsInstall()) {
  say("필요한 프로그램을 내려받는 중입니다 (처음 한 번만, 몇 분 걸릴 수 있습니다)…");
  if (!runNpm(["install", "--no-audit", "--no-fund"])) fail("npm install 에 실패했습니다. 인터넷 연결을 확인하고 다시 실행해 주세요.");
}

// 2) ffmpeg
const { FFMPEG } = await import(pathToFileURL(path.join(ROOT, "server/media/bin.js")).href);
if (spawnSync(FFMPEG, ["-version"], { stdio: "ignore", windowsHide: true }).status !== 0) {
  say("영상 편집 엔진(ffmpeg)을 다시 받는 중…");
  fs.rmSync(path.join(ROOT, "node_modules", "ffmpeg-static"), { recursive: true, force: true });
  runNpm(["install", "--no-audit", "--no-fund"]);
  fail("ffmpeg 을 준비하지 못했습니다. 다시 실행해 보고, 계속 실패하면 https://ffmpeg.org/download.html 에서 설치하세요.");
}

// 3) A browser engine for the motion-graphics renderer: Chrome/Edge if present, otherwise download Chromium once.
const render = await import(pathToFileURL(path.join(ROOT, "server/media/render.js")).href);
try {
  await render.getBrowser();
} catch {
  say("모션그래픽 렌더러용 브라우저 엔진을 내려받는 중 (처음 한 번만)…");
  const cli = path.join(ROOT, "node_modules", "playwright-core", "cli.js");
  // Only the headless shell is needed for rendering (~290MB instead of ~700MB for full Chromium).
  const r = spawnSync(process.execPath, [cli, "install", "--only-shell", "chromium"], { stdio: "inherit" });
  if (r.status !== 0) fail("브라우저 엔진을 내려받지 못했습니다. Chrome 을 설치한 뒤 다시 실행해 주세요.");
  try {
    await render.getBrowser();
  } catch (err) {
    const hint = process.platform === "linux" ? "\n  리눅스라면: sudo npx playwright-core install-deps chromium" : "";
    fail(`브라우저 엔진을 실행하지 못했습니다: ${String(err.message).split("\n")[0]}${hint}`);
  }
}
await render.closeBrowser();

// 4) Start the studio and open it.
say("스튜디오를 켜는 중…");
const server = spawn(process.execPath, ["server/index.js"], { stdio: "inherit", env: process.env });
const stop = () => server.kill("SIGINT");
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
server.on("exit", (code) => process.exit(code ?? 0));

for (let i = 0; i < 120; i++) {
  await new Promise((r) => setTimeout(r, 500));
  if (server.exitCode !== null) break;
  if (await ping()) {
    openBrowser(URL_TO_OPEN);
    console.log("  이 창을 닫거나 Ctrl+C 를 누르면 스튜디오가 꺼집니다.\n");
    break;
  }
}
