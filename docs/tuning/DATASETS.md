# Candidate downloaded sets for tuning: what must be verified before any file is fetched

Status: checklist, nothing downloaded, nothing verified. Every fact about a dataset below is "to verify" (TBV); where a recollection
is mentioned it is marked as a recollection and is not a fact. The owner approves each download file by file, with the source, the
size and the checksum (TUNING 3, Q9). No dataset content goes in this repository: only aggregates computed from it may, and only if
the licence allows publishing aggregates.

The kit side is ready: `tunekit.external.load_beir_like` turns a corpus, queries and relevance judgements in the BEIR file layout
into the harness's cell format, so the same baseline curves and signal tables (Z2, Z6) run on a downloaded set. It was tested on a
tiny synthetic fixture only.

## What every candidate needs before use (the checklist)

For each candidate, record these in the run file (not in the repository), each from the official source and with the date read:

1. **Official source URL**: the page the dataset's authors maintain (not a mirror). TBV.
2. **Licence text and its terms for this use**: internal evaluation, tuning of defaults, and publishing aggregate numbers in a public
   repository that does not redistribute the data. A licence that forbids any of these, or that is silent, stops the download. TBV.
3. **Size**: compressed and expanded bytes of exactly the files wanted, and the free disk on the host against the 15 GB budget. TBV.
4. **Format and label granularity**: file layout, ids, whether relevance labels are per passage, per document or per session, graded
   or binary, and whether a "turn" or "session" unit exists (the signals use sessions). TBV.
5. **Splits**: the authors' own train, development and test splits, if any. Tune on development portions only; never on a test split
   (TUNING 4.2). A set with no split can be used for description only. TBV.
6. **Version and checksum** of each file (recorded in the run file). TBV.
7. **Personal data and contamination**: whether it holds personal data, and whether the pinned embedding model's or the answering
   models' training data may include it. TBV.
8. **Owner approval**: file, source and size, in the owner's own words.

## Candidates named by TUNING 3

| Candidate | Use here | What is to be verified beyond the checklist |
|---|---|---|
| LongMemEval, cleaned S set (already used by the registered experiment; not stored here) | DEV of TUNING 1; development splits only | whether the development questions and their content clusters are available to this kit at all (TUNING 4.2 assumes 120 development questions, count TBV); that the fresh confirmatory set and the registered test split are never read by any tuning run (the run driver must refuse them) |
| LoCoMo | multi-session dialogue: D9, D10 and a descriptive look at D11 | TBV: number of conversations, sessions per conversation, whether it has any train, development and test split (a recollection, not checked, is that it has none, which would limit it to description); whether question labels point at sessions or turns |
| MSC (Multi-Session Chat) | topic shifts and returns, descriptive for D11 and D12 | TBV: splits; whether any label says which earlier memory should be offered at a turn (a recollection, not checked, is that it has none, so AT-6 stays synthetic only) |
| A standard BEIR corpus | flat-retrieval difficulty features with real relevance labels (D3, D4) | TBV: which corpus; its licence per corpus (collections inside one benchmark can carry different terms); corpus size against the host; qrels format; whether queries are short enough to look like agent recall queries |
| A passage-ranking set | the same | TBV: which set; licence; whether a development portion with labels is public; label sparsity (a set with one judged passage per query counts unjudged relevant passages as misses) |
| A multi-hop set | multi-session answers (one question needs facts from two sessions), D10 | TBV: which set; licence; whether supporting facts are labelled per passage |

## What the measurement will and will not give, if approved

- A real-text set gives the real embedder's geometry on real language: the level of `agree`, `margin` and `spread` and their AUC
  against real failures (D3, D4 levels). The synthetic grid gives only the shape (docs/tuning/RESULTS-Z2.md says why).
- None of these is known to hold "the memory that should be offered at this turn", so AT-6 (injection) cannot be decided on them
  (TUNING 3).
- A retrieval set with one judged passage per query understates recall, and the understatement differs between sets; compare
  within a set, not across sets.
- Which of these questions the product's own tree arm would answer differently needs model calls and is outside this kit.

## Order proposed for approval (the owner decides)

One candidate at a time, retrieval sets first (they serve D3 and D4 and have the simplest format), the dialogue sets afterwards and
descriptive only. Nothing is requested until the checklist for that candidate is filled in from its official source.
