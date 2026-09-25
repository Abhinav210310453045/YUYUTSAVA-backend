# 07 — Errors, logging, state, and defects

How failures are handled, where output goes, what state is hidden in module
globals — and the behaviour defects found along the way.
Rules: [01 §8–9](01-standards.md). Numbers: [02](02-metrics.md).

**Headline.** Fifty-eight exception handlers catch everything and do nothing.
Seven stores are still reached through module-level singletons. The review
found four confirmed defects: compaction budgets for most providers, a price
cache that can destroy its own file, a renderer that drops visuals, and a dead
default port. It also found two suspected defects, which need a
reproduction before they are fixed.

---

## R-ERR-01 — Broad exception handlers that do nothing

**Severity** P1 · **Wave** W5 · **Status** Open

**Claim.** 58 handlers catch `Exception` (or everything) and only `pass`,
`continue` or return a falsy value.

**Evidence.** `measure_code_health.py --details` lists all 58. ruff counts
`S110` (try/except/pass) 50, `BLE001` (blind except) 67, `SIM105` 55.

| Files with the most | Lines |
|---|---|
| `cli/commands/chat_repl.py` | `110, 663, 693, 1158, 1322, 1344, 1356` |
| `cli/render/plain.py` | `305, 512, 530, 549, 574` (JSON parsing; `tool_format` catches `json.JSONDecodeError`) |
| `async_subagents/interrupt_middleware.py` | `119, 130, 146, 157` |
| `async_subagents/watcher.py` | `455, 457, 507, 566` |
| `context/meter.py` | `504, 510, 516` (around first-party imports, `R-IMP-02`) |
| `context/compaction.py` | `434, 460` |

**Consequence.** A real failure and an expected one look identical: nothing
happens and nothing is logged. When something is wrong, the logs are empty.

**Fix.** Classify each handler:

- **Boundary** (turn loop, teardown, background task, plugin): keep the broad
  catch, log with `exc_info=True`, and state the reason in
  `# noqa: BLE001  <reason>`.
- **Internal:** catch the specific exception the code can handle
  (`json.JSONDecodeError`, `OSError`, `KeyError` …) and let the rest propagate.

**Verification.** `silent_broad_excepts` 0; ruff `S110` gated at 0 and `BLE001`
only on allowlisted boundaries.

---

## R-ERR-02 — Failures that matter, silently converted into defaults

**Severity** P1 · **Wave** W5 · **Status** Open

**Evidence.**

| Where | What is lost |
|---|---|
| `core/retrieval_injection_policy.py:62-66` | Injector exceptions become an empty block with no log. `PrefsInjector.build_block` (`prefs/injector.py:59`) has no handling of its own, so a preferences-store failure disappears on every turn. |
| `core/tool_registry.py:71-74` | A broken tool argument schema is published as `{}`: the model sees a tool that takes no parameters. |
| `core/tool_result.py:46-96` | One `try` covers both JSON parsing and building the notice, so a programming error is reported as "could not be parsed". |
| `context/compaction.py:432-435` | If token counting fails, the summary is stored with `token_count=0`. |
| `llm/quirks/loop_affinity.py:66-74` | `except Exception: pass` at the bottom of a five-level nest. |
| `cli/agent_stack.py:265-269` | A missing module is reported as a price-refresh failure at DEBUG. |

**Fix.** Log each at a level an operator would see. Narrow each exception type.
Separate parsing from building in `tool_result`.

---

## R-ERR-03 — One failure, several exception types

**Severity** P2 · **Wave** W3–W5 · **Status** Open

**Evidence.**
- **A missing optional voice package** raises `AudioUnavailableError` in
  `io/audio.py:27-52`, but a plain `RuntimeError` with the same install hint in
  `io/stt.py:99-102,180-183`, `io/tts.py:93-94`, `io/wake.py:107-110` and
  `audio_io/synth.py:58-61,106-110`. The announcer handles only the first.
- **Re-raised exceptions lose their cause:** ruff `B904` counts 13
  `raise` statements inside `except` blocks without `from`.

**Fix.** One `VoiceDependencyMissing` raised through one `require(package)`
helper; `raise … from exc`.

---

## R-LOG-01 — Library code prints

**Severity** P1 · **Wave** W5 · **Status** Open

**Claim.** 86 `print` calls sit outside `cli/`, in code the daemon also runs.

**Evidence.**
- `core/streaming.py` holds 25: permission prompts and status lines to stderr
  (`:132`, `:200-264`).
- The `core/agent_profiles.py:379-394` prints sit inside its `__main__` block,
  which is acceptable, but the block itself belongs in a script.
- ruff `T201` counts 341 across `yuyutsava`, `test` and `scripts`.

**Consequence.** Output bypasses logging configuration and log levels, and the
daemon's Logs panel. In the CLI dashboard it depends on stdout being redirected.

**Fix.** Terminal interaction moves to `cli/render/` (`R-PLC-03`); everything
else uses `logging.getLogger(__name__)`.

