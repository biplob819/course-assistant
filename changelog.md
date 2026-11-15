# Changelog

One line per change: `[TYPE] [NODE] — summary`. Types: FEAT, FIX, DOCS, HARNESS, OWNER.

- [DOCS] [—] — PRD, Tech Spec, build graph and PRD loophole review; owner rulings D-6, D-11, D-19, D-20
- [HARNESS] [W0.1] — EvalKit harness ported: eligibility, node_start, node_converge (12 gates), status, secret scan, new corpus guard and external attest nodes; graph and catalog generated from the Tech Spec
- [FIX] [W0.1] — verifier pass 1: exact external marker and attest-backed green, strict criteria lines, gate 10 self-test names the defect, corpus guard normalises text and withholds private paths (D-22, L-16 to L-18)
- [OWNER] [R3.1] — OWNER D-21: open-source release node added (PyPI course-assistant, init/doctor, docs, community files)
- [FIX] [W0.1] — verifier pass 2: git -z file listing, token-based private-term matching with unescaping, attest rulings bound to their node (L-19)
- [FIX] [W0.1] — verifier pass 3: markup-aware term matching without prose false positives, archive and magic-byte checks, Office XML scanned, attest wording bound (D-23, L-20)
- [FIX] [W0.1] — verifier pass 4: raw and stripped views, accent folding, archive reading, strict 'Attests <node>' marker (L-21)
- [FIX] [W0.1] — verifier pass 5: punctuation-holding terms, PDF streams, bz2/xz, transcripts, fence matching (D-24, L-22)
- [FIX] [W0.1] — verifier pass 6: transliteration, JSON escapes, spaced-markup view, reference links, UTF-16 bytes; corpus guard in pre-commit; HAR-08 enumerated (D-25, L-23)
- [FIX] [W0.1] — verifier pass 7: transliterate after decoding; phrase split on any comma or full stop; UTF-32 byte forms
- [FIX] [W0.1] — verifier pass 8: per-term clause breaks, sentence-ending full stops, double %-decoding
- [FIX] [W0.1] — verifier pass 9: quoted-printable and MIME names, exact held-out allowance, Scandinavian spellings, sentence ends before tags and quotes
- [FIX] [W0.1] — verifier pass 10: removed the zero-width-as-space view, cp1252 only for non-UTF-8, PDF endstream and corrupt gzip (L-24)
- [FIX] [W0.1] — verifier pass 11: one plausible reading for wide encodings, cp1252 only for legacy bytes, sentence ends before emphasis
- [FIX] [W0.1] — verifier pass 12: text must be UTF-8 (non-UTF-8 text is a finding), encoding guessing removed, binary by content magic (D-26, L-25)
- [FIX] [W0.1] — verifier pass 13: UTF-8 first then binary magic, fonts and media magic, stricter ICO, quoted hyphen breaks
- [FIX] [W0.1] — verifier pass 14: binary needs control bytes and valid font/movie headers; CR-terminated PDF streams
- [FIX] [W0.1] — verifier pass 15: binaries only in sample_course/ and docs/assets/ with suffix and magic agreeing; PDF streams read as UTF-8; nested encodings (D-27, L-26)
- [HARNESS] [W0.1] — green after 16 verifier passes: harness, CI, records, secret scan and corpus guard (D-22 to D-27, L-16 to L-26)
