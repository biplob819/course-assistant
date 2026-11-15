# N1.3 acceptance criteria

**CRS-01** Three sessions exist as YAML source; the build script produces one pptx per session (one slide per source slide, in order, titles and bodies intact, notes where given) and one pdf pre-read per session (one page per source page, text extractable), and two builds from the same source are byte-identical.
**CRS-02** `sample_course/LICENSE` is CC BY 4.0, and every manifest entry has `licence: CC-BY-4.0`.
**CRS-03** `manifest.yaml` loads with `load_manifest`, has one entry per built file, and each `sha256` matches the built file; a changed source without a rebuild fails the check.
**CRS-04** `assessments.yaml` loads as `Assessment` models with at least 3 briefs per session, no solution text, and is not referenced by the manifest; solutions exist only under the git-ignored `golden/heldout/solutions/`.
**CRS-05** `absent_topics.yaml` lists at least 12 topics, and none of their keywords appears in any slide, note or page text of the sample course.
