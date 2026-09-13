# Display face

One file, one request, self-hosted. No font CDN is contacted at runtime.

| | |
|---|---|
| Family | Source Serif 4, variable on the weight axis (400–700), normal |
| File | `source-serif-4-var-latin.woff2` |
| Bytes | 50,824 |
| SHA-256 | `c1df4596be5029233ed2afbb8b2f6ea20784b3fb1aa5d6b5c6519ccd85eb3dfb` |
| Licence | SIL Open Font License 1.1 — `OFL.txt` in this directory |
| Copyright | 2014 The Source Serif 4 Project Authors, https://github.com/adobe-fonts/source-serif |
| Fetched | 2026-09-13, from the Google Fonts `latin` variable build (v14) |
| Source URL | `https://fonts.gstatic.com/s/sourceserif4/v14/vEFF2_tTDB4M7-auWDN0ahZJW3IX2ih5nk3AucvUHf6kDXr4.woff2` |

## Why variable rather than one static weight

A single static 600 was tried first, at 21,532 bytes, and it was wrong. The
generated headlines wrap their key figures in `<b>` — *"…are **65.10%**
full…"* — because those numbers are the finding. At one weight, 600 and 700
measured **identically** (86.1px for `65.10%`) and the emphasis was flat.

Worse, `<b>` defaults to `font-weight: bolder`, which resolves **relative to
its parent**: inside a 600 heading that is 900, a value no file had, so the
browser matched the nearest and the markup silently meant nothing. The CSS now
sets `font-weight: 700` explicitly on emphasis inside headings, and the
variable file actually has it — 400 / 600 / 700 measure 83.7 / 86.1 / 88.9.

Two statics (600 + 700) would have been ~43KB against the variable's 50.8KB,
but that is two files and two preloads, and the variable leaves the whole axis
available. 8KB on a page that already ships about 918KB.

## Why this file rather than a subset built here

`fontTools` is installed and `pyftsubset` is on PATH, but **brotli is not**,
and woff2 compression needs it. Subsetting locally would have produced woff or
ttf — roughly 1.4× and 2× the bytes for the same glyphs. The Google `latin`
build is already subset to the range below and already woff2, so it is the
smaller artefact for identical coverage. The URL is versioned and the SHA-256
above pins exactly what was fetched.

## Coverage

```
U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC,
U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193,
U+2212, U+2215, U+FEFF, U+FFFD
```

Basic Latin and Latin-1, which covers every region name and every figure the
titles can contain, plus U+2000-206F for the em dash, en dash and curly quotes
the generated headlines use, and U+00B7 for the middle dot in the captions.

**The `unicode-range` is declared in the `@font-face`, deliberately.** The
headlines are generated from the data, so their exact characters are not known
in advance. With the range declared, a character outside the subset falls back
to the next family in the stack and renders correctly; without it the browser
would use this file for every character and draw a missing-glyph box.
Subsetting to the glyphs observed on one day would have been smaller and would
have broken the first time the data produced a character nobody had seen.

## No layout shift when it swaps

Measured, not assumed. Two things caused a shift and both are fixed:

* **Leading.** `h1`, `h2`, `.sub-h` and `.fh .nm` were on `line-height: normal`,
  which is computed from **the font's own metrics** — the stage `h2` measured
  92px in Georgia and 127px in Source Serif at the same `font-size`. They now
  set `--lh-tight` explicitly.
* **Width.** On the actual headline strings Georgia sets **8% wider** than
  Source Serif 4 at the same size (1043px against 966px for the `h1`), so it
  wraps earlier. An `SS4 fallback` face maps `local("Georgia")` with
  `size-adjust: 92.8%`, the measured ratio, declared across `font-weight:
  400 700` — declared at a single weight it failed to match anything set at
  400, which was the whole of a residual 15px shift on the `h2`.

Forcing the fallback and re-measuring gives **0px** on all four headings.

`<link rel="preload">` in the `<head>` starts the fetch alongside the HTML
instead of after the stylesheet is parsed, so in practice the face arrives
before first paint and no swap is seen. `crossorigin` is required on font
preloads even same-origin — fonts are fetched in CORS mode, and without it the
browser issues a second request and the preload is wasted.
