import fsp from "node:fs/promises";

/**
 * Stand-in background music, synthesised locally (no music AI connected).
 * A soft pad over a four-chord loop, with a light pulse at faster tempos.
 * It exists so the edit, the ducking and the loudness pipeline can be
 * exercised end to end — connect a music model for real tracks.
 */

const RATE = 44100;
const NOTE = (semi) => 220 * Math.pow(2, semi / 12); // A3 = 0

const PROGRESSIONS = {
  minor: [[0, 3, 7], [-4, 0, 3], [-9, -5, 0], [-2, 2, 5]], // Am F C G
  major: [[3, 7, 10], [-2, 2, 5], [0, 3, 7], [-4, 0, 3]], // C G Am F
};

export async function synthMusic({ outFile, duration, bpm = 96, mood = "" }) {
  const total = Math.ceil((duration + 0.5) * RATE);
  const beat = 60 / bpm;
  const bar = beat * 4;
  const bright = /밝|경쾌|귀여|산뜻|playful|light|happy|pop|funk/i.test(mood);
  const chords = bright ? PROGRESSIONS.major : PROGRESSIONS.minor;
  const driving = bpm >= 100;
  const data = new Float32Array(total);

  for (let i = 0; i < total; i++) {
    const t = i / RATE;
    const chord = chords[Math.floor(t / bar) % chords.length];
    const inBar = (t % bar) / bar;
    // Pad: three voices, each with a quiet octave, swelling in at every chord change.
    const swell = Math.min(1, inBar * 6) * (0.75 + 0.25 * Math.cos(inBar * Math.PI * 2));
    let s = 0;
    for (const semi of chord) {
      const f = NOTE(semi);
      s += Math.sin(2 * Math.PI * f * t) * 0.16 + Math.sin(2 * Math.PI * f * 2.003 * t) * 0.05 + Math.sin(2 * Math.PI * f * 0.5 * t) * 0.07;
    }
    s *= swell;
    // Plucked arpeggio on eighth notes.
    const step = Math.floor(t / (beat / 2));
    const tp = t - step * (beat / 2);
    const arp = NOTE(chord[step % 3] + 12);
    s += Math.sin(2 * Math.PI * arp * t) * Math.exp(-tp * 9) * (driving ? 0.13 : 0.07);
    if (driving) {
      const tb = t % beat;
      s += Math.sin(2 * Math.PI * (48 + 70 * Math.exp(-tb * 28)) * tb) * Math.exp(-tb * 11) * 0.42; // soft kick
      const th = (t + beat / 2) % beat;
      s += (Math.random() * 2 - 1) * Math.exp(-th * 60) * 0.05; // off-beat hat
    }
    data[i] = s;
  }

  const pcm = Buffer.alloc(44 + total * 4);
  pcm.write("RIFF", 0);
  pcm.writeUInt32LE(36 + total * 4, 4);
  pcm.write("WAVEfmt ", 8);
  pcm.writeUInt32LE(16, 16);
  pcm.writeUInt16LE(1, 20);
  pcm.writeUInt16LE(2, 22);
  pcm.writeUInt32LE(RATE, 24);
  pcm.writeUInt32LE(RATE * 4, 28);
  pcm.writeUInt16LE(4, 32);
  pcm.writeUInt16LE(16, 34);
  pcm.write("data", 36);
  pcm.writeUInt32LE(total * 4, 40);
  for (let i = 0; i < total; i++) {
    const v = Math.max(-1, Math.min(1, data[i] * 0.7)) * 32767;
    pcm.writeInt16LE(v, 44 + i * 4);
    pcm.writeInt16LE(v, 46 + i * 4);
  }
  await fsp.writeFile(outFile, pcm);
  return outFile;
}
