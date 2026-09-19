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

**Severity** P1 · **Wave** W5 · **Status** Open (legacy `F-S08`, partly closed)

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

**Severity** P2 · **Wave** W5 · **Status** Open

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

**Severity** P1 · **Wave** W5 (`fix:` commit) · **Status** Open

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

**Severity** P1 · **Wave** W5 (`fix:` commit) · **Status** Open

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

**Severity** P2 · **Wave** W4/W5 (`fix:` commit) · **Status** Open

**Evidence.** `cli/render/renderer.py:300` handles `ev.kind == "image"`;
`cli/render/plain.py:149-239` has no such branch, so the event is silently ignored.

**Consequence.** With `--classic`, a pipe or a dumb terminal, the user is never
told a chart or diagram was produced.

**Fix.** Handle the event; add the "every event kind is handled by every
renderer" test from `R-SOL-02`.

---

## R-BUG-04 — The remote-attach client defaults to the wrong port

**Severity** P2 · **Wave** W5 · **Status** Open

**Evidence.** `cli/remote_attach.py:46` defaults to `http://127.0.0.1:8765`; the
daemon's default is 7654 (`cli/commands/attach.py:49`). Both callers pass
`base_url`, so the default is dead today, and wrong the moment anyone relies on it.

**Fix.** One shared constant; no default in the client.

---

## R-BUG-05 — `/clear` may paint an escape sequence into the dashboard

**Severity** P2 · **Wave** W5 · **Status** Open (suspected — reproduce first)

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
`channels` (`web/server.py` defaults it to `None`) — every test and embedded use.
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

**Severity** P0 · **Wave** W5 (`fix:` commit) · **Status** Open (found during W4)

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

**Fix.** The waiter must exist before the ask is visible. The registration is
currently several `await`s downstream of the emit, so either the ask frame moves
behind registration, or the inline surface gets a future created synchronously in
`_ask` (before the emit) and raced against `channels.post_ask`, first answer
winning — the router already owns `run.pending_ask` for exactly this shape.

**Verification.** The live delete-with-immediate-answer completes, and a
regression test drives an answer that lands before the hub registers its waiter.
