import { EventEmitter } from "node:events";

/**
 * In-process event bus. Everything the dashboard shows live flows through here
 * and is fanned out to browsers over SSE.
 */
class Bus extends EventEmitter {
  constructor() {
    super();
    this.setMaxListeners(200);
    this.activity = []; // ring buffer for the "지금 일하는 AI" feed
    this.seq = 0;
  }

  /** Broadcast a state change to every connected dashboard. */
  send(type, payload = {}) {
    this.emit("event", { type, payload, at: Date.now() });
  }

  /** Record a human-readable line in the live activity feed. */
  log(entry) {
    const item = { id: ++this.seq, at: Date.now(), level: "info", ...entry };
    this.activity.push(item);
    if (this.activity.length > 300) this.activity.splice(0, this.activity.length - 300);
    this.send("activity", item);
    return item;
  }
}

export const bus = new Bus();
