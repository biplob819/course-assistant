# N1.4 acceptance criteria

**ING-01** Each sample-course pptx yields one unit per slide with title-prefixed text, notes only when allowed; each pdf yields one unit per page; the near-empty canvas slide is dropped and reported; a file whose hash differs from the manifest is refused.
**ING-02** Units are ordered session, kind, manifest order, locator; `unit_index` is 1-based and stable when the manifest is reordered within a kind; the corpus version changes when and only when a unit's text or locator changes; every golden-set locator resolves.
**ING-03** New units are uploaded and attached with all seven attributes, and ingest waits for each to complete; a failed or cancelled file stops the run with a config error.
**ING-04** A unit no longer in the manifest is removed from the vector store and its File object is deleted, in the same run.
**ING-05** After applying, the store's file list equals the planned units; any difference exits 2 with the ids that differ.
**ING-06** `ingest --dry-run` prints the plan and makes no API calls (works with no key); a second run with no change makes zero write calls.
