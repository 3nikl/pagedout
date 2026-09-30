"""
Soundtrack for the PagedOut launch video.

Synthesised with numpy, not composed. Honest about what this is: a bed that
sits under the content, not a score. The brief is restraint — the video's
job is to be read, and anything busy in the audio fights that.

Design:
  key        D minor, which keeps the pad dark without being mournful
  pad        stacked sines with slow detune drift, low-passed
  pulse      a soft filtered thump on the beat, felt rather than heard
  ticks      short transients timed to the trace lines, in the same space
             as the pad (same reverb tail) so they blend instead of sitting
             on top
  arc        quiet under the hook, opens at the title, peaks on the counter,
             drops away for the finding so the text has silence around it,
             returns for the outro
"""

from __future__ import annotations

import numpy as np
import wave
from pathlib import Path

SR = 44100
DUR = 21.0
N = int(SR * DUR)
t = np.arange(N) / SR

HERE = Path(__file__).resolve().parent


def adsr(start: float, length: float, a=0.01, d=0.1, s=0.7, r=0.3) -> np.ndarray:
    """Envelope over the whole timeline, zero outside the note."""
    env = np.zeros(N)
    i0, i1 = int(start * SR), int((start + length) * SR)
    i1 = min(i1, N)
    if i1 <= i0:
        return env
    seg = i1 - i0
    ai, di = int(a * SR), int(d * SR)
    ri = int(r * SR)
    body = np.ones(seg) * s
    if ai:
        body[:min(ai, seg)] = np.linspace(0, 1, min(ai, seg))
    if di and ai + di < seg:
        body[ai:ai + di] = np.linspace(1, s, di)
    if ri:
        k = min(ri, seg)
        body[-k:] *= np.linspace(1, 0, k)
    env[i0:i1] = body
    return env


def tone(freq: float, start: float, length: float, amp: float,
         detune: float = 0.0, harm: float = 0.35, **env_kw) -> np.ndarray:
    """A sine with a quiet octave and a slow detune drift."""
    drift = 1.0 + detune * np.sin(2 * np.pi * 0.07 * t + freq)
    base = np.sin(2 * np.pi * freq * drift * t)
    octave = harm * np.sin(2 * np.pi * freq * 2 * drift * t)
    return amp * (base + octave) * adsr(start, length, **env_kw)


def thump(at: float, amp: float = 0.30) -> np.ndarray:
    """Pitch-dropping sine — a kick you feel more than hear."""
    out = np.zeros(N)
    i0 = int(at * SR)
    length = int(0.30 * SR)
    if i0 + length > N:
        length = N - i0
    if length <= 0:
        return out
    lt = np.arange(length) / SR
    f = 110 * np.exp(-14 * lt) + 42
    out[i0:i0 + length] = amp * np.sin(2 * np.pi * f * lt) * np.exp(-9 * lt)
    return out


def tick(at: float, amp: float = 0.10, freq: float = 2100) -> np.ndarray:
    """Short transient for a trace line landing. Deliberately tiny."""
    out = np.zeros(N)
    i0 = int(at * SR)
    length = int(0.05 * SR)
    if i0 + length > N:
        length = N - i0
    if length <= 0:
        return out
    lt = np.arange(length) / SR
    out[i0:i0 + length] = amp * np.sin(2 * np.pi * freq * lt) * np.exp(-70 * lt)
    return out


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """One-pole filter. Takes the glassy edge off the stacked sines."""
    a = np.exp(-2 * np.pi * cutoff / SR)
    out = np.zeros_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc = (1 - a) * x[i] + a * acc
        out[i] = acc
    return out


def reverb(x: np.ndarray, decay: float = 0.32, taps=(0.051, 0.089, 0.137, 0.211)) -> np.ndarray:
    """Cheap multi-tap delay. Puts pad and ticks in the same room."""
    out = x.copy()
    for k, d in enumerate(taps):
        shift = int(d * SR)
        if shift >= N:
            continue
        g = decay ** (k + 1)
        out[shift:] += g * x[:-shift]
    return out


# ── notes ────────────────────────────────────────────────────────────────────
# D minor: D3 146.83, F3 174.61, A3 220.00, C4 261.63, D4 293.66

