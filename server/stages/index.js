import { research, plan, script, storyboard } from "./pre.js";
import { voice, visuals, motion, sound, pack } from "./production.js";
import { edit, qa, publish, analyze } from "./post.js";

/**
 * Stage id (pipeline.yaml) → implementation. A stage returns a one-line
 * summary for the dashboard, or { summary, revise: [...] } to send work back.
 */
export const STAGES = { research, plan, script, storyboard, voice, visuals, motion, sound, package: pack, edit, qa, publish, analyze };
