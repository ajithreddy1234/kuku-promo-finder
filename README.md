# Kuku Promo Finder

A local React + FastAPI tool that finds 20–60 second promo selections from timed screenplay files. Code controls timestamps and boundaries; models assess hooks, clarity and unresolved endings.

## Run

Python3.12, uv and Node/npm are required.

```bash
uv sync --frozen
uv run python -m tools.studio
```

Open http://127.0.0.1:8000/. The launcher installs locked frontend packages and builds React if needed. The private server binds only to loopback.

## Use

1. Add scripts: upload a ZIP, an entire folder, or multiple episode files, then Check my scripts. No AI call occurs during import.
2. Find promos: all usable episodes are selected by default. Use Analyze all or Choose episodes myself, select a processing mode, then explicitly start analysis. Large cloud runs can pause at free usage limits; progress is saved.
3. Review & download: read the proposed cuts, save your own keep/change/reject decisions and download timestamps/reasons.

The app starts empty. There is no preloaded show, saved demo replay, forced reference cohort, six-episode cap or fixed episode count. No reference labels are required for normal discovery. Start a new run reuses an upload without overwriting the original run; it does not start inference until you click Find promos.

## Pipeline

Parse every input → analyse every selected usable episode once → build a source-linked whole-show index → assemble exact continuous cuts → allocate a bounded audience-review pool across chronological bands → context-check leading clips → deduplicate and rank → editor decision and exports.

The default ceilings are two audience candidates per episode (100 globally), 30 context checks and up to 20 AI-eligible recommendations. Conservative remaining quota can reduce the allocated pass. The coverage-first profile disables early previews so early episodes cannot consume later coverage slots. Every recommendation requires both reviews. Partial runs are labelled provisional; unreviewed candidates are not rejected. Selecting only some episodes cannot establish the best promos across the whole show.

For new runs, the review pool is reduced to fit conservative remaining cloud capacity before deeper reviews begin. Cached exact responses cost no new call. Budget exclusions stay unreviewed; this is not exhaustive best-clip recall. This policy does not alter older pinned runs.

The whole-show index covers usable, non-reserved source records. Context retrieval uses bounded lexical matches with exact source IDs/quotes; it can miss paraphrases, multilingual links and unstated relationships. It is not exhaustive story understanding. Only the context reviewer receives this evidence. Audience requests remain isolated and clip-only. Native dialogue can still reveal names/backstory.

Context packets use a compact wire representation. Full selected source is preserved; optional surrounding context is bounded. A candidate that still exceeds capacity remains unverified while the rest continue. Blank optional resolution placeholders are normalized to unknown; substantive source IDs remain strictly validated and raw responses remain unchanged.

## Inputs

ZIP or individual timed TXT/Markdown, SRT/VTT subtitles, timed CSV, canonical Episode JSON, or DOCX/text PDF containing supported timing blocks. Untimed scripts need timestamps; scanned PDFs need OCR. Exact time is never invented from reading speed. The browser accepts4MB per file,32MB per ZIP and64MB total upload. CLI also accepts folders.

Invented source fixtures are in data/examples/manual-test-pack. They contain no Kuku content or precomputed recommendations. Upload them yourself if you want a small test. The private original pack is not in source control.

## AI and keys

Set GROQ_API_KEY in the backend .env; never in React or a VITE variable. Roles remain pinned per run. Choose:

- Groq: OSS120b analyses episodes, OSS20b reviews isolated clips, and Qwen 3.8 27B checks context. The roles use separate model budgets. Fast responses do not guarantee sufficient free daily capacity.
- Hybrid: an installed local Ollama model analyses episodes; Groq OSS20b reviews clips and Qwen 3.8 27B checks context. This saves cloud analysis quota. Local discovery quality and free review capacity remain limitations.
- Local: all roles run through an installed local Ollama model. No API key or inference-service bill; hardware/electricity are still needed. Reviews use the same model and are exploratory. This is not guaranteed to be faster or equally capable.

The app lists installed local models; it does not download models. Cloud-tagged Ollama models are rejected, and local requests use only 127.0.0.1. Structured output, exact source anchors and exact threat-target quotations are validated. The consequence-target audit is supplemented by a conservative relationship-paraphrase warning; no model judge is a guarantee of factual correctness.

