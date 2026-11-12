/* eslint-env browser */
/**
 * Motion graphics runtime — runs inside headless Chromium.
 *
 * A composition is plain data (layers with start/end times). Every layer is a
 * pure function of time: __frame(t) puts the page in the exact state for
 * second `t` and returns a token describing that state. The renderer captures
 * one screenshot per frame and reuses the previous one whenever the token is
 * unchanged, so held captions cost nothing.
 *
 * No timers, no CSS transitions — nothing here depends on the wall clock.
 */
(() => {
  const clamp = (v, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, v));
  const lerp = (a, b, p) => a + (b - a) * p;
  const q = (v) => Math.round(v * 200) / 200; // quantise so "almost equal" frames compare equal

  const EASE = {
    linear: (p) => p,
    out: (p) => 1 - Math.pow(1 - p, 3),
    snappy: (p) => (p >= 1 ? 1 : 1 - Math.pow(2, -10 * p)),
    smooth: (p) => -(Math.cos(Math.PI * p) - 1) / 2,
    bouncy: (p) => {
      const c1 = 1.70158;
      const c3 = c1 + 1;
      return 1 + c3 * Math.pow(p - 1, 3) + c1 * Math.pow(p - 1, 2);
    },
  };

  /** Progress 0→1 of an animation that starts at `start` and lasts `dur`. */
  const prog = (t, start, dur) => clamp((t - start) / dur);

  function el(tag, cls, parent, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }

  function seeded(seed) {
    let s = 0;
    for (const ch of String(seed)) s = (s * 31 + ch.charCodeAt(0)) >>> 0;
    return () => {
      s = (s + 0x6d2b79f5) >>> 0;
      let r = Math.imul(s ^ (s >>> 15), 1 | s);
      r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
      return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
    };
  }

  // ── Backgrounds (stand-in art when no image AI is connected, and motion scenes) ──

  const ART = {
    bokeh(root, pal, rnd) {
      root.style.background = `radial-gradient(120% 90% at 50% 20%, ${pal.surface}, ${pal.bg} 70%)`;
      for (let i = 0; i < 16; i++) {
        const d = el("div", "", root);
        const size = 8 + rnd() * 30;
        d.style.cssText = `position:absolute;left:${rnd() * 100}%;top:${rnd() * 100}%;width:${size}vmin;height:${size}vmin;border-radius:50%;transform:translate(-50%,-50%);background:${i % 3 ? pal.accent : pal.accent2};opacity:${0.05 + rnd() * 0.16};filter:blur(${1 + rnd() * 3}vmin)`;
      }
      const v = el("div", "", root);
      v.style.cssText = "position:absolute;inset:0;background:radial-gradient(ellipse at center, transparent 45%, rgba(0,0,0,.55))";
    },
    sunburst(root, pal, rnd) {
      const a = rnd() * 30;
      root.style.background = `repeating-conic-gradient(from ${a}deg at 50% ${45 + rnd() * 20}%, ${pal.bg} 0 9deg, ${pal.surface} 9deg 18deg)`;
      const glow = el("div", "", root);
      glow.style.cssText = `position:absolute;inset:0;background:radial-gradient(circle at 50% 50%, ${pal.accent}55, transparent 55%)`;
    },
    blobs(root, pal, rnd) {
      root.style.background = pal.bg;
      const colors = [pal.accent, pal.accent2, pal.surface];
      for (let i = 0; i < 7; i++) {
        const d = el("div", "", root);
        const w = 30 + rnd() * 50;
        const r = () => 35 + rnd() * 30;
        d.style.cssText = `position:absolute;left:${rnd() * 100}%;top:${rnd() * 100}%;width:${w}vmin;height:${w * (0.7 + rnd() * 0.5)}vmin;transform:translate(-50%,-50%) rotate(${rnd() * 360}deg);background:${colors[i % 3]};opacity:${i % 3 === 2 ? 0.9 : 0.5};border-radius:${r()}% ${r()}% ${r()}% ${r()}% / ${r()}% ${r()}% ${r()}% ${r()}%`;
      }
    },
    grid(root, pal, rnd) {
      root.style.background = `linear-gradient(${160 + rnd() * 40}deg, ${pal.surface}, ${pal.bg} 65%)`;
      const g = el("div", "", root);
      g.style.cssText = `position:absolute;inset:-10%;background-image:linear-gradient(${pal.accent}26 1px, transparent 1px),linear-gradient(90deg, ${pal.accent}26 1px, transparent 1px);background-size:7vmin 7vmin;transform:perspective(90vmin) rotateX(${28 + rnd() * 16}deg) translateY(${rnd() * 10}%);transform-origin:50% 100%`;
      for (let i = 0; i < 5; i++) {
        const b = el("div", "", root);
        const w = 6 + rnd() * 22;
        b.style.cssText = `position:absolute;left:${10 + rnd() * 80}%;top:${8 + rnd() * 50}%;width:${w}vmin;height:${w * 0.18}vmin;background:${i % 2 ? pal.accent : pal.accent2};opacity:${0.25 + rnd() * 0.4};border-radius:.4vmin`;
      }
      const glow = el("div", "", root);
      glow.style.cssText = `position:absolute;inset:0;background:radial-gradient(circle at ${30 + rnd() * 40}% 30%, ${pal.accent2}40, transparent 50%)`;
    },
    paper(root, pal, rnd) {
      root.style.background = pal.bg;
      for (let i = 0; i < 6; i++) {
        const d = el("div", "", root);
        const w = 40 + rnd() * 60;
        d.style.cssText = `position:absolute;left:${rnd() * 100}%;top:${rnd() * 100}%;width:${w}vmin;height:${w * 0.7}vmin;transform:translate(-50%,-50%) rotate(${rnd() * 180}deg);background:radial-gradient(ellipse at 40% 40%, ${i % 2 ? pal.accent : pal.accent2}, transparent 68%);opacity:${0.12 + rnd() * 0.16};filter:blur(2.5vmin)`;
      }
      const fibre = el("div", "", root);
      fibre.style.cssText = "position:absolute;inset:0;opacity:.5;background-image:repeating-linear-gradient(0deg, rgba(0,0,0,.018) 0 1px, transparent 1px 3px),repeating-linear-gradient(90deg, rgba(0,0,0,.014) 0 1px, transparent 1px 4px)";
    },
    halftone(root, pal, rnd) {
      root.style.background = `linear-gradient(${120 + rnd() * 60}deg, ${pal.bg}, ${pal.accent2}55)`;
      const dots = el("div", "", root);
      dots.style.cssText = `position:absolute;inset:0;background-image:radial-gradient(${pal.accent} 28%, transparent 30%);background-size:3.2vmin 3.2vmin;-webkit-mask-image:linear-gradient(${rnd() * 360}deg, #000, transparent 75%);opacity:.55`;
      const burst = el("div", "", root);
      burst.style.cssText = `position:absolute;left:50%;top:${35 + rnd() * 25}%;width:150vmax;height:150vmax;transform:translate(-50%,-50%);background:repeating-conic-gradient(${pal.text}10 0 4deg, transparent 4deg 12deg);-webkit-mask-image:radial-gradient(circle, transparent 22%, #000 60%)`;
    },
  };

  function buildBackground(root, bg, pal) {
    const layer = el("div", "bg", root);
    layer.style.cssText = "position:absolute;inset:0;overflow:hidden";
    if (bg.image) {
      layer.style.background = `#000 url("${bg.image}") center/cover no-repeat`;
      if (bg.dim) {
        const d = el("div", "", layer);
        d.style.cssText = `position:absolute;inset:0;background:${bg.dim}`;
      }
      return;
    }
    (ART[bg.art] || ART.bokeh)(layer, pal, seeded(bg.seed || "seed"));
  }

  // ── Layers ───────────────────────────────────────────────────────────────

  /** Shared enter/exit envelope. Returns { p: enter 0→1 eased, o: opacity } */
  function envelope(t, layer, ease, enter = 0.38, exit = 0.22) {
    const pin = prog(t, layer.start, enter);
    const pout = layer.end == null ? 0 : prog(t, layer.end - exit, exit);
    return { p: ease(pin), raw: pin, o: clamp(pin * 3) * (1 - pout), out: pout, live: t >= layer.start && (layer.end == null || t < layer.end) };
  }

  const LAYERS = {
    captions(layer, ctx) {
      const { W, H, u, pal, fonts, style } = ctx;
      const clean = layer.style === "clean";
      const max = layer.maxWords || (clean ? 7 : 3);
      const boxed = Boolean(style.captions?.box);
      const stroked = style.captions?.stroke !== false && !boxed;
      const size = (clean ? 46 : 92) * u * (layer.scale || 1);

      // Group words into readable chunks; break early on punctuation.
      const chunks = [];
      let cur = [];
      for (const w of layer.words) {
        cur.push(w);
        if (cur.length >= max || /[.!?…,]$/.test(w.w)) {
          chunks.push(cur);
          cur = [];
        }
      }
      if (cur.length) chunks.push(cur);

      const wrap = el("div", "cap", ctx.root);
      wrap.style.cssText = `position:absolute;left:${ctx.safe.left * W}px;right:${ctx.safe.right * W}px;top:${layer.y * H}px;height:0;display:flex;justify-content:center;align-items:center`;
      const nodes = chunks.map((chunk) => {
        const line = el("div", "", wrap);
        // "clean" (long-form subtitle) always sits on a dark plate so it reads over any footage.
        const plate = clean ? "rgba(0,0,0,.68)" : boxed ? pal.accent : "";
        line.style.cssText = `position:absolute;max-width:100%;text-align:center;font-family:"${clean ? fonts.body : fonts.display}","Pretendard",sans-serif;font-weight:${clean ? 700 : 800};font-size:${size}px;line-height:1.22;letter-spacing:-0.01em;color:${clean ? "#fff" : boxed ? pal.bg : "#fff"};opacity:0;will-change:transform;text-wrap:balance;word-break:keep-all` +
          (plate ? `;background:${plate};padding:${(clean ? 0.24 : 0.18) * size}px ${(clean ? 0.6 : 0.42) * size}px;border-radius:${style.motion?.shape === "round" ? 0.5 * size : (clean ? 0.22 : 0.12) * size}px` : "");
        const spans = chunk.map((w, i) => {
          const s = el("span", "", line, w.w);
          s.style.cssText = `display:inline-block;margin:0 ${0.12 * size}px;will-change:transform` +
            (stroked && !clean ? `;-webkit-text-stroke:${0.13 * size}px ${pal.stroke || "#000"};paint-order:stroke fill;text-shadow:0 ${0.05 * size}px ${0.1 * size}px rgba(0,0,0,.45)` : "");
          return s;
        });
        return { line, spans, chunk, start: chunk[0].start, end: chunk[chunk.length - 1].end };
      });
      // Hold each chunk until the next one starts (up to a short gap) so captions don't flicker.
      nodes.forEach((n, i) => {
        const next = nodes[i + 1];
        n.until = next ? Math.min(next.start, n.end + 0.35) : n.end + 0.25;
      });

      const hi = boxed ? pal.text : pal.accent;
      return (t) => {
        let token = "";
        for (let i = 0; i < nodes.length; i++) {
          const n = nodes[i];
          const on = t >= n.start && t < n.until;
          if (!on) {
            if (n.line.style.opacity !== "0") n.line.style.opacity = "0";
            continue;
          }
          const pin = prog(t, n.start, clean ? 0.12 : 0.1);
          n.line.style.opacity = String(q(clean ? pin : 1));
          n.line.style.transform = clean ? "" : `scale(${q(lerp(0.86, 1, EASE.out(pin)))})`;
          token += `c${i}:${q(pin)}`;
          if (!clean) {
            for (let k = 0; k < n.spans.length; k++) {
              const w = n.chunk[k];
              const said = t >= w.start;
              const pop = prog(t, w.start, 0.16);
              const active = said && t < (n.chunk[k + 1]?.start ?? n.until);
              const scale = said ? 1 + 0.14 * Math.sin(Math.PI * pop) * (pop < 1 ? 1 : 0) : 0.96;
              n.spans[k].style.transform = `scale(${q(scale)})`;
              n.spans[k].style.opacity = said ? "1" : "0.55";
              n.spans[k].style.color = w.em && said ? hi : active && !boxed ? "#fff" : "";
              token += `w${k}:${q(scale)}${said ? 1 : 0}`;
            }
          }
        }
        return token;
      };
    },

    title(layer, ctx) {
      const { u, pal, fonts, style, ease } = ctx;
      const box = ctx.zone(layer);
      const kind = style.motion?.title || "mask";
      const size = fitSize(layer.text, 132, 78) * u * ctx.gscale;
      const wrap = el("div", "", box);
      wrap.style.cssText = `text-align:center;max-width:100%`;
      const lines = splitLines(layer.text, size > 110 * u ? 9 : 12);
      const rows = lines.map((text) => {
        const mask = el("div", "", wrap);
        mask.style.cssText = `overflow:${kind === "mask" ? "hidden" : "visible"};padding:${0.06 * size}px ${0.1 * size}px`;
        const row = el("div", "", mask, kind === "type" ? "" : text);
        row.style.cssText = `font-family:"${fonts.display}","Pretendard",sans-serif;font-weight:800;font-size:${size}px;line-height:1.12;letter-spacing:-0.02em;color:${pal.text};white-space:nowrap;will-change:transform;${textOutline(ctx, size)}`;
        return { row, text };
      });
      const bar = el("div", "", wrap);
      bar.style.cssText = `height:${0.11 * size}px;width:${Math.min(60, 12 + layer.text.length * 3)}%;margin:${0.12 * size}px auto 0;background:${pal.accent};border-radius:${style.motion?.shape === "round" ? 99 : 0}px;transform-origin:50% 50%`;
      let sub;
      if (layer.sub) {
        sub = el("div", "", wrap, layer.sub);
        sub.style.cssText = `margin-top:${0.26 * size}px;font-family:"${fonts.body}","Pretendard",sans-serif;font-weight:700;font-size:${0.36 * size}px;color:${pal.text};opacity:0;${textOutline(ctx, 0.36 * size)}`;
      }
      const total = lines.join("").length;
      return (t) => {
        const env = envelope(t, layer, ease, 0.45);
        box.style.opacity = String(q(env.o));
        let token = `T${q(env.o)}`;
        rows.forEach((r, i) => {
          const p = ease(prog(t, layer.start + i * 0.09, 0.42));
          if (kind === "slam") r.row.style.transform = `scale(${q(lerp(1.7, 1, p))}) rotate(${q(lerp(-4, 0, p))}deg)`;
          else if (kind === "mask") r.row.style.transform = `translateY(${q(lerp(110, 0, p))}%)`;
          else if (kind === "type") {
            const before = lines.slice(0, i).join("").length;
            const shown = Math.floor(clamp((t - layer.start) / Math.max(0.4, total * 0.045)) * total) - before;
            const n = clamp(shown, 0, r.text.length);
            if (r.row.dataset.n !== String(n)) {
              r.row.textContent = r.text.slice(0, n) || "​";
              r.row.dataset.n = String(n);
            }
            token += `n${n}`;
          } else r.row.style.transform = `translateY(${q(lerp(24 * u, 0, p))}px)`;
          token += `r${q(p)}`;
        });
        const pb = ease(prog(t, layer.start + 0.25, 0.4));
        bar.style.transform = `scaleX(${q(pb)})`;
        if (sub) sub.style.opacity = String(q(prog(t, layer.start + 0.4, 0.3)));
        return token + `b${q(pb)}` + (sub ? `s${q(prog(t, layer.start + 0.4, 0.3))}` : "");
      };
    },

    stat(layer, ctx) {
      const { u, pal, fonts, ease } = ctx;
      const box = ctx.zone(layer);
      const m = /^([^\d-]*)(-?[\d,]*\.?\d+)(.*)$/.exec(String(layer.value || "").trim());
      const size = 230 * u * ctx.gscale * (String(layer.value || "").length > 6 ? 0.72 : 1);
      const wrap = el("div", "", box);
      wrap.style.cssText = "text-align:center";
      const numEl = el("div", "", wrap, layer.value);
      numEl.style.cssText = `font-family:"${fonts.display}","Pretendard",sans-serif;font-weight:800;font-size:${size}px;line-height:1;color:${pal.accent};letter-spacing:-0.03em;font-variant-numeric:tabular-nums;will-change:transform;${textOutline(ctx, size * 0.6)}`;
      const label = el("div", "", wrap, layer.text);
      label.style.cssText = `margin-top:${0.12 * size}px;font-family:"${fonts.body}","Pretendard",sans-serif;font-weight:800;font-size:${0.25 * size}px;color:${pal.text};text-wrap:balance;word-break:keep-all;${textOutline(ctx, 0.25 * size)}`;
      if (layer.sub) {
        const sub = el("div", "", wrap, layer.sub);
        sub.style.cssText = `margin-top:${0.05 * size}px;font-family:"${fonts.body}","Pretendard",sans-serif;font-weight:600;font-size:${0.16 * size}px;color:${pal.text};opacity:.8;${textOutline(ctx, 0.16 * size)}`;
      }
      const target = m ? Number(m[2].replace(/,/g, "")) : null;
      const decimals = m && m[2].includes(".") ? m[2].split(".")[1].length : 0;
      const grouped = m && m[2].includes(",");
      return (t) => {
        const env = envelope(t, layer, ease);
        box.style.opacity = String(q(env.o));
        const pc = EASE.out(prog(t, layer.start + 0.1, 0.9));
        let shown = layer.value;
        if (target != null) {
          const v = target * pc;
          const fixed = v.toFixed(decimals);
          shown = m[1] + (grouped ? Number(fixed).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) : fixed) + m[3];
        }
        if (numEl.textContent !== shown) numEl.textContent = shown;
        numEl.style.transform = `scale(${q(lerp(0.6, 1, env.p))})`;
        label.style.opacity = String(q(prog(t, layer.start + 0.35, 0.3)));
        return `S${q(env.o)}${shown}${q(env.p)}${q(prog(t, layer.start + 0.35, 0.3))}`;
      };
    },

    list(layer, ctx) {
      const { u, pal, fonts, style, ease } = ctx;
      const box = ctx.zone(layer);
      const size = 68 * u * ctx.gscale * (layer.items.length > 4 ? 0.8 : 1);
      const wrap = el("div", "", box);
      wrap.style.cssText = `text-align:left;display:inline-block;max-width:100%`;
      const head = el("div", "", wrap, layer.text);
      head.style.cssText = `font-family:"${fonts.display}","Pretendard",sans-serif;font-weight:800;font-size:${1.25 * size}px;color:${pal.text};margin-bottom:${0.5 * size}px;${textOutline(ctx, 1.25 * size)}`;
      const round = style.motion?.shape === "round";
      const rows = layer.items.map((text, i) => {
        const row = el("div", "", wrap);
        row.style.cssText = `display:flex;align-items:center;gap:${0.4 * size}px;margin-top:${0.32 * size}px;will-change:transform;opacity:0`;
        const chip = el("div", "", row, String(i + 1));
        chip.style.cssText = `flex:none;width:${1.25 * size}px;height:${1.25 * size}px;display:grid;place-items:center;background:${pal.accent};color:${pal.bg};font-family:"${fonts.display}","Pretendard",sans-serif;font-weight:800;font-size:${0.75 * size}px;border-radius:${round ? "50%" : 0.16 * size + "px"}`;
        const label = el("div", "", row, text);
        label.style.cssText = `font-family:"${fonts.body}","Pretendard",sans-serif;font-weight:800;font-size:${size}px;line-height:1.2;color:${pal.text};word-break:keep-all;${textOutline(ctx, size)}`;
        return row;
      });
      const span = Math.max(0.6, (layer.end ?? layer.start + 3) - layer.start - 0.8);
      const step = Math.min(0.7, span / Math.max(1, rows.length));
      return (t) => {
        const env = envelope(t, layer, ease);
        box.style.opacity = String(q(env.o));
        let token = `L${q(env.o)}`;
        rows.forEach((row, i) => {
          const p = ease(prog(t, layer.start + 0.3 + i * step, 0.34));
          row.style.opacity = String(q(clamp(p * 2)));
          row.style.transform = `translateX(${q(lerp(-40 * u, 0, p))}px)`;
          token += q(p);
        });
        return token;
      };
    },

    lower_third(layer, ctx) {
      const { W, H, u, pal, fonts, style, ease } = ctx;
      const size = 44 * u;
      const wrap = el("div", "", ctx.root);
      const y = Math.min(ctx.captionY - 0.13, 0.74);
      wrap.style.cssText = `position:absolute;left:${ctx.safe.left * W}px;top:${y * H}px;display:flex;align-items:stretch;will-change:transform`;
      const bar = el("div", "", wrap);
      bar.style.cssText = `width:${0.22 * size}px;background:${pal.accent}`;
      const body = el("div", "", wrap);
      body.style.cssText = `background:${pal.bg}f0;padding:${0.4 * size}px ${0.7 * size}px;border-radius:0 ${style.motion?.shape === "round" ? 0.5 * size : 0}px ${style.motion?.shape === "round" ? 0.5 * size : 0}px 0`;
      const name = el("div", "", body, layer.text);
      name.style.cssText = `font-family:"${fonts.display}","Pretendard",sans-serif;font-weight:800;font-size:${size}px;color:${pal.text === "#ffffff" || pal.text === "#fff" ? "#fff" : pal.text}`;
      if (layer.sub) {
        const sub = el("div", "", body, layer.sub);
        sub.style.cssText = `font-family:"${fonts.body}","Pretendard",sans-serif;font-weight:600;font-size:${0.6 * size}px;color:${pal.text};opacity:.8;margin-top:${0.1 * size}px`;
      }
      return (t) => {
        const env = envelope(t, layer, ease);
        wrap.style.opacity = String(q(env.o));
        wrap.style.transform = `translateX(${q(lerp(-60 * u, 0, env.p))}px)`;
        return `3${q(env.o)}${q(env.p)}`;
      };
    },

    quote(layer, ctx) {
      const { u, pal, fonts, ease } = ctx;
      const box = ctx.zone(layer);
      const size = fitSize(layer.text, 84, 56) * u * ctx.gscale;
      const wrap = el("div", "", box);
      wrap.style.cssText = "text-align:center;max-width:100%";
      const mark = el("div", "", wrap, "“");
      mark.style.cssText = `font-family:Georgia,serif;font-size:${2.6 * size}px;line-height:.7;color:${pal.accent};height:${1.1 * size}px`;
      const lines = splitLines(layer.text, 13).map((text) => {
        const row = el("div", "", wrap, text);
        row.style.cssText = `font-family:"${fonts.display}","Pretendard",sans-serif;font-weight:700;font-size:${size}px;line-height:1.3;color:${pal.text};will-change:transform;${textOutline(ctx, size)}`;
        return row;
      });
      let sub;
      if (layer.sub) {
        sub = el("div", "", wrap, "— " + layer.sub);
        sub.style.cssText = `margin-top:${0.5 * size}px;font-family:"${fonts.body}","Pretendard",sans-serif;font-weight:600;font-size:${0.5 * size}px;color:${pal.text};opacity:.85;${textOutline(ctx, 0.5 * size)}`;
      }
      return (t) => {
        const env = envelope(t, layer, ease);
        box.style.opacity = String(q(env.o));
        let token = `Q${q(env.o)}`;
        lines.forEach((row, i) => {
          const p = EASE.smooth(prog(t, layer.start + 0.15 + i * 0.22, 0.5));
          row.style.opacity = String(q(p));
          row.style.transform = `translateY(${q(lerp(18 * u, 0, p))}px)`;
          token += q(p);
        });
        return token;
      };
    },

    chapter(layer, ctx) {
      const { W, H, u, pal, fonts, style, ease } = ctx;
      const size = 34 * u;
      const chip = el("div", "", ctx.root);
      chip.style.cssText = `position:absolute;left:${ctx.safe.left * W}px;top:${(ctx.safe.top + 0.02) * H}px;display:flex;align-items:center;gap:${0.5 * size}px;background:${pal.bg}e6;padding:${0.35 * size}px ${0.7 * size}px;border-radius:${style.motion?.shape === "round" ? 99 : 0.12 * size}px;will-change:transform`;
      const dot = el("div", "", chip);
      dot.style.cssText = `width:${0.4 * size}px;height:${0.4 * size}px;background:${pal.accent};border-radius:50%`;
      const text = el("div", "", chip, layer.sub ? `${layer.sub} · ${layer.text}` : layer.text);
      text.style.cssText = `font-family:"${fonts.body}","Pretendard",sans-serif;font-weight:800;font-size:${size}px;color:${pal.text}`;
      return (t) => {
        const env = envelope(t, layer, ease);
        chip.style.opacity = String(q(env.o));
        chip.style.transform = `translateY(${q(lerp(-30 * u, 0, env.p))}px)`;
        return `H${q(env.o)}${q(env.p)}`;
      };
    },

    badge(layer, ctx) {
      const { u, pal, fonts, style } = ctx;
      const box = ctx.zone(layer, "top");
      const size = 76 * u * ctx.gscale;
      const b = el("div", "", box, layer.text);
      b.style.cssText = `display:inline-block;background:${pal.accent};color:${pal.bg};font-family:"${fonts.display}","Pretendard",sans-serif;font-weight:800;font-size:${size}px;padding:${0.22 * size}px ${0.6 * size}px;border-radius:${style.motion?.shape === "round" ? 99 : 0.14 * size}px;box-shadow:0 ${0.14 * size}px 0 rgba(0,0,0,.28);will-change:transform`;
      return (t) => {
        const env = envelope(t, layer, EASE.bouncy, 0.4);
        box.style.opacity = String(q(env.o));
        b.style.transform = `rotate(-4deg) scale(${q(lerp(0.2, 1, env.p))})`;
        return `B${q(env.o)}${q(env.p)}`;
      };
    },

    cta(layer, ctx) {
      const { u, pal, fonts, ease } = ctx;
      const box = ctx.zone(layer);
      const size = 84 * u * ctx.gscale;
      const wrap = el("div", "", box);
      wrap.style.cssText = "text-align:center";
      const text = el("div", "", wrap, layer.text);
      text.style.cssText = `font-family:"${fonts.display}","Pretendard",sans-serif;font-weight:800;font-size:${size}px;line-height:1.2;color:${pal.text};text-wrap:balance;word-break:keep-all;${textOutline(ctx, size)}`;
      const pill = el("div", "", wrap, layer.sub || "구독");
      pill.style.cssText = `display:inline-block;margin-top:${0.5 * size}px;background:${pal.accent};color:${pal.bg};font-family:"${fonts.body}","Pretendard",sans-serif;font-weight:800;font-size:${0.62 * size}px;padding:${0.3 * size}px ${0.9 * size}px;border-radius:99px;will-change:transform;box-shadow:0 ${0.1 * size}px ${0.4 * size}px rgba(0,0,0,.3)`;
      return (t) => {
        const env = envelope(t, layer, ease);
        box.style.opacity = String(q(env.o));
        text.style.transform = `translateY(${q(lerp(30 * u, 0, env.p))}px)`;
        const pp = EASE.bouncy(prog(t, layer.start + 0.3, 0.45));
        const pulse = t > layer.start + 0.8 ? 1 + 0.05 * Math.sin((t - layer.start - 0.8) * 6) : 1;
        pill.style.transform = `scale(${q(pp * pulse)})`;
        return `A${q(env.o)}${q(env.p)}${q(pp * pulse)}`;
      };
    },

    /** Thin progress bar across the whole video — a small retention device. */
    progress(layer, ctx) {
      const { W, H, pal, u } = ctx;
      const track = el("div", "", ctx.root);
      const top = ctx.landscape ? H - 8 * u : 0;
      track.style.cssText = `position:absolute;left:0;top:${top}px;width:${W}px;height:${8 * u}px;background:rgba(255,255,255,.16)`;
      const fill = el("div", "", track);
      fill.style.cssText = `height:100%;width:100%;background:${pal.accent};transform-origin:0 50%`;
      return (t) => {
        const v = lerp(layer.from, layer.to, clamp(t / ctx.duration));
        const stepPx = 4 * u; // advance in small steps so unchanged frames can be reused
        const s = (Math.round((v * W) / stepPx) * stepPx) / W;
        fill.style.transform = `scaleX(${s})`;
        return `P${s}`;
      };
    },

    /** Scene-entry transition drawn as an overlay, so every cut can stay a hard cut in the edit. */
    transition(layer, ctx) {
      if (layer.kind === "cut") return () => "";
      const cover = el("div", "", ctx.root);
      const color = layer.kind === "flash" ? "#fff" : "#000";
      cover.style.cssText = `position:absolute;inset:0;background:${color};opacity:0;pointer-events:none`;
      const dur = layer.kind === "flash" ? 0.16 : 0.32;
      return (t) => {
        const o = q((1 - prog(t, 0, dur)) * (layer.kind === "flash" ? 0.85 : 1));
        cover.style.opacity = String(o);
        return `X${o}`;
      };
    },

    /** Thumbnail text block (still image). */
    thumb(layer, ctx) {
      const { W, H, u, pal, fonts } = ctx;
      const landscape = W > H;
      const size = fitSize(layer.text, landscape ? 190 : 170, landscape ? 120 : 110) * u;
      const wrap = el("div", "", ctx.root);
      const align = layer.layout === "center" || !landscape ? "center" : layer.layout;
      wrap.style.cssText = `position:absolute;${landscape ? `top:0;bottom:0;${align === "right" ? "right" : "left"}:${0.05 * W}px;width:${align === "center" ? 0.9 * W : 0.56 * W}px;display:flex;flex-direction:column;justify-content:center` : `left:${0.07 * W}px;right:${0.07 * W}px;top:${0.16 * H}px`};text-align:${align === "center" ? "center" : align}`;
      splitLines(layer.text, landscape ? 7 : 8).forEach((text, i) => {
        const row = el("div", "", wrap, text);
        row.style.cssText = `font-family:"${fonts.display}","Pretendard",sans-serif;font-weight:800;font-size:${size}px;line-height:1.08;letter-spacing:-0.03em;color:${i % 2 ? pal.accent : "#fff"};-webkit-text-stroke:${0.11 * size}px #000;paint-order:stroke fill;text-shadow:0 ${0.06 * size}px 0 #000, 0 ${0.1 * size}px ${0.3 * size}px rgba(0,0,0,.6);white-space:nowrap`;
      });
      if (layer.sub) {
        const sub = el("div", "", wrap, layer.sub);
        sub.style.cssText = `display:inline-block;align-self:${align === "center" ? "center" : align === "right" ? "flex-end" : "flex-start"};margin-top:${0.2 * size}px;background:${pal.accent};color:#000;font-family:"${fonts.body}","Pretendard",sans-serif;font-weight:800;font-size:${0.34 * size}px;padding:${0.1 * size}px ${0.3 * size}px;border-radius:${0.08 * size}px`;
      }
      return () => "thumb";
    },
  };

  // ── Helpers ──────────────────────────────────────────────────────────────

  function textOutline(ctx, size) {
    // Light text needs a dark edge to stay readable over any footage.
    if (ctx.opaque && !ctx.forceOutline) return "";
    const dark = ctx.pal.stroke || "#000";
    return `-webkit-text-stroke:${Math.max(2, 0.09 * size)}px ${dark};paint-order:stroke fill;text-shadow:0 ${0.04 * size}px ${0.14 * size}px rgba(0,0,0,.4);`;
  }

  /** Shrink long headlines so they stay on a few lines. */
  function fitSize(text, max, min) {
    const n = [...String(text)].length;
    return n <= 8 ? max : n >= 26 ? min : lerp(max, min, (n - 8) / 18);
  }

  /** Break Korean text at spaces into lines of roughly `target` characters. */
  function splitLines(text, target) {
    const words = String(text).trim().split(/\s+/);
    const lines = [];
    let cur = "";
    for (const w of words) {
      if (cur && (cur + " " + w).length > target) {
        lines.push(cur);
        cur = w;
      } else cur = cur ? cur + " " + w : w;
    }
    if (cur) lines.push(cur);
    return lines;
  }

  // ── Entry points ─────────────────────────────────────────────────────────

  let updaters = [];

  window.__setup = (comp) => {
    const { width: W, height: H } = comp;
    const style = comp.style || {};
    const pal = { bg: "#101216", surface: "#1b1f27", text: "#ffffff", accent: "#ffb23e", accent2: "#63a8ff", stroke: "#000000", ...(style.palette || {}) };
    const fonts = { display: "Pretendard", body: "Pretendard", ...(style.fonts || {}) };
    const safe = { top: 0.08, bottom: 0.12, left: 0.06, right: 0.06, ...(comp.safe || {}) };
    const landscape = W > H;
    const root = document.getElementById("root");
    root.innerHTML = "";
    root.style.cssText = `position:relative;width:${W}px;height:${H}px;overflow:hidden`;

    const opaque = Boolean(comp.background);
    if (opaque) buildBackground(root, comp.background, pal);

    const captionLayer = comp.layers.find((l) => l.type === "captions");
    const captionY = captionLayer?.y ?? (landscape ? 0.86 : 0.62);
    const ctx = {
      W, H, pal, fonts, style, safe, root, landscape, opaque,
      forceOutline: Boolean(comp.background?.image),
      u: Math.min(W, H) / 1080,
      duration: comp.duration,
      captionY,
      gscale: comp.prominent ? 1.12 : 1,
      ease: EASE[style.motion?.ease] || EASE.out,
      /** A flex box covering the area graphics may occupy (inside the safe area, above the captions). */
      zone(layer, valign) {
        const top = safe.top * H + 0.02 * H;
        const bottom = landscape ? (1 - (captionLayer ? 1 - captionY + 0.06 : safe.bottom)) * H : (captionLayer ? captionY - 0.07 : 1 - safe.bottom) * H;
        const box = el("div", "", root);
        box.style.cssText = `position:absolute;left:${safe.left * W}px;width:${(1 - safe.left - safe.right) * W}px;top:${top}px;height:${bottom - top}px;display:flex;align-items:${valign === "top" ? "flex-start" : "center"};justify-content:center;opacity:0`;
        return box;
      },
    };

    updaters = comp.layers.map((layer) => (LAYERS[layer.type] ? LAYERS[layer.type](layer, ctx) : () => ""));
    return true;
  };

  window.__frame = (t) => updaters.map((fn) => fn(t)).join("|");
})();