**Verification.** `prints_outside_cli` 0.

---

## R-GLB-01 — Stores reached through module-level singletons

**Severity** P1 · **Wave** W5 · **Status** **Fixed** (legacy `F-S08`):
`set_default_definitions` 7 → 0 and `default_accessor_call_sites` 46 → 0.
Retiring them surfaced `R-BUG-17`, `R-BUG-18` and `R-BUG-19`.

**Claim.** Seven `set_default_*` functions remain. Together with their getters
there are 46 accessor calls and 25 `global` statements.

**Evidence.**
- **The seven setters:**
  - `agents/task_runner/tools.py:169` `set_default_policy`, `:181` `set_default_consent`
  - `storage/feedback_store.py:119`
  - `storage/sessions/sqlite_impl.py:352`
  - `todoboard/recall.py:272`
  - `todoboard/store.py:207`
  - `visuals/store.py:114`
- **Most accessor calls in one file:** `daemon/bootstrap.py`, with 10.
- **The task-runner setters write private attributes of cached agents:**
  `agents/task_runner/tools.py:178,191` (`agent._policy = policy  # type: ignore[attr-defined]`).

**Consequence.** Tests must reset globals. Two daemons or two test cases in one
process share state. What a function depends on is invisible from its signature.

**Fix.** Inject through constructors, FastAPI `Depends` and the existing
`AppContext`. Give `TaskRunnerAgent` a public setter or constructor parameter.

**Verification.** `set_default_definitions` 0, `default_accessor_call_sites` 0.

---

## R-GLB-02 — Hidden channels between components

**Severity** P2 · **Wave** W5 · **Status** Two fixed, the rest kept by decision
(2026-09-25):

- **Fixed: one retry-listener slot.** `add_retry_listener` keeps a list and
  returns each owner's remover, so the last registration no longer wins.
- **Fixed: the injected-block sizes were process-global.** They are now keyed
  by thread (and bounded), so one conversation's meter never reads another's
  blocks. It still reads its own thread's *previous* block: the meter sits
  before the injection in the chain, and moving it would change the pinned
  middleware order.
- **Kept:**
  - The `ctx_compact` flag is keyed by thread and cleared when consumed. A
    shared object would thread through four builders for no behaviour change.
  - Tracing's client and reachability cache are genuinely one per process.
  - `skills.injector.last_recalled` is a diagnostic only the CLI's `/skills`
    reads.
  - The meter's per-thread state is module-level on purpose. The masters,
    subagents and per-task orchestrator graphs must all see one thread's
    state, which a per-policy instance could not.

**Evidence.**
- **`context/compaction.py:54` `_FORCE_COMPACT`:** a module-level set that the
  `ctx_compact` tool writes and the middleware reads.
- **Process-global "last block":** `retrieval/injector.py:30` and
  `skills/injector.py:75` store the last injected block, which
  `context/meter.py:558` reads. The comment admits it "can be one turn stale" when
  the daemon serves concurrent threads.
- **`llm/quirks/first_chunk_retry.py:65-71`:** one global listener slot, so the
  last registration silently wins.
- **`context/meter.py`:** nine mutable module-level stores.
- **`core/tracing.py`:** reachability and client state in globals
  (`:85, 98, 114, 189, 219`).

**Fix.**
- A shared object injected into both the compaction tool and the middleware.
- Injectors return their block sizes alongside the block.
- A listener list.
- Meter state owned by the policy instance.

---

## R-BUG-01 — Most providers get a 128k context budget

**Severity** P1 · **Wave** W5 (`fix:` commit) · **Status** **Fixed** (2026-09-25).
Each settings class declares `max_input_tokens`, and the budget is found
through `SETTINGS_BY_PROVIDER`, the alias table `LLM_PROVIDER` resolves by.
Changed budgets: `google` 128k → 1M, `bedrock`/`aws` 128k → 200k. The rest
are unchanged but now declared; the `google_vertexai` key, which is not an
accepted alias, is gone. There is no changelog file, so this entry and the
commit are the record.

**Claim.** The compaction budget table misses most provider names, so those
providers compact far earlier than their models require.

**Evidence.**
- **The table:** `context/config.py:27-35` has keys `anthropic`, `groq`,
  `openrouter`, `ollama`, `vertex`, `gemini` and `google_vertexai`. Anything
  else falls to `_DEFAULT_MAX_INPUT_TOKENS = 128_000`.
- **The callers:** all three pass the raw `LLM_PROVIDER` value —
  `cli/agent_stack.py:189-191`, `agents/tinker/agent.py:67-69`,
  `daemon/bootstrap.py:1283-1286`.
- **The effect:** `google`, `openai`, `azure`, `bedrock`, `mistral` and `cohere`
  all get 128k. `google` compacts at 89.6k tokens while its alias `gemini` gets 1M.
- **The table's own comment** (`:23-26`) describes exactly this failure for
  Vertex and states the rule it breaks: budgets must be the model's real input
  window.

