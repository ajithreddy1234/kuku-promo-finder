# Fresh manual-upload session

## Missing eligibility gate, corrected 8 November

A high-scoring candidate with `cliffhanger_type = none` passed the previous final eligibility gate. This was a code-policy bug: scores were allowed to stand despite a required component being absent. Added matching audience-screen and final-review gates, regression tests for none/uncertain types, and a separate offline correction of saved results. The five-row shortlist becomes four AI suggestions. Original judgments and scores remain unchanged; no human approval is claimed.

Previous notes and evidence are preserved in the recoverable reset archive. No prior result is preloaded or claimed as a fresh run. Keys and real quota accounting remain. Current architecture: local React/FastAPI, Groq free-plan roles and JSON/JSONL persistence. No database or app deployment is required by the brief.

## Consequence-target paraphrase, 7 November

During a development source audit, an audience reviewer described the person being coerced as facing execution, although the exact source threatened that person's relative. The context judge incorrectly agreed, including after an explicit target-audit prompt. This was an AI inspection, not a human reference label. The initial benchmark eligibility must not be used as verified quality evidence.

The revised audience schema requires an exact threat-target quote and source record, checked against the selected clip. The context schema separately extracts source/described targets. A conservative code guard routes a relationship-target paraphrase to editor review when the relationship/name cannot be reconciled. Exact quotes remain visible to the editor. A subsequent new-version run described the relative correctly. This reduces the observed failure; it does not establish universal factual correctness.

## Recovery of the whole-pack run, 8 November

One episode's local model response used blank strings in optional resolution arrays. Strict source validation rejected both attempts even though its substantive source references were valid. The repair normalizes only blank optional resolution placeholders to unknown/empty, preserves every factual reference and the raw response, and records the normalization. It does not repair missing/unknown hook evidence or invent a resolving event. The existing response was recovered without another inference call.

An oversized context package originally stopped all further context reviews. Context is now compacted losslessly, optional surrounding records are bounded, and a selected clip that still cannot fit is left unverified while other candidates continue. A separate null-camera-text attribution bug was fixed with a regression test. An empty structural audit is labelled as having no recommendations to validate, rather than treated as pipeline success.

The recovery run reused 49 source analyses, completed 21 audience reviews and five context reviews, and exported five AI-eligible suggestions. Structural source/cut validation passed for those five. No human acceptance or ad-performance result is claimed.

## Coverage and editorial evidence correction

The previous early-preview allocation spent reviews near the start of the show. Replaced it with a deterministic coverage-first allocator, disabled previews for that profile, and added source-bound reservations. New reports distinguish source-ready, eligible, candidate-bearing and actually reviewed episodes.

Four historically eligible clips carry editor flags under the fixed policy. They are now editor-review material in a separate derived view; the no-cliffhanger candidate stays rejected. Raw judgments and model scores are preserved. This is code-policy correction, not human quality approval.
