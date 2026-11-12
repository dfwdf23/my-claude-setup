import { config, getChannel, getFormat, getStyle } from "./config.js";

/** The studio settings that apply to one project, resolved fresh each time (so config edits apply immediately). */
export function contextFor(project) {
  const format = getFormat(project.brief.format);
  const channel = getChannel(project.brief.channel);
  const style = project.styleId ? getStyle(project.styleId) : null;
  const mix = style?.visual_mix || format.visual_mix || { video: 0.5, image: 0.4, motion: 0.1 };
  return { project, channel, format, style, mix };
}

export function budgetFor(project) {
  const b = config.studio.budget?.per_project_usd;
  const base = typeof b === "number" ? b : (b?.[project.brief.format] ?? 20);
  return base + (project.budgetExtra || 0);
}

export class BudgetError extends Error {
  constructor(spent, limit) {
    super(`예산 상한 도달 — 추정 $${spent.toFixed(2)} / 상한 $${limit.toFixed(2)}`);
    this.budget = true;
  }
}

/** Add an estimated cost to the project and stop the stage if the cap is passed. */
export function spender(project) {
  return (costUsd = 0, use = "") => {
    if (!costUsd) return;
    project.cost = Math.round(((project.cost || 0) + costUsd) * 10000) / 10000;
    project.costBy ||= {};
    project.costBy[use] = Math.round(((project.costBy[use] || 0) + costUsd) * 10000) / 10000;
    const limit = budgetFor(project);
    if (project.cost > limit) throw new BudgetError(project.cost, limit);
  };
}