pad = np.zeros(N)
# Hook — a single low root, barely there. The trace should feel quiet.
pad += tone(146.83, 0.0, 4.0, 0.16, detune=0.0016, a=1.2, d=0.6, s=0.62, r=1.2)

# Title — the fifth arrives, the pad opens up.
pad += tone(220.00, 3.4, 3.1, 0.13, detune=0.0020, a=0.7, d=0.4, s=0.66, r=0.9)
pad += tone(174.61, 3.4, 3.1, 0.10, detune=0.0014, a=0.9, d=0.4, s=0.60, r=0.9)

# Stakes — minor third sits under the cascade.
pad += tone(174.61, 6.0, 3.8, 0.13, detune=0.0018, a=0.6, d=0.4, s=0.64, r=0.8)
pad += tone(146.83, 6.0, 3.8, 0.11, detune=0.0012, a=0.6, d=0.4, s=0.60, r=0.8)

# Counter — highest point of the piece.
pad += tone(220.00, 9.5, 4.2, 0.15, detune=0.0022, a=0.5, d=0.4, s=0.70, r=0.9)
pad += tone(293.66, 9.5, 4.2, 0.10, detune=0.0026, a=0.8, d=0.5, s=0.58, r=0.9)
pad += tone(146.83, 9.5, 4.2, 0.12, detune=0.0010, a=0.5, d=0.4, s=0.66, r=0.9)

# Finding — strip back to a bare root. The sentence needs room.
pad += tone(146.83, 13.5, 4.1, 0.13, detune=0.0008, a=1.0, d=0.7, s=0.56, r=1.1)

# Outro — resolve to the full triad.
pad += tone(146.83, 17.4, 3.7, 0.14, detune=0.0012, a=0.5, d=0.4, s=0.66, r=1.6)
pad += tone(174.61, 17.4, 3.7, 0.11, detune=0.0016, a=0.7, d=0.4, s=0.60, r=1.6)
pad += tone(261.63, 17.6, 3.5, 0.09, detune=0.0020, a=0.9, d=0.5, s=0.54, r=1.6)

pad = lowpass(pad, 1500)

# ── percussion ───────────────────────────────────────────────────────────────
perc = np.zeros(N)
# ~100 BPM from the title onward. Absent under the hook on purpose.
for beat_t in np.arange(3.5, 13.4, 0.6):
    perc += thump(float(beat_t), 0.20)
# Emphasis on the scene changes.
for hit in (3.5, 6.0, 9.5, 17.5):
    perc += thump(hit, 0.32)
# No pulse during the finding — silence frames the sentence.
for beat_t in np.arange(17.5, 20.6, 0.6):
    perc += thump(float(beat_t), 0.17)

# ── ticks on the trace lines ────────────────────────────────────────────────
ticks = np.zeros(N)
for line_t, f in ((0.55, 2200), (1.10, 2000), (1.62, 1900), (2.14, 1500)):
    ticks += tick(line_t, 0.085, f)
ticks += tick(2.40, 0.11, 1200)          # the verdict landing
for i in range(3):                        # chain nodes
    ticks += tick(6.10 + i * 0.22, 0.055, 2400)

wet = reverb(pad + ticks * 0.9, decay=0.30)
mix = 0.72 * wet + 0.85 * perc

# Gentle fade at both ends so nothing clicks.
fade = int(0.12 * SR)
mix[:fade] *= np.linspace(0, 1, fade)
mix[-int(1.1 * SR):] *= np.linspace(1, 0, int(1.1 * SR))

# Soft-clip rather than hard-limit; keeps the pad from sounding crushed.
mix = np.tanh(mix * 1.25) * 0.80
mix = mix / max(np.max(np.abs(mix)), 1e-9) * 0.82

stereo = np.stack([mix, np.roll(mix, 90)], axis=1)   # tiny width on the pad
pcm = (stereo * 32767).astype(np.int16)

out = HERE / "score.wav"
with wave.open(str(out), "w") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())

print(f"wrote {out.name}  {DUR}s  peak {np.max(np.abs(mix)):.3f}")
