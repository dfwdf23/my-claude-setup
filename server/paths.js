import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

export const ROOT = path.resolve(here, "..");
export const STUDIO_DIR = path.join(ROOT, "studio");
export const WEB_DIR = path.join(ROOT, "web");
export const DATA_DIR = path.resolve(process.env.STUDIO_DATA_DIR || path.join(ROOT, "data"));
export const PROJECTS_DIR = path.join(DATA_DIR, "projects");
export const PLAYBOOK_DIR = path.join(DATA_DIR, "playbook");
export const OUTBOX_DIR = path.join(DATA_DIR, "outbox");
export const NODE_MODULES = path.join(ROOT, "node_modules");

/** Resolve `rel` inside `base`, refusing anything that escapes it. */
export function safeJoin(base, rel) {
  const target = path.resolve(base, "." + path.sep + rel);
  if (target !== base && !target.startsWith(base + path.sep)) {
    throw Object.assign(new Error("경로가 허용 범위를 벗어났습니다"), { status: 400 });
  }
  return target;
}
