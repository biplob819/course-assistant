# N1.3 run state

**Now:** green

## History
- 2026-11-15 — spec and criteria written.
- 2026-11-15 — built: YAML source written first, build script with a dependency-free PDF writer and restamped pptx zips (byte-identical rebuilds); private model solutions in git-ignored golden/heldout/solutions/.
- 2026-11-15 — verifier confirmed CRS-01..05 (byte-identical rebuilds across TZ, cwd and hash seed; extraction round-trip exact; no solution n-grams anywhere tracked). Should-fix done: content expanded about 70% (60 slides, 18 pre-read pages, 7 slides with speaker notes) so about 50 distinct golden questions per session are possible; the A/B-test absent topic narrowed to power analysis (no overlap with golden-set sizing). Dev setup needs --all-extras (python-pptx, pypdf), noted in README. Converge passed twice; rows flipped green.
