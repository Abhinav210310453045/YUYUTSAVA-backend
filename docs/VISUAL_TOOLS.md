# Visual Wings — the `yuyutsava/visuals` library

Give the agent the ability to **show**, not just tell: charts, diagrams, styled
tables, syntax-highlighted code, math, and timelines — rendered to PNG without
paying for an image-generation model.

The core (`yuyutsava/visuals/`) is a **delivery-agnostic** library: the same
renderers are reused as LLM tools, a REST endpoint, and an SSE event. The CLI
sees the file path; the Electron UI shows the image (Artifacts panel today,
inline chat next).

---

## What each underlying tool/library does

| Tool / Library | Family | Input | Output | Runs where | Offline? | Notes |
|---|---|---|---|---|---|---|
| **matplotlib** | Data charts | data (arrays/JSON) | PNG | in-process | ✅ | bar, line, pie, scatter, histogram, box. Also powers tables & math. |
| **seaborn** | Statistical charts | tabular data | PNG (matplotlib) | in-process | ✅ | heatmap, violin, correlation. |
| **matplotlib table** | Styled tables | rows + columns | PNG | in-process | ✅ | Native table (no browser needed, unlike a pandas `.style` HTML export). |
| **Pygments** | Code-to-image | source + language | PNG | in-process | ✅ | Syntax-highlighted snapshot. Needs Pillow. |
| **matplotlib mathtext** | Math / LaTeX | LaTeX string | PNG | in-process | ✅ | Equations without a LaTeX install. |
| **Pillow** | Image utils | image bytes | image bytes | in-process | ✅ | Pulled in by Pygments' image formatter. |
| **Mermaid** | Diagram-as-code | mermaid script | PNG | Kroki | ✅ (self-host) | flowchart, sequence, ER, mindmap, gantt, class. |
| **Graphviz (DOT)** | Graph diagrams | DOT text | PNG | local `dot` **or** Kroki | ✅ | Dependency graphs, trees, networks. Local `dot` used when installed. |
| **PlantUML** | UML | PlantUML text | PNG | Kroki | ✅ (self-host) | Full UML coverage. |
| **D2** | Modern diagrams | D2 text | PNG | Kroki | ✅ (self-host) | Nicer aesthetics than Mermaid. |
| **Kroki** | Unified diagram API | any of the above | PNG/SVG | one HTTP service | ✅ (self-host) | Single endpoint renders Mermaid/Graphviz/PlantUML/D2 + ~20 more. |

**Design decision:** charts / tables / code / math render **in-process** (pure
Python, always offline). Diagram-as-code goes to **Kroki** so one HTTP service
covers every diagram language instead of installing four toolchains. Graphviz has
a local-`dot` fast path so the most common diagram works with no service at all.

---

## Installation

```bash
uv pip install -e ".[visuals]"     # matplotlib, seaborn, pandas, pygments, pillow
```

### Diagram backend (Kroki)

Only needed for `vis_diagram` with Mermaid / PlantUML / D2 (Graphviz works via a
local `dot` if present). Self-host with one container:

```bash
docker run -d --name kroki -p 8000:8000 yuzutech/kroki
```

Config via env (defaults to `http://localhost:8000`):

```bash
export YUYUTSAVA_KROKI_URL=http://localhost:8000   # or https://kroki.io (sends source out)
```

If no backend is reachable, `vis_diagram` returns a clean
`"diagram backend unavailable"` error — every other family keeps working.

---

## Using it

### 1. As agent tools (`vis_*`)

Discovered through the normal lazy tool catalog (`tool_search`). Available to the
master **and** background sub-agents. Each returns JSON with a disk `path` (for
the CLI/user) and a `url` (for the UI):

| Tool | Purpose |
|---|---|
| `vis_chart` | data → bar/line/pie/scatter/histogram/heatmap |
| `vis_diagram` | mermaid/graphviz/plantuml/d2 source → diagram |
| `vis_table` | rows + columns → styled table image |
| `vis_code` | source code → syntax-highlighted image |
| `vis_math` | LaTeX → equation image |
| `vis_timeline` | dated/numbered items → timeline / Gantt |

### 2. As a REST API

```bash
curl -XPOST localhost:7654/visuals/render -H 'Content-Type: application/json' -d '{
  "kind": "chart",
  "spec": {"chart_type": "bar", "title": "Sales",
           "labels": ["Q1","Q2","Q3"],
           "series": [{"name": "2026", "data": [4,6,5]}]}
}'
# -> {"visual_id": "vis_…", "kind": "chart", "url": "/visuals/vis_…", ...}
curl localhost:7654/visuals/vis_… -o chart.png
```

List a session's visuals (Artifacts panel): `GET /sessions/{id}/visuals`.

### 3. Over SSE / WebSocket

When a `vis_*` tool runs in a converse turn, the daemon emits an `image`
StreamEvent on `/ws/converse` alongside the normal `tool_result`:

```json
{"type": "image", "visual_id": "vis_…", "url": "/visuals/vis_…",
 "kind": "chart", "title": "Sales", "mime": "image/png"}
```

### 4. Directly in Python (library, no agent)

```python
from yuyutsava.visuals import render
result = render("diagram", {"language": "mermaid", "source": "flowchart TD; A-->B"})
open("out.png", "wb").write(result.image_bytes)
```

---

## Spec reference

- **chart** — `chart_type` (bar|barh|line|pie|scatter|histogram|heatmap; `barh`
  = horizontal bars, best for long category names), `title`,
  `x_label`, `y_label`; `labels` + `series:[{name,data}]` (bar/line/scatter);
  `values`+`labels` (pie); `values`+`bins` (histogram); `matrix`+`row_labels`+`col_labels` (heatmap).
- **diagram** — `language`, `source`, `title`.
- **table** — `columns:[…]`, `rows:[[…]]`, `title`, `highlight:{row,col}`.
- **code** — `source`, `language`, `title`.
- **math** — `latex` (no surrounding `$`), `title`.
- **timeline** — `title`, `items:[{label,start,end,status}]` (start/end are ISO
  dates or numbers; status ∈ done|active|todo|blocked).

## Storage & retention

PNGs are written to `_output/visuals/` in the workspace (so the CLI can point at
them) and indexed in the `visual_artifacts` table in `state.db`. They are
session-scoped user output: removed when the session is deleted (`purge_session`),
not aged out by the TTL sweeper.
