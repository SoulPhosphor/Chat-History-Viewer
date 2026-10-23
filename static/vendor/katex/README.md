# KaTeX 0.19.0 (vendored)

Copied unchanged from the `katex@0.19.0` npm package (`dist/`), so maths
renders offline and no code loads from the internet (AUDIT.md #15).
MIT licence: see `LICENSE`.

Kept: `katex.min.js`, `katex.min.css`, `contrib/auto-render.min.js` and the
`.woff2` fonts. The `.woff` / `.ttf` fallbacks the stylesheet also names are
left out: every browser the app supports uses `.woff2`, which it lists first.

To update: `npm pack katex@<version>`, copy the same files over these, and
change the version here and in `CHANGELOG.md`.