**Consequence.** Compaction summarises history away that the model could still
have held verbatim. It is the one step that drops messages from state.

**Fix.** Each provider declares its input window. The budget is looked up by the
resolved provider, not by string.

**Verification.** A test per accepted provider alias asserts the budget. Changelog entry.

---

## R-BUG-02 — A corrupt price file is overwritten with one entry

**Severity** P1 · **Wave** W5 (`fix:` commit) · **Status** **Fixed** (2026-09-25).
A missing file is created; an unreadable file, or one that is not a JSON
object, is left byte-for-byte with a warning naming it.

**Claim.** `refresh_price_cache` treats an unreadable price file as empty, then
atomically writes back only the newly fetched model.

**Evidence.**
- `core/pricing.py:122-126`: `_read_json` returns `{}` on any exception.
- `:160-176`: `prices[model] = …` followed by `_write_json_atomic(price_path, prices)`.
- The docstring (`:143-148`) promises that it "merges into the file rather than
  overwriting it, so a hand-authored entry is preserved".

**Consequence.** One malformed character in `model_prices.json` and the next
start-up permanently deletes every hand-authored price.

**Fix.** Distinguish a missing file from an unreadable one. On an unreadable
file, log a warning and do not write.

**Verification.** A test with a corrupt file asserts the file is unchanged and a
warning is logged.

---

## R-BUG-03 — The plain renderer drops visuals

**Severity** P2 · **Wave** W4/W5 (`fix:` commit) · **Status** Fixed (W4, `cli/render/plain.py`)

**Found by the parity test rather than by looking.** `R-SOL-01` asks for a test
that every renderer handles every event kind; writing it turned this from a
noted divergence into a failing assertion, which is why the fix came forward
from W5 — leaving it open would have meant committing a red suite.

**Fix.** The plain renderer names the visual the way it already names an
artifact: `◨ visual: <title>`, through `tool_format.sanitize` so a crafted title
cannot write escape sequences into the transcript. Named rather than inlined —
a pipe cannot show an image.

**Verification.** Live, piped (which is what selects this renderer — `is_tty`
is false): asking the agent for a chart now prints `◨ visual: Tiny Bar Chart`
where previously nothing appeared at all.

**Evidence.** `cli/render/renderer.py:300` handles `ev.kind == "image"`;
`cli/render/plain.py:149-239` has no such branch, so the event is silently ignored.

**Consequence.** With `--classic`, a pipe or a dumb terminal, the user is never
told a chart or diagram was produced.

**Fix.** Handle the event; add the "every event kind is handled by every
renderer" test from `R-SOL-02`.

---

## R-BUG-04 — The remote-attach client defaults to the wrong port

**Severity** P2 · **Wave** W5 · **Status** **Fixed** (2026-09-25).
`config/network.py` holds `DEFAULT_DAEMON_PORT`/`DEFAULT_DAEMON_URL`, used by
the daemon config, the attach fallback and the Settings catalog; the client has
no default.

**Evidence.** `cli/remote_attach.py:46` defaults to `http://127.0.0.1:8765`; the
daemon's default is 7654 (`cli/commands/attach.py:49`). Both callers pass
`base_url`, so the default is dead today, and wrong the moment anyone relies on it.

**Fix.** One shared constant; no default in the client.

---

## R-BUG-05 — `/clear` may paint an escape sequence into the dashboard

**Severity** P2 · **Wave** W5 · **Status** **Fixed** (2026-09-25). It
reproduced: the escape landed in the pane, and the transcript stayed. `/clear`
now calls the dashboard's `clear_transcript`, and in the rich and plain REPLs
the terminal clear.

**Evidence.** `cli/commands/chat_repl.py:815` prints `"\033[2J\033[H"` to
`sys.stderr`. In dashboard mode `sys.stderr` is the transcript buffer.

**Fix.** Route `/clear` through the renderer (the dashboard clears its buffer).
**Verification.** A dashboard test that `/clear` leaves no escape bytes in the
buffer — written first, and expected to fail.

---

## R-BUG-06 — One-shot turns skip repair and empty-turn recovery

**Severity** P2 · **Wave** W5 (`fix:` commit) · **Status** Open

**Evidence.** `R-PLC-06`: `sessions/runner.py` drives one-shot turns without the
pre-turn repair of interrupted tool calls, and without the empty-turn retry, that
`ConversationService` applies to the REPL and the daemon.

**Consequence.** A one-shot run that follows an interrupted session can fail, or
end in silence, where the REPL would recover.

**Fix.** Run one-shot turns through `ConversationService`; add a test with an
interrupted checkpoint.

---

## R-BUG-07 — A daemon started from the app serves no API the app can reach

**Severity** P1 · **Wave** W5 (`fix:` commit) · **Status** Open (found during W2)

