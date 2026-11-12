import fs from "node:fs/promises";
import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { secret, hasSecret } from "../secrets.js";
import { priceOf } from "./registry.js";

/**
 * Claude — the brain of the team (planning, writing, direction, QA, learning).
 *
 * Requests stream so long scripts never hit an HTTP timeout, and answers are
 * constrained to the stage's JSON schema (structured outputs), so a stage
 * always receives data it can use.
 */

let cached = { key: null, client: null };
function client() {
  const key = secret("ANTHROPIC_API_KEY");
  if (cached.key !== key) cached = { key, client: key ? new Anthropic({ apiKey: key }) : new Anthropic() };
  return cached.client;
}

// Models whose safety classifiers can decline a request; for these we ask the
// API to re-run a declined request on a fallback model inside the same call.
const SUPPORTS_FALLBACKS = /^claude-(fable-5|mythos-5|opus-5|sonnet-5-5)/;

function baseParams(model, effort, maxTokens) {
  const params = { model, max_tokens: maxTokens, output_config: { effort } };
  if (SUPPORTS_FALLBACKS.test(model)) {
    params.betas = ["server-side-fallback-2026-07-01"];
    params.fallbacks = "default";
  }
  return params;
}

function textOf(message) {
  return message.content
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");
}

function checkStop(message) {
  if (message.stop_reason === "refusal") {
    const why = message.stop_details?.explanation || message.stop_details?.category || "사유 미상";
    throw new Error(`모델이 이 요청을 거절했습니다 (${why}). 주제나 지시문을 조정해 주세요.`);
  }
  if (message.stop_reason === "max_tokens") {
    throw new Error("응답이 길이 한도에서 잘렸습니다 — 영상 길이를 줄이거나 장면 수를 나눠 주세요.");
  }
}

function costOf(model, usage) {
  const use = `anthropic/${model}`;
  const input = (usage?.input_tokens || 0) + (usage?.cache_read_input_tokens || 0) + (usage?.cache_creation_input_tokens || 0);
  return (input * priceOf(use, "input_mtok") + (usage?.output_tokens || 0) * priceOf(use, "output_mtok")) / 1_000_000;
}

function friendly(err) {
  // Wrong key / no access will not fix itself on a retry: fail the stage straight away.
  if (err instanceof Anthropic.AuthenticationError) return Object.assign(new Error("Anthropic API 키가 올바르지 않습니다 — 설정에서 키를 확인하세요."), { fatal: true, status: 401 });
  if (err instanceof Anthropic.PermissionDeniedError) return Object.assign(new Error("이 API 키로는 해당 모델을 쓸 수 없습니다."), { fatal: true, status: 403 });
  if (err instanceof Anthropic.RateLimitError) return Object.assign(new Error("Anthropic 사용량 한도에 걸렸습니다 — 잠시 후 자동으로 다시 시도합니다."), { retryable: true });
  if (err instanceof Anthropic.BadRequestError) return new Error(`Anthropic 요청 오류: ${err.message}`);
  if (err instanceof Anthropic.APIConnectionError) return Object.assign(new Error("Anthropic 서버에 연결하지 못했습니다."), { retryable: true });
  if (err instanceof Anthropic.APIError) return Object.assign(new Error(`Anthropic 오류 (${err.status}): ${err.message}`), { retryable: (err.status || 0) >= 500 });
  return err;
}

async function imageBlocks(images = []) {
  const blocks = [];
  for (const img of images) {
    const data = img.data || (await fs.readFile(img.file)).toString("base64");
    blocks.push({ type: "image", source: { type: "base64", media_type: img.mediaType || "image/jpeg", data } });
  }
  return blocks;
}

export default {
  id: "anthropic",
  name: "Anthropic Claude",
  keys: ["ANTHROPIC_API_KEY"],
  available: () => hasSecret("ANTHROPIC_API_KEY") || Boolean(process.env.ANTHROPIC_AUTH_TOKEN),

  capabilities: {
    /** { system, user, schema (zod), effort, maxTokens, images } → { data, usage, costUsd } */
    async llm({ model: routed, agentModel, system, user, schema, effort = "high", maxTokens = 32000, images }) {
      // A team member may pin its own Claude model in studio/agents/<id>.md.
      const model = agentModel?.startsWith("claude-") ? agentModel : routed;
      try {
        const stream = client().beta.messages.stream({
          ...baseParams(model, effort, maxTokens),
          output_config: { effort, format: zodOutputFormat(schema) },
          system,
          messages: [{ role: "user", content: [...(await imageBlocks(images)), { type: "text", text: user }] }],
        });
        const message = await stream.finalMessage();
        checkStop(message);
        let json;
        try {
          json = JSON.parse(textOf(message));
        } catch {
          throw Object.assign(new Error("모델 응답을 JSON으로 읽지 못했습니다."), { retryable: true });
        }
        return { data: schema.parse(json), usage: message.usage, costUsd: costOf(model, message.usage), servedBy: message.model };
      } catch (err) {
        throw friendly(err);
      }
    },

    /** Free-form web research. { system, user, effort, maxSearches } → { text, sources, costUsd } */
    async search({ model: _tool, llmModel = "claude-opus-5-5", system, user, effort = "medium", maxSearches = 6 }) {
      try {
        const messages = [{ role: "user", content: user }];
        const sources = new Map();
        let text = "";
        let cost = 0;
        // A long server-tool turn can pause; send it back so the server continues.
        for (let turn = 0; turn < 6; turn++) {
          const stream = client().beta.messages.stream({
            ...baseParams(llmModel, effort, 16000),
            system,
            messages,
            tools: [{ type: "web_search_20260209", name: "web_search", max_uses: maxSearches }],
          });
          const message = await stream.finalMessage();
          cost += costOf(llmModel, message.usage);
          for (const block of message.content) {
            // Search errors come back as an object, results as a list.
            if (block.type === "web_search_tool_result" && Array.isArray(block.content)) {
              for (const r of block.content) if (r.type === "web_search_result") sources.set(r.url, { title: r.title, url: r.url });
            }
          }
          if (message.stop_reason === "pause_turn") {
            messages.push({ role: "assistant", content: message.content });
            continue;
          }
          checkStop(message);
          text = textOf(message);
          break;
        }
        return { text, sources: [...sources.values()].slice(0, 20), costUsd: cost };
      } catch (err) {
        throw friendly(err);
      }
    },
  },
};
