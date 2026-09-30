# /brag plan — PagedOut

## Angle

Most launch videos for an infrastructure project open with a logo and a
tagline. PagedOut has something better: a **real bug, caught on camera.**

Seed 3 shows an automated repair running twice because an acknowledgement was
lost. That is a genuine failure in a real system, reproduced exactly, and it is
the most visually distinctive thing this project owns. Nobody else's launch
video can show that, because nobody else's project can reproduce a concurrency
bug on demand.

So the video opens on the trace, not the logo.

## Hook (first 2 seconds)

Four monospace lines type in on a dark ground. The last one is the same action
as the first, highlighted red:

```
0.0020s   effect   /admin/pool/drain — applied
0.0020s   fault    ack dropped
5.0020s   fault    verify probe dropped
8.5357s   effect   /admin/pool/drain — applied again   ← red
```

Then two words: **This ran twice.**

Someone who has ever been on call understands the stakes instantly.

## Tone

`polished`, leaning `deadpan`. Restrained, precise, a little cold. The subject
supplies the drama; the treatment should not add any. No swooshes, no
exclamation marks, no stock startup language.

## Visual identity

Inherited from `docs/showcase.html` so the video and the page look like one
thing.

| Role | Value |
|---|---|
| Ground | `#0f1218` near-black with a blue bias |
| Panel | `#161b23` |
| Ink | `#e8ebf0` |
| Muted | `#98a2b3` |
| Accent | `#8ba3f7` periwinkle |
| Critical | `#f0796a` |
| Good | `#4cc38a` |
| Display | IBM Plex Serif 600 |
| Data | IBM Plex Mono 400/500 |

Motion is cuts and reveals, not easing flourishes. Traces type on line by line.
Numbers count up. Transitions dip through the ground colour rather than
crossfading, so two busy layouts never double-expose.

## Format

1920×1080, 30 fps, **21 seconds** (630 frames).

## Storyboard

| # | Time | Scene | Content |
|---|---|---|---|
| 1 | 0.0–3.5s | **Hook** | Trace types in. Last line flashes red. "This ran twice." |
| 2 | 3.5–6.0s | **Title** | PagedOut wordmark + one-line positioning |
| 3 | 6.0–9.5s | **Stakes** | Service chain; ledger goes red; failure cascades upward. "Automated repair takes destructive actions." |
| 4 | 9.5–13.5s | **Groundhog** | Counter runs to 600,000. "214 hours of failure. 6.6 seconds." |
| 5 | 13.5–17.5s | **The finding** | "Client-side idempotency keys cannot guarantee at-most-once execution." |
| 6 | 17.5–21.0s | **Outro** | "2 bugs found. 0 caught by the test suite." + repo URL |

Every claim on screen is a measured number from `docs/benchmarks/`. Nothing is
invented for the video.

## Sound

Synthesised locally: a low sine pad in D minor, a muted pulse on the beat, and
soft transient clicks timed to the trace lines. Sparse on purpose — the
soundtrack sits under the content and never competes with text the viewer is
meant to read.

Honest limitation: this is generated with numpy and mixed with ffmpeg, not
composed. It is a bed, not a score.

## Punchline

> 2 bugs found. 0 caught by the test suite.

That is the whole argument for the project in nine words.