**Evidence.**
- `electron-app/src/main/daemon.js:186` spawns `yuyutsava daemon --no-ui`.
  `--no-ui` sets `headless`, and a headless daemon builds no web hub, so it also
  builds no HTTP/WebSocket server (`daemon/bootstrap.py:1238`; the server is
  built only when `web_hub` exists).
- Observed on 2026-09-15: started exactly that way, the daemon logged "ready",
  and `--status` advertised `http://127.0.0.1:7654/`, but nothing listened on
  port 7654.
- `daemon.js` has passed the flag since `d1a33e6` (2026-05-14).
- Callers: Settings → Start/Restart Daemon, the tray menu, and the launch prompt
  shown when no daemon is running (`index.js:178`).

**Consequence.** A daemon the app starts or restarts serves nothing the app can
connect to. Starting it from a terminal (`yuyutsava daemon`, which opens the app
itself) works, and that hides the problem. `daemon.json` also advertises a URL
nothing serves.

**Fix.** Separate "don't open a window" from "no API": a flag that keeps the API
but skips launching Electron, used by `daemon.js`. Publish no `web_url` when no
server runs.

**Verification.** Start the daemon the way `daemon.js` does and get `200` from
`/health`, then drive it from the Electron app.

---

## R-BUG-08 — An answered inline ask leaves the turn silent and the socket waiting

**Severity** P1 · **Wave** W5 (`fix:` commit) · **Status** Fixed (W4, `converse.py:896`)

**Evidence.** Measured over `/ws/converse` with a stub conversation manager and
no `channels` wired — the path the code itself calls the fallback "for tests /
embedded use":

```
hello → turn_start → token → ask{body: "proceed?"}
     ← {"type": "ask_response", "text": "yes go"}
ask_resolved → (nothing further; event loop idle)
```

**Cause.** `_ask` and the `ask_response` handler chose their mechanism from two
different variables:

| | selects on | when absent |
|---|---|---|
| `_ask` (`:506`) | `channels` | parks on a run-local `asyncio.Future` (`run.pending_ask`) |
| handler (`:900`) | `decisions` | falls through to `registry.answer_ask` |

`create_app` **always** ends up with a `DecisionService` — it builds a hub-local
one when the caller passes none (`web/app.py:172-178`) — while `channels` passes
through as given. So `channels=None, decisions=<live>` is the *default* shape for
any embedder, and in it the two disagree: the turn waits on the run-local future
while the handler answers a hub that never received the ask. `respond_ask` then
raises `DecisionConflictError` for the unknown id, whose handler emits the
`ask_resolved` seen above. The future is never resolved, so `_ask` never returns
and `TurnRegistry._drive` never reaches `turn_end`.

That also makes `registry.answer_ask` at `:909` — and its comment "No hub (tests
/ embedded): the run-local future is the path" — unreachable.

**Scope.** This cause is confined to callers of `create_app`/`make_app` that omit
`channels` (it defaults to `None` on `AppDeps`) — every test and embedded use.
The shipped daemon passes both (`bootstrap.py:1576`) and so escapes *this* cause,
but it is **not** unaffected: it hits a second, independent cause recorded as
`R-BUG-09`, measured live against the running daemon.

An earlier revision of this entry claimed the shipped daemon was unaffected. That
claim was made from reading the wiring, not from running it, and it was wrong.

