# docs/ — the contracts' one source: design intent and conventions

> This file (AGENTS.md / CLAUDE.md) must not exceed 100 lines.

- conduct-state.md — where the state-and-advancing design comes from: who writes it down, how
  claiming and the status divide, the two ways in, the stopping criteria, the settings chain and the
  plugins (the maintenance channel is one of them) (read before changing conduct's behaviour or
  writing pf-conduct)
- conduct-dsh-dispatch.md — dispatch and authentication: measured calibration, the self-signed
  cookie, failure modes and troubleshooting (read when a dispatch fails)
- conduct-node-tags.md — node tags and splitting between cycles: the word list, the filter
  criteria, why filtering lands only on the picking step (read before opening a second line or
  touching tags)
- vocabulary.md — the word list's one source: node / cycle / the note each on its own axis,
  derived tags never land on disk, and a Chinese-English table at the end (read before touching
  state words, aligning UI wording, or translating a skill)
- tool-modules.md — the shape of tools/' modules: the one loader that loads by path, who decides
  how many copies of a file one process holds, where shared pieces go (read before touching
  cross-file references or adding a shared piece)
