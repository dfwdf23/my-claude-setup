import crypto from "node:crypto";
import { secret, setSecret } from "./secrets.js";

/**
 * Signed, unguessable links to a project's media. Only needed when a platform
 * has to pull the video from us (Instagram Login tokens) and the studio is
 * reachable at PUBLIC_BASE_URL.
 */
function key() {
  let k = secret("MEDIA_SIGNING_KEY");
  if (!k) {
    k = crypto.randomBytes(32).toString("hex");
    setSecret("MEDIA_SIGNING_KEY", k);
  }
  return k;
}

const sign = (projectId, rel) => crypto.createHmac("sha256", key()).update(`${projectId}/${rel}`).digest("hex").slice(0, 40);

export function signedMediaUrl(projectId, rel) {
  const base = secret("PUBLIC_BASE_URL");
  const trimmed = base.endsWith("/") ? base.slice(0, -1) : base;
  return `${trimmed}/public/${sign(projectId, rel)}/${projectId}/${rel}`;
}

export function verifySignature(sig, projectId, rel) {
  const want = Buffer.from(sign(projectId, rel));
  const got = Buffer.from(String(sig));
  return want.length === got.length && crypto.timingSafeEqual(want, got);
}