**Correcting the earlier entry.** Three mechanisms had been recorded as refuted.
Two genuinely were (the emit-before-future window — there is no `await` between
the emit and the assignment, so the task cannot be interrupted there; and plain
test staleness). The third was refuted *wrongly*: it was checked as
"`channels` set with `decisions` unset", on the stated premise that "both default
to `None`". `decisions` never defaults to `None`, and the real split is the
opposite direction. The inference that "the turn task is gone rather than busy"
was also wrong — it was parked on the future the whole time. `ask_resolved` had
two emitters (`_ask`'s `finally` at `:524` and the conflict handler at `:906`),
so its arrival never proved `_ask` had returned, which is what made the earlier
evidence read as a contradiction.

**Fix.** Answer whichever mechanism is actually waiting. `pending_ask` is set
only by the run-local branch, so `run.answer_ask(text)` returning `True`
identifies that branch exactly; anything else falls through to the hub. With
`channels` wired, `pending_ask` is always `None`, so the hub path is unchanged.

**Verification.** `test/web/test_converse_frames.py::test_an_answered_ask_resumes_the_turn`
drives the round-trip and asserts `ask_resolved`, `final`, `turn_end` and that
the agent resumed with the answer text. Negative-controlled: with the fix
reverted it fails (on a 15 s deadline) rather than hanging the suite. The full
suite's failure set is byte-identical before and after.


## R-BUG-09 — A promptly answered permission prompt hangs the turn (race)

**Severity** P0 · **Wave** W5 (`fix:` commit) · **Status** Fixed (W4, `aio/waiters.py`)

**Evidence.** Measured against the **live daemon** (real Vertex turn, channels and
DecisionService wired — the shipped configuration), asking it to delete a
workspace file, which the zone policy gates `asks user`:

| answer timing | outcome |
|---|---|
| immediately on receiving the `ask` frame | `ask_resolved`, then silence; turn parked 369 s until answered over HTTP |
| after a 4 s pause | `ask` → `ask_resolved` → `tool_result` → … → `final` → `turn_end` |

Same prompt, same daemon, same answer (`approve`); only the delay differs. While
parked, `GET /asks` still listed the ask as pending, and `POST /ask/{id}/respond`
resolved it and resumed the turn (`turn … done … (369.3s)`), proving the waiter
map and the ask id were both correct.

**Cause.** `_ask` publishes the ask before anything is waiting for the answer:

```
run.emit({"type": "ask", ...})        # the client may answer from here on
await channels.post_ask(ask)
  └─ await ask_registry.record(a)     # a DB write — the window is wide
  └─ _race_ask -> asyncio.create_task(WebChannel.post_ask(a))
       └─ hub.pending_asks[ask_id] = fut   # the waiter finally exists
```

An answer arriving in that window finds no waiter: `DecisionService.respond_ask`
falls through `_find`, finds no `_ask_resume` entry either, and raises
`DecisionConflictError`. The router treats that as "answered elsewhere first" and
emits `ask_resolved` — so every surface clears the card while the turn is still
blocked on a future that will now never be resolved by anyone.

**Consequence.** Any surface that answers quickly loses the turn: the Electron
app's `approve` button, a remembered/auto-approval, or a scripted client. The
user sees the prompt vanish and the agent go silent forever, with no error. The
slower the ask registry write, the wider the window.

**Why it was missed.** The inline-ask path had no executing test (`R-TST-11`), and
the `ask_resolved` frame has two emitters — `_ask`'s `finally` and this conflict
handler — so its arrival reads as success on the wire.

**Fix.** Reordering cannot close this — a slow registry write would always lose
to a fast click — so the answer is held instead of refused. `hub.pending_asks`
and the InboundSink's map become a `WaiterMap` (`yuyutsava/aio/waiters.py`, L0
and stdlib-only so both the daemon and `channels/` can import it downward): a
`dict` subclass whose `__setitem__` delivers an answer that arrived early.
`DecisionService.respond_ask` holds such an answer rather than raising.

Holding is **opt-in** (`hold_if_unwaited=`), and only the conversation socket
that raised the ask passes it — it still holds the id in `run.pending_ask_id`.
Without that knowledge an unregistered id cannot be told from a bogus one, and a
bogus id must keep conflicting rather than report a false success; the existing
`409` contract on `POST /ask/{id}/respond` is unchanged. A `WaiterMap` also
refuses to hold for an id whose waiter has already been and gone, so a genuinely
finished prompt still conflicts.

**Verification.** Live, against the running daemon, same prompt and answer:

| | before | after |
|---|---|---|
| answered immediately | parked 369.3 s | `turn … done … (20.0s)`, file deleted |

Plus `test/aio/test_waiters.py` (6 tests: delivery, no-hold-once-seen, TTL,
bounding, dict behaviour) and two `test/web/test_decision_service.py` cases. The
full-suite failure set is byte-identical to the pre-fix baseline.


## R-BUG-10 — A style rename to snake_case took every MCP server offline

**Severity** P0 · **Wave** W4 · **Status** Fixed (`mcp/tool_adapter.py`)

**Evidence.** Daemon startup, every run:

```
W yuyutsava.mcp.loader: MCP server 'google-drive' exposed 128 tools; capping at max_tools=32
E yuyutsava.mcp.loader: MCP server 'google-drive' failed to start:
    'Tool' object has no attribute 'input_schema'
E yuyutsava.mcp.loader: MCP server 'apify-makemytrip' failed to start: (same)
```