Eligibility policy v2 rejects `cliffhanger_type: none` even when scores are high, and routes an uncertain type to editor review. Offline policy rechecks retain original responses/scores and identify the policy version separately. Coverage-first allocation is implemented: highest discovery priority, then complete source records, fewer boundary warnings, a larger duration margin and stable timestamps/IDs. The first pass prefers different episodes before expansions, and re-stratifies after a budget reduction. Reports distinguish 43 eligible episodes from the number actually reviewed; reserved/exposed inputs are excluded by source-bound policy. Full three-lane overlap is deferred.

Gemini is optional for synthetic diagnostics only and is not used on confidential screenplay input. Its token usage was not recorded. No automatic Gemini fallback occurs. Preflight makes tiny real API calls and consumes quota; do not run it as an offline check.

## Persistence and free limits

Local per-project JSON/JSONL stores source timelines, review evidence, editor decisions and checkpoints. No external database is required. Exact-request caching and an org/model quota ledger protect free capacity. Real prior usage remains accounted after clearing app state. Published free limits are planning assumptions, not account-console verification. A fifty-episode full AI run is not guaranteed to fit one free quota window.

Uploads, source validation and the pre-run capacity estimate work without hosted inference. A Groq/hybrid run needs the backend key and capacity; local mode needs a running local Ollama service and installed model. Cached input is excluded from quota charges only when reported by the provider. Unknown historical usage stays conservatively charged. No paid tier was activated. Deployment and an external database are optional; localhost alone is not an accessible submission link.

## Checks and CLI

```bash
uv run python -m pytest -q
npm --prefix web ci
npm --prefix web run build
uv run python run.py --input data/examples/manual-test-pack --output runs/check-input
uv run python -m tools.batch_worker --input /path/to/episodes.zip --output runs/my-run --max-minutes 30
uv run python -m tools.batch_worker --input /path/to/episodes.zip --output runs/my-hybrid-run --profile hybrid --episodes all --max-minutes 45
```

Groq and hybrid commands consume inference quota. Empty batch selection derives all episode numbers from the input, excluding only explicitly reserved references and unusable timing. Repeat the same command to resume; changed input/model/config needs a new run. Use --episodes 1,5,12 for an explicitly limited subset.

Keep .env, runs, labels, the original pack and real source quotations out of public Git. The app is a private local prototype, not an authenticated public multi-user service. It finds continuous source clips; footage editing, stitching, OCR and ad-performance prediction are outside this version.

## Fixed editor routing and review recovery

The main review desk has AI suggestions, Needs your judgment, and Not reviewed yet. A question ending, partial camera record, duration margin below one second, any uncertain review field, or a backup-only judgment goes to editor review when it otherwise passes. A missing cliffhanger remains rejected. Failed/capacity-limited requests stay unfinished, never quality-rejected.

Two exhausted primary audience attempts allow one attempt on the configured backup with the identical frozen prompt/schema. `judged_by` records actual models; backup-only scores stay outside the homogeneous primary ranking. Local-only mode disables the cloud backup. Successes and attempt limits survive resume.

Private source-hash reservations can exclude a particular uploaded source without reserving the same episode number in unrelated shows. A reservation made after analysis does not establish held-out independence. Human labels, a matched baseline and the failure log remain separate quality evidence; no human labels or baseline improvement are fabricated.

## Cut-first delivery update

The main result is now a downloadable promo cut sheet: episode, start/end, hook, cliffhanger, why, source warnings and the selected screenplay text. Source-checked editor-flagged rows may be delivered as **draft cuts**, preserving their editor-review status and lack of human approval. Unreviewed, resolved, misleading and timing-invalid rows are not delivered as completed cuts. No edited video is produced; the assignment asks for moments and boundaries only.

New uploads do not inherit saved cuts, even when their screenplay bytes match an earlier upload. The app and cut exports show only the selected run's completed evidence. Historical lookup remains available solely as an explicit offline audit argument; no app endpoint enables it. Public request caches belong to each run, so identical new uploads cannot replay another upload's model responses. Resume the same run to continue its own completed work.

Normal new uploads no longer inherit optional benchmark reservations. The `respect_evaluation_reservations` setting enables them for a deliberate evaluation profile; existing pinned runs retain their prior settings. Actual protected reference hashes remain blocked. Source timing exclusions remain in effect.

## Hosted submission

See [PUBLIC_DEPLOYMENT.md](PUBLIC_DEPLOYMENT.md) and `render.yaml` for the free Docker deployment. The hosted page starts empty, uses server-side Groq configuration, and requires no account login. Anonymous sessions isolate uploads/results. Download the cut sheet or private raw outputs before Clear this session or a free-host restart. No Ollama server is used in deployment.
