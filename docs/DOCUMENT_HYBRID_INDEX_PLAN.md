# Document Reading + Hybrid Search Index for YUYUTSAVA

## Context

Today YUYUTSAVA can only read **UTF-8 plain text**. The single content path is
`tr_read_file` → `execute_read` → `_sync_read_paginated`, which does
`path.read_text(encoding="utf-8", errors="replace")`
([executor.py:242](yuyutsava/agents/task_runner/executor.py#L242)). Consequences:

- **Binary docs (PDF/DOCX/XLSX/PPTX) return replacement-char garbage**, silently — no error.
- **Non-UTF-8 text/code** (latin-1, utf-16, cp1252, shift-jis …) is mangled.
- **Large docs blow up context** — reading a whole doc dumps it into the model.

We want a *proper*, reliable capability, not a workaround: read **all formats**
(PDF, DOCX, XLSX, PPTX, MD, PY, JSX, TSX, YML, JSON, CSV, HTML, …) in **any
encoding**, and index large ones into a **hybrid (semantic + lexical) search
store** so the agent retrieves only the relevant slices — never the whole file.

The codebase already has almost all the machinery: a reusable pgvector engine
(`yuyutsava/retrieval/`), a char-based chunker, an embedder, a forward-only PG
migration system, and a mature "digest + targeted-fetch" context-offload layer.
This design **extends** those; it does not duplicate them. The only genuinely
new piece is the **lexical (full-text) + RRF fusion** — there is no BM25/tsvector
anywhere today.

## Locked decisions (from Q&A)

| Decision | Choice |
|---|---|
| Extraction backing | **Granular per-format libs** (pypdf/pdfplumber, python-docx, openpyxl, python-pptx) behind a pluggable **extractor registry**; MarkItDown optional for long-tail formats only. Chosen for agent control: real page/sheet/slide locators. |
| PDF OCR | **Tiered**: text-layer first (pypdf/pdfplumber) → OCR fallback for scanned PDFs (OCRmyPDF+Tesseract local default; cloud/MCP OCR — Mistral/Azure — config-gated for hard scans). |
| Encoding | **Detect all encodings** via `charset-normalizer` (already transitive through `requests`) with utf-8→latin-1 fallback chain. |
| Indexing trigger | **Auto-on-attach + explicit tools.** Files attached to TODO cards / uploaded auto-index in the background; agent also has explicit `doc_*` tools. |
| Hybrid retrieval | **Vector + Postgres full-text (tsvector), fused by Reciprocal Rank Fusion (RRF).** |
| Format scope (v1) | **Modern formats only.** Legacy OLE `.doc/.xls/.ppt` deferred (would need LibreOffice-headless). |

## Non-goals (v1)

- Legacy `.doc/.xls/.ppt` (OLE). - Watched-directory corpus indexing.
- Cross-encoder reranking. - Replacing native model ingestion for tiny one-off
  reads (that path stays; this is the *indexable* store for large docs).

---

## Architecture

```
                       ┌─────────────────────────────────────────────┐
  file (any format) →  │ 1. EXTRACTION LAYER  (new: yuyutsava/documents/) │
                       │   registry.extract(path) → ExtractedDoc         │
                       │     • encoding-detect for text/code             │
                       │     • pypdf/pdfplumber (+OCR tier) for PDF       │
                       │     • python-docx / openpyxl / python-pptx       │
                       │   → normalized text + per-segment locators      │
                       └───────────────┬─────────────────────────────────┘
                                       │
                       ┌───────────────▼─────────────────────────────────┐
   idempotent (sha256) │ 2. INDEX PIPELINE (new: PgDocumentIndex)         │
   fire-and-forget     │   chunk_text() → embed(mode=document) → INSERT   │
                       │   documents + document_chunks (embedding + tsv)  │
                       └───────────────┬─────────────────────────────────┘
                                       │
                       ┌───────────────▼─────────────────────────────────┐
   never dumps whole   │ 3. HYBRID SEARCH  (extend PgVectorSearch)        │
   doc into context    │   vector (cosine <=>) ⊕ lexical (ts_rank_cd)     │
                       │   fused by RRF → top-k chunks + locators         │
                       └───────────────┬─────────────────────────────────┘
                                       │
        ┌──────────────────────────────▼──────────────────────────────┐
        │ 4. AGENT SURFACE  (new doc_* tool family)                    │
        │   doc_index(path)  → compact manifest {doc_id,pages,outline} │
        │   doc_search(q)    → ranked snippets + locators (budget-cap) │
        │   doc_read(doc_id, page/offset, limit) → one slice on demand │
        │   doc_list()       → indexed docs                           │
        │   + tr_read_file binary-guard → points at doc_* not garbage  │
        └─────────────────────────────────────────────────────────────┘
```

**How context-blast is prevented (the crux):** the agent never receives the
whole document. It gets (a) a **small manifest** from `doc_index`, (b) only the
**top-k relevant chunks** from `doc_search` (bounded by a char budget, exactly
like `RetrievalInjector.build_block`), and (c) a **specific slice** from
`doc_read` on demand (paginated, like `tr_read_file`/`ctx_fetch_artifact`). This
is the *same discipline* the existing `ToolResultOffloadMiddleware` + `ctx_*`
tools already enforce for oversized tool results — we reuse it, not reinvent it.

---

## Components

### 1. Extraction layer — new package `yuyutsava/documents/`

Mirrors the existing pluggable-registry idiom (`todoboard/artifacts.py`
`ArtifactBlock` registry + `_MAGIC` byte-signatures in
[tools.py:60](yuyutsava/agents/task_runner/tools.py#L60)).

- `documents/extractors/base.py` — contract:
  ```python
  @dataclass(frozen=True)
  class Segment:      # one page / sheet / slide / heading region
      text: str
      locator: dict   # {"page":5} | {"sheet":"Q3"} | {"slide":12} | {"heading":"..."}
  @dataclass(frozen=True)
  class ExtractedDoc:
      text: str                 # full normalized text (concatenated segments)
      segments: list[Segment]   # structural locators → precise doc_read targeting
      mime: str
      page_count: int | None
      title: str | None
      outline: list[str]        # headings / sheet names / slide titles → manifest
  class Extractor(Protocol):
      def can_handle(ext, mime, head_bytes) -> bool
      def extract(path) -> ExtractedDoc
  ```
- `documents/registry.py` — `register(extractor)`, `resolve(ext, mime, head)`,
  `extract(path)`. Detection reuses the `_MAGIC` sniffing approach + mimetypes.
- **Extractors** (one file each, lazy imports so heavy deps load only on use):
  - `text.py` — text/code (md, py, jsx, tsx, yml, json, csv, html, txt, …).
    **Encoding**: `charset_normalizer.from_bytes(raw).best()`, fallback
    utf-8→latin-1. This is the "all encodings" answer.
  - `pdf.py` — **tiered**: pypdf/pdfplumber text-layer → if `chars/page` below
    threshold, OCR fallback (`ocrmypdf`/`pytesseract`, lazy). OCR backend is a
    config seam (`local` | `mistral` | `azure` | `off`); degrades gracefully with
    a clear notice if Tesseract absent. Segments carry `{"page": n}`.
  - `docx.py` — python-docx; segments per heading. `xlsx.py` — openpyxl; segment
    per sheet, locator `{"sheet":name}`. `pptx.py` — python-pptx; segment per
    slide + speaker notes, locator `{"slide":n}`.
  - (optional) `markitdown_ext.py` — long-tail formats only.

### 2. Storage — PG migration **v17** in [migrations.py](yuyutsava/storage/pg/migrations.py)

Append `(17, sql)` (current max is v16 — never edit an applied migration). Two tables:

```sql
CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,
    thread_id   TEXT,                 -- nullable; session-scoped ⇒ CASCADE cleanup
    source_path TEXT NOT NULL,
    mime        TEXT, ext TEXT,
    sha256      TEXT NOT NULL,        -- idempotency / staleness key
    size_bytes  BIGINT, mtime DOUBLE PRECISION,
    page_count  INT, chunk_count INT,
    title       TEXT, outline JSONB,
    status      TEXT NOT NULL DEFAULT 'indexed',   -- indexing|indexed|failed
    created_ts  TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_ts  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS documents_sha_idx ON documents (sha256);

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id    TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    seq         INT NOT NULL,
    char_offset INT NOT NULL,         -- maps hit → exact region (like artifact_chunks)
    locator     JSONB,                -- {"page":5} etc.
    text        TEXT NOT NULL,
    embedding   vector(768),          -- nullable → NULL-row fallback + backfill
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX IF NOT EXISTS document_chunks_embedding_idx
    ON document_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS document_chunks_tsv_idx
    ON document_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS document_chunks_doc_idx ON document_chunks (doc_id);
-- optional thread FK on documents (NOT VALID → VALIDATE) for CASCADE cleanup
```

Conventions copied from v13/v16: `vector(768)` (nomic-embed-text; pgvector can't
ALTER dims), nullable embedding, HNSW cosine. The **`tsv` GENERATED column + GIN
index is the only new pattern** — it is what makes true lexical search possible.
Vector/FTS tables stay **Postgres-only** (no SQLite twin — same as
transcript/artifact chunks); resilience is NULL-row backfill + boot re-index.

### 3. Index pipeline — new `yuyutsava/documents/index.py :: PgDocumentIndex`

Direct analogue of [transcript_index.py](yuyutsava/context/transcript_index.py)
`PgTranscriptIndex` — copy its structure:

- `index_document(path, thread_id=None)` — **idempotent by sha256**: if a
  `documents` row with the same hash exists and file unchanged, skip. Else
  extract → `chunk_text(seg.text, target_chars=1200)` (reuse
  [chunking.py](yuyutsava/retrieval/chunking.py), carrying each chunk's `locator`
  + `char_offset`) → `embedder.embed(texts, mode="document")` → INSERT
  `documents` + `document_chunks`. On embed failure store **NULL-embedding rows**
  (keyword-findable, later re-embedded by `PgVectorSearch.backfill`). Never raises.
- Fire-and-forget spawner (`_spawn` + `set[Task]`) exactly like
  `PgTranscriptIndex.index_messages`.
- `search(query, k, filters)` implementing `VectorStore` (drives hybrid — §4).
- `sync()` / boot re-index of `status='indexing'` stragglers, wired in bootstrap
  like `TodoNoteIndex.sync`.

### 4. Hybrid search — extend [pg.py](yuyutsava/retrieval/pg.py) `PgVectorSearch`

Add an optional `text_search_col: str | None = None` to `PgVectorTable`, and a
new method (keeps the caller-owns-connection + `where`/`params` contract):

```python
async def hybrid_search(self, conn, qvec, query, k, *,
                        where="", params=(), rrf_k=60, candidate_k=40) -> list[Hit]:
    # CTE v: top candidate_k by cosine (embedding <=> %s::vector)
    # CTE l: top candidate_k by ts_rank_cd(tsv, websearch_to_tsquery('english', %s))
    # FULL OUTER JOIN on id_col; score =
    #   coalesce(1/(rrf_k + v.rank),0) + coalesce(1/(rrf_k + l.rank),0)
    # ORDER BY score DESC LIMIT k
```

RRF needs no score normalization — robust for exact terms/code identifiers AND
semantics. `PgDocumentIndex.search` calls `hybrid_search` when the query embeds,
falls back to `keyword_search` (existing) when the embedder is down — same
try/except ladder as `PgTranscriptIndex.search`. This method is generic and could
later upgrade memory/skills to hybrid too.

### 5. Agent surface — new `doc_*` tool family

New factory `yuyutsava/tools/documents.py :: make_document_tools(...)` (mirror
`make_search_tools` in [search.py](yuyutsava/tools/search.py)):

- `doc_index(path, reason)` → `{doc_id, mime, pages, chunk_count, outline}` — small manifest.
- `doc_search(query, doc_id=None, k=8, reason)` → ranked snippets + locators,
  **budget-capped** (reuse `RetrievalInjector`-style whole-entry fitting).
- `doc_read(doc_id, page=None, offset=0, limit, reason)` → one slice by
  locator/offset — paginated like `tr_read_file`.
- `doc_list(reason)` → indexed docs for this thread.

**Registration** — one edit in `_build_tool_registry_and_tools`
([engine.py:302](yuyutsava/core/engine.py#L302)): add `document_tools` to
`all_custom_tools` (covers CLI agent + orchestrator via `master_tools`). Add
`"doc_"` to `_SUPPRESS_PREFIXES`
([tool_filter_middleware.py:48](yuyutsava/core/tool_filter_middleware.py#L48))
for lazy discovery. Subagents opt in via `extra_tools()`
([base_sub_agent.py](yuyutsava/agents/base_sub_agent.py#L225)).

**Prompt** — add a short section to
[task_runner/prompts.py](yuyutsava/agents/task_runner/prompts.py): "For
PDF/DOCX/XLSX/PPTX or large docs use `doc_index`/`doc_search`/`doc_read` — never
`tr_read_file` (which only reads UTF-8 text)."

**`tr_read_file` binary guard** — small, high-value fix in
[executor.py](yuyutsava/agents/task_runner/executor.py): sniff `b"\x00"` in the
first 8 KB (the grep path already does this at
[executor.py:363](yuyutsava/agents/task_runner/executor.py#L363)) and, for a
binary/non-text file, return the **already-existing-but-unused**
`SuppressedContentNotice.binary_content()`
([tool_messages.py:153](yuyutsava/models/tool_messages.py#L153)) with a recovery
hint pointing at `doc_index`, instead of replacement-char garbage.

### 6. Auto-on-attach

Hook the existing attachment path so uploaded/attached docs index in the
background. In [todoboard/artifacts.py](yuyutsava/todoboard/artifacts.py) the
`file` block already accepts `application/pdf`; extend `upload_mimes` to the doc
mimes and, on successful attach, fire `PgDocumentIndex.index_document(path,
thread_id)` fire-and-forget (best-effort, mirrors `TodoNoteIndex.schedule`).

### 7. Wiring

- Construct `PgDocumentIndex(pg_pool, embedder=embedder)` where
  `PgTranscriptIndex` is built — [agent_stack.py:227](yuyutsava/cli/agent_stack.py#L227)
  (CLI) and [daemon/bootstrap.py](yuyutsava/daemon/bootstrap.py) (daemon); `None`
  on the SQLite/no-PG fallback (docs simply not indexable then, like transcript recall).
- Pass into `make_document_tools`; register per §5.
- Lifecycle: `document_chunks` CASCADEs from `documents`; session-scoped
  `documents` CASCADE from `threads`; add table names to
  `storage/purge.py` if explicit purge is wanted.

### 8. Dependencies (`pyproject.toml`)

Add: `pypdf`, `pdfplumber`, `python-docx`, `openpyxl`, `python-pptx`,
`charset-normalizer` (explicit; already transitive). **OCR extras** (optional
group): `ocrmypdf`, `pytesseract`, `pdf2image` — lazy-imported; system
`tesseract`+`poppler` documented as optional. **Optional**: `markitdown`
(long-tail), `docling` (high-accuracy OCR backend). All core libs are pure-Python
MIT/BSD; avoid PyMuPDF in core (AGPL). Because `tr_run_python` uses
`sys.executable` (the daemon interpreter), these are immediately importable by
in-process tools and sandbox scripts alike.

---

## Phasing

1. **Extraction layer** — `documents/` registry + text(+encoding)/pdf/docx/xlsx/pptx
   extractors; unit-test on sample files. *No DB yet.*
2. **Schema + index pipeline** — migration v17; `PgDocumentIndex` write path
   (sha256 idempotency, NULL-row fallback). `doc_index` tool + manifest.
3. **Hybrid search** — `hybrid_search` (RRF) on `PgVectorSearch`; `doc_search` /
   `doc_read` / `doc_list` tools; prompt section; `tr_read_file` binary guard.
4. **Auto-on-attach + OCR tier** — attachment hook; scanned-PDF OCR fallback;
   boot `sync`/backfill wiring.

## Verification

- **Extraction**: standalone script over a fixtures dir (a born-digital PDF, a
  scanned PDF, .docx/.xlsx/.pptx, and text/code in utf-8/utf-16/latin-1) →
  assert non-empty text, correct locators, correct decoding. (Fast; avoids the
  heavy langgraph import per the "avoid heavy import tests" note.)
- **Hybrid search**: seed `document_chunks`, run `hybrid_search` for a semantic
  query and an exact-identifier query; assert RRF surfaces the right chunk for
  both (vector-only would miss the identifier).
- **End-to-end** via `/run`: index a large PDF, confirm the manifest is small,
  `doc_search` returns bounded top-k snippets, `doc_read(page=N)` returns one
  page — and confirm the model context never receives the whole document.
- **Regression**: `tr_read_file` on a PDF now returns the `binary_content`
  notice (not garbage); `tr_read_file` on a utf-8 text file unchanged.
- `vite build` if any renderer surface (attachment tile) changes.

## Notes / alternatives on record

- **OCR MCP alternative**: instead of in-process OCRmyPDF, route the OCR tier to
  an MCP server (Mistral OCR / Azure Document Intelligence / `markitdown-mcp`) via
  the existing MCP client — same registry seam, config-gated. In-process chosen as
  default for privacy + reliability.
- **Reranking**: RRF now; a cross-encoder rerank tier can bolt on later behind
  `doc_search` without schema change.
- **Watched corpus**: a daemon loop that keeps a directory indexed (staleness by
  mtime/sha) is a clean future extension on this exact schema.