**Cause.** `51e85c3` ("fix(mcp): fix camelCase attributes in tool adapter to
snake_case") renamed `mcp_tool.inputSchema` to `mcp_tool.input_schema` and
`result.isError` to `result.is_error`. Both are **MCP wire-protocol field
names**, and the SDK's pydantic models carry the protocol's spelling verbatim —
`mcp.types.Tool.model_fields` is `[…, 'inputSchema', 'outputSchema', …]` and
`CallToolResult` has `isError`. The codebase being snake_case everywhere else
makes the rename look like a tidy-up; it is actually an API change.

`isError` survived only because that line kept a `getattr` fallback.
`inputSchema` had none, so `adapt()` raised on the first tool of every server.

**Consequence.** All MCP tooling was gone — 38 tools across two servers. The
loader catches per-server startup failures and continues, so the daemon reports
itself ready, the UI works, and chat works; only the MCP tools are missing, with
the reason buried in a startup traceback. Nothing in the suite touched it: the
adapter had **no tests at all**.

**Fix.** Restore the protocol spellings, and say in the docstring why they are
camelCase so the next tidy-up does not repeat it.

**Verification.** `test/mcp_layer/test_tool_adapter.py` (5 tests) drives `adapt`
and `_flatten_content` with real `mcp.types.Tool` / `CallToolResult` objects
rather than stubs — a stub would grow whatever attribute the adapter asked for,
which is exactly the bug. Negative-controlled: reinstating the rename fails two
of them with the production `AttributeError`. Live: after the fix the daemon
logs `google-drive started — 32 tool(s)` and `apify-makemytrip started — 6
tool(s)`.


## R-BUG-11 — A whitespace-only transcript ran a turn

**Severity** P2 · **Wave** W4 · **Status** Fixed (`services/converse_session.py`)

**Evidence.** The two mic paths tested a transcript for emptiness differently.
The talk-over path stripped first:

```python
text = (result.text or "").strip()
if not text:  # noise/echo while speaking → ignored
```

while the utterance-to-turn path did not:

```python
text = result.text
if not text:  # → turn_end, no turn
```

So a transcript of `"   "` was silence on one path and a request on the other.

**Consequence.** A blank turn: `transcript` echoed as whitespace, a
`voice_messages` row persisted with no text, and the agent handed a prompt of
spaces to answer. Small, but it costs a model call and puts an empty user turn
in the replayable history.

**Why it survived.** The rule was written out twice, ~100 lines apart, inside a
771-line function — the shape `R-RDB-01` describes. Extracting `UtteranceRouter`
put both paths on one gate, which is what made the divergence visible at all.

**Fix.** Both paths test `Heard.words` (the stripped transcript), and the turn
path uses it for the echo, the persisted row and the turn text.

**Verification.** `test_a_whitespace_only_transcript_is_silence` — a `"   "`
transcript yields `turn_end` and no turn. The refactor that exposed it was
committed separately, with the divergence deliberately preserved, so this
behaviour change stands on its own.


## R-BUG-12 — `/help` hid the commands for answering a background ask

**Severity** P2 · **Wave** W4 · **Status** Fixed (`cli/commands/chat_repl.py`)

**Evidence.** The chat REPL declared its commands in two hand-kept places — the
completer's `_SLASH_COMMANDS` dict and the body of `_print_help` — and they had
drifted:

```
offered by tab-complete but absent from /help: /approve  /asks  /reject  /reply
in /help but not tab-completed              : (none)
```

**Consequence.** Those four are how a user answers a pending approval from a
background subagent. Undocumented, someone with a blocked background task has no
way to discover them short of pressing Tab and guessing — and the one moment
they are needed is the moment the agent has stopped and is waiting.

**Cause.** Two lists and no check between them, plus five dispatch styles
(`_handle_ask_command` and `_handle_settings_command` returning `bool`,
`_handle_slash` returning sentinels, and `/usage` / `/context` / `/skills`
compared inline in the loop). Adding a command meant touching up to four places,
and nothing noticed when one was missed.

**Fix.** One `SLASH_COMMANDS` registry of `SlashCommand(name, help, usage,
aliases, group)`. `/help` and the completer are both generated from it, so they
cannot disagree. Dispatch is unchanged for now — unifying the five styles is the
`ReplLoop` work in `R-SOL-02`.

**Verification.** `test/cli/test_slash_commands.py` (8 tests, 40 subtests) pins
the surface and the invariants: help lists exactly what completion offers, the
four approval commands are documented, usage hints (`/reply <id> <text>`) reach
the help, and **every advertised command is actually dispatched** — a command in
the list that nothing handles answers "unknown command", which is worse than not
offering it because the user believes it. Negative-controlled with a phantom
command, caught by both the pinned surface and the handler invariant. Confirmed
in a live REPL: `/help` renders all seventeen.

One false lead worth recording: a first pass compared the two lists with a loose
regex and reported `/results` as advertised-but-missing. It is not a command —
the string comes from the phrase "tool calls/results" inside `/ring`'s
description. The test matches the first token of a help line for that reason.

---

## R-BUG-13 — Every CLI turn ends by printing two tracebacks it has already handled

**Severity** P2 · **Wave** W5 · **Status** Open

**Evidence.** Three consecutive one-shot turns, each finishing correctly with
exit status 0, each ending with this on stderr:

```
httpcore.ReadError
  transcript_index.py:184 in _index_items -> embedder.embed(texts, mode="document")
RuntimeError: PgPool.open() must be called first
  transcript_index.py:194 in _index_items -> self._pool.connection()
```

**Consequence.** Both are `logger.warning(..., exc_info=True)` on paths that
degrade as designed — the first stores NULL embeddings, the second gives up and
returns 0 — so nothing is broken. But a user reading a terminal cannot tell a
handled warning from a crash, and these arrive immediately after "task
complete", which is exactly where a real failure would appear. The cost of the
noise is that it trains people to ignore tracebacks.

**Cause.** Two separate things, and only the second is the bug.

1. The embedder's endpoint is unreachable in this environment, which is also
   why `test/memory/test_embedder.py` is red in the standing baseline. Not a
   code defect.
2. Transcript indexing is still running when the PG pool closes. `_spawn`
   (`transcript_index.py:114`) creates the task, adds it to `self._tasks` and
   drops it on completion — and nothing else in the module touches that set:
   there is no drain, `gather` or `close`. So whether the write lands before
   teardown is a race with nothing arbitrating it. A turn whose embedder
   answers quickly will usually win it, which is why this is visible here and
   not everywhere: an unreachable embedder has to time out first, and then the
   write always loses. This is the teardown-ordering defect.

**Fix.** Await or cancel the outstanding index work before closing the pool. The
containment itself is right and should stay — indexing is an enhancement, never
a gate, exactly as `NoteService._schedule_index` documents for the board's own
index. Once the ordering is fixed, an embedder that is merely unreachable should
log one line, not a traceback, since the handler already knows what it will do
about it.

**Verification.** A one-shot turn with no embedder configured ends with no
traceback on stderr, and the transcript chunks for that thread are either
written with NULL embeddings or absent — never half-written. Negative control:
force the embed to fail and confirm one warning line and one NULL-row insert.

**Found by** running three real agent turns while verifying the `TodoExchange`
split. The static gates are green and the suite is at baseline; nothing in the
test tree exercises process teardown with a live pool.

---

## R-BUG-14 — An explicitly chosen Postgres that is down produces a raw traceback

**Severity** P2 · **Wave** W5 · **Status** Open

**Evidence.** With `YUYUTSAVA_STORAGE_BACKEND=postgres` and the database
container stopped, `yuyutsava --list-sessions` ends in:

```
psycopg_pool.PoolTimeout: pool initialization incomplete after 10.0 sec
```

— a full stack trace, after a ten-second wait, with nothing naming the host,
the port, or what to do.

**Not a regression.** Found while auditing W4 against the plan's per-wave
checks, and first mistaken for one: HEAD crashed in the main checkout while
the end-of-W3 commit ran clean in a separate worktree. The difference was the
worktree, not the code — `.env` is gitignored, so a fresh worktree has no
`YUYUTSAVA_STORAGE_BACKEND` and falls back to SQLite. Given the same `.env`,
the end-of-W3 code crashes identically, and a bisect run without it reports a
docs-only commit as "first bad", which is how the environment dependence showed
up.

**Consequence.** Choosing Postgres explicitly is a statement that there is
nothing to fall back to, so failing is right. Failing *like this* is not: a
user sees a psycopg internal, and the only clue is buried in the last line.

**Fix.** Catch the pool timeout at the CLI boundary and say which database was
unreachable and that it was selected explicitly — the same shape the daemon
already uses for its storage fallback reason.

**Verification.** `--list-sessions` with the explicit backend and the database
down exits non-zero with one line naming the DSN host and port, and no
traceback.

---

## R-BUG-15 — A background task that finishes mid-turn wakes the orchestrator onto a thread still being written

**Severity** P1 · **Wave** W5 · **Status** Open

**Evidence.** Found while verifying the `build_async_subagents` split live
(2026-09-25). A chat turn starts a background task on `general-purpose`
(`start_async_task`); when the task finishes, the watcher's completion sink
enqueues a `subagent_completed` `OrchestratorTask` on the chat's own thread,
and the orchestrator loop runs it there. On the main daemon, twice out of
three:

```
orchestrator task failed: … INVALID_ARGUMENT
details = "Requests ending with a model turn are not supported."
```

In both failures the wake-up started **while that chat's turn was still
running** (turn `e847777d3bd1` ran 01:26:49–01:27:22; the orchestrator failed
at 01:27:13). The run that succeeded had finished its turn first. Four runs
on isolated daemons — two on the code before this wave's orchestrator and
streaming changes, two after — all happened not to overlap and all succeeded.
The completion sink moved verbatim, so this is the timing, not the refactor.

**Consequence.** Two graphs write one checkpoint thread at once. The
conversation manager's per-thread turn gate (`TurnRegistry`) exists for
exactly this — "a LangGraph checkpoint is single-writer per thread" — but the
orchestrator loop does not go through it. With `gemini-3.8-flash` the
interleaved history is rejected outright, so the user is never told the
background task finished; other providers may accept it and produce
duplicated or out-of-order turns instead.

**Fix.** Route a `subagent_completed` wake-up for a conversation thread
through that thread's turn gate: queue it behind the running turn (or deliver
it as the conversation's next turn) instead of running a second graph on the
thread concurrently.

**Verification.** A background task that completes while its launching turn
is still streaming is reported after that turn ends, every time — a test with
a gated fake turn, and live with a task shorter than the turn.

## R-BUG-16 — Runtime settings write into the package's own `events_config.json`

**Severity** P1 · **Wave** W5 · **Status** Fixed. The owner chose the per-user
copy on 2026-09-25.

**Evidence.** Found live on 2026-09-25 while checking W5's settings handler.
`storage/paths.events_config_path()` resolves to
`yuyutsava/events/events_config.json`, next to the source, and its docstring
calls the file "a project artifact, not user runtime state". But it is
written at runtime:

- `PATCH /settings/runtime` with `voice.wake_enabled` flips
  `sources.voice.enabled` in it (`routers/settings.py`, by design, so the hot
  reload releases the microphone);
- the `/config` endpoints (watched directories and source settings) save it
  through `ConfigService`.

A scratch daemon with its own `YUYUTSAVA_HOME` toggled wake on and off and
left the tracked file changed (`"enabled": false` → `true`) in the working
tree.

**Consequence.** Every toggle in the app dirties the git tree, and a commit
can carry one user's microphone setting into everyone's default. All daemons
on a machine share one file whatever their home, so an isolated test is not
isolated. A non-editable install writes into site-packages, or fails where
that is read-only.

**Fix.** Keep the file in the package as the shipped default, and read and
write a per-user copy under `state_dir()` (created from the default on first
write), the way `channels_config_path()` already does with
`YUYUTSAVA_CHANNELS_CONFIG`. Existing users' toggles live in the repo copy
today, so on first start the user copy is seeded from the current repo file
rather than from the default.

**Verification.** A settings patch under a scratch `YUYUTSAVA_HOME` leaves
`git status` clean, and a second home does not see it; the hot reload still
tears the voice source down.

## R-BUG-17 — The first chat in the daemon replaced its consent registry

**Severity** P1 · **Wave** W5 · **Status** Fixed (with `R-GLB-01`'s task-runner
half)

**Evidence.** Found while mapping `set_default_*` call sites on 2026-09-25.
`build_agent_stack`, which the daemon runs for every chat and voice bundle,
called `set_default_consent(ConsentRegistry())`: a fresh registry with no
store, installed as the process default and written into every cached
`TaskRunnerAgent`, the orchestrator's subagents included. Confirmed live on
the code before the fix. A chat turn reading a file outside the workspace,
answered "allow for this project", left **0** rows in `consent_grants`. On
the fixed code the same turn leaves the project grant.

**Consequence.** From the first chat of a daemon's life, project grants were
no longer persisted, and the grants loaded from the database at boot were no
longer consulted. A user who had allowed something for the project was asked
again after a restart, and every surface was asked again after a chat.

**Fix.** The task-runner tools bind to an explicit `TaskRunnerAgent`
(`bind_tools(agent)`); the per-workspace cache and both setters are gone.
Each stack builds its own gateway: the daemon passes its policy and consent
through `StackWiring`, and a standalone CLI stack has its own registry.

## R-BUG-18 — The first chat in the daemon replaced its board, visual and note stores

**Severity** P1 · **Wave** W5 · **Status** **Fixed**: the board and note index
come through an injected `TodoExchange`, and the visual store is handed over too.

**Evidence.** The same pattern as `R-BUG-17`. Confirmed in-process on
2026-09-25: `build_daemon` against a scratch database, then one
`ConversationManager._build_master_bundle()`, the chat bundle every
conversation uses:

| default | after boot | after the first chat bundle |
|---|---|---|
| todo store | `RoutedStore` (Postgres, SQLite spillover) | `UnifiedTodoStore` (plain Postgres) |
| visual store | `RoutedStore` | `UnifiedVisualStore` |
| note index | the boot index (configured `min_score`) | a new `TodoNoteIndex` |

`agent_stack._install_board_stores` sets all three process defaults on every
stack build, and the daemon runs that stack for chat and voice.

**Consequence.** After the first chat, the daemon's board API, the todo_*
tools and the vis_* tools write straight to Postgres on the chat stack's own
pool. A Postgres outage then fails those writes instead of buffering them to
SQLite for the reconciler to drain. Note recall loses the configured
`min_score`.

**Fix.** Build one `TodoExchange` (store and note index) and one visual store
per process: the daemon's at boot, and the standalone CLI's in its stack.
Pass them explicitly to the web routers (`AppDeps`), the conversation manager,
the stacks (`StackWiring`) and the tools. The `set_default_*` accessors for
all three go.

## R-BUG-19 — A CLI session delete on Postgres left the session's feedback behind

**Severity** P1 · **Wave** W5 · **Status** Fixed (with `R-GLB-01`)

**Evidence.** Confirmed live on 2026-09-25. A session with one feedback row in
the scratch Postgres database, deleted with `yuyutsava --delete-session`: the
session was gone, and `message_feedback` still held its row. A CLI process
never set the feedback global, so `purge_session` fell back to the SQLite twin
and deleted from a file the row was never in. The row stores the user's and
the assistant's text verbatim, which is exactly what the user asked to delete.
The visual store had the same fallback. Its row went through the Postgres
cascade, but the image file would have stayed on disk.

**Fix.** `AppContext` has no fallbacks. `purge_session` takes one, and a CLI
command opens the configured backend's stores with `open_app_context()`: one
pool, migrations applied, and every store on it. Live on the fixed code, the
same delete removes the feedback row, the visual row and the image file.
