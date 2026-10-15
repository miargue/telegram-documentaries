# Plan — ADK Orchestration (Phase 8)

Red/Green TDD: write failing tests first, then code, then `scripts/test` until
green. Run `scripts/hooks` at the end of each task group. No test touches
Telegram or Gemini.

Modified: `bouncer.py`, `requirements.txt`, `README.md`, `SPECS/*` (new
`SPECS/ARCHITECTURE.md`, update MISSION/TECH/ROADMAP/README). New:
`telegram_documentaries/adk_orchestration.py`.

## 1. Install dependency + spike (TG1)

Red: none (spike). 
Green:
- Add `google-adk==2.12.0` to `requirements.txt` (runtime dep). Keep
  `google-genai` implicit via transitive; do not pin unless necessary.
- In a quick spike, verify ADK imports and can create a minimal custom `BaseAgent`
  + `Runner` + `InMemorySessionService` with state passing works in the
  environment (offline). Document findings in the implementation notes.
- Run `scripts/test` + `scripts/hooks` (no new code/tests yet).

## 2. Define ADK agent wrappers (TG2) — Red/Green

Red (`tests/unit/test_adk_orchestration.py`, new):
- `BouncerAgent`, `InterviewerAgent`, `ConverterAgent`, `NarratorAgent` subclass
  `BaseAgent`.
- Each agent takes injected dependencies (ports/callables) in `__init__`.
- When run via `Runner.run_async` with appropriate state, each agent delegates to
  the corresponding existing stage function/port (verify by injecting fakes and
  asserting the fake was called). Since delegation uses existing code paths, this
  is thin.
- Agents do not make network calls when given fakes.

Green (`telegram_documentaries/adk_orchestration.py`):
- Implement the 4 custom BaseAgents. For each, `_run_async_impl`:
  - reads `ctx.session.state` for `chat_id`/`update` (reconstruct `Update` from
    dumped JSON if needed).
  - calls the existing stage logic via injected callables (e.g. `handle_conversion`,
    `handle_scripting`, `handle_narrating`, interview entrypoints). Or call the
    port methods directly.
- Keep logic minimal: agents wrap the existing code paths; do not re-implement
  Gemini/transport logic.

Run `scripts/test` + `scripts/hooks`.

## 3. Root orchestrator + routing parity (TG3)

Red (`tests/unit/test_adk_orchestration.py`):
- `DocumentaryOrchestrator` is a `BaseAgent` and has sub-agents (the 4).
- Given an update in each phase (`awaiting_photo`, `gate_passed`,
  `interviewing`, `done`), the orchestrator produces the same routing outcome as
  `bouncer._route_after_gate` (and the pre-routing: messageless skip,
  start-command handling conceptually - start command is handled by bouncer's
  pre-check; but the orchestrator must coordinate).
- The result is written to `ctx.session.state["result"]`.

Green:
- Implement `DocumentaryOrchestrator`. Its `_run_async_impl` mirrors the routing:
  - handle messageless → SKIPPED
  - command check (orchestrator can check the command from update; bouncer may
    still do the high-level command reset — see TG5)
  - album skip
  - phase routing: awaiting_photo → BouncerAgent path; else → InterviewerAgent
    then post-handoff (conversion/scripting/narration) as in
    `_route_after_gate` (folding scripting into NarratorAgent means NarratorAgent
    does script+audio; the orchestrator invokes NarratorAgent when appropriate).
- Ensure the orchestrator produces the same `bool | Skipped` results as current
  `bouncer.handle_update` for the same inputs (behaviour parity).

Run `scripts/test` + `scripts/hooks`.

## 4. AdkPipeline driver (TG4)

Red (`tests/unit/test_adk_pipeline.py`, new):
- `AdkPipeline` creates Runner + InMemorySessionService + orchestrator once.
- `handle(update, chat_id, ..., log)` returns the same type as before.
- Given fakes, it produces identical results to direct `bouncer.handle_update`
  for representative cases (start command mid-interview, photo gate, interview
  turn, done handoff).
- Uses `asyncio.run` internally; drains the async generator.
- Sessions are created/reused per `chat_id`.

Green (`adk_orchestration.py`):
- `AdkPipeline` class as specified. Build the full agent tree with injected deps.
- `handle()` does: ensure session exists; call `runner.run_async` with
  `state_delta={"chat_id": chat_id, "update": update.model_dump(mode="json")}`,
  `user_id=str(chat_id)`, `session_id=str(chat_id)`; drain; read `result` from
  session state (or from the final yielded event); return as `bool | Skipped`.
- Handle the case where result might be missing (default to False) but should
  match the expected behaviour.

Run `scripts/test` + `scripts/hooks`.

## 5. Wire bouncer to use AdkPipeline (TG5)

Red: existing tests in `tests/unit/test_bouncer.py` and
`tests/component/test_full_gateway_flow.py` will still pass (behaviour
preserving). Add a unit test asserting `bouncer.handle_update` uses the ADK
path? Optional; not required. The key is behaviour parity.
Green:
- Modify `bouncer.py`:
  - Import `AdkPipeline` from `telegram_documentaries.adk_orchestration`.
  - In `handle_update`, construct an `AdkPipeline` (or a module-level singleton
    built from the same deps passed in). Since `handle_update` takes all deps
    (`session`, `transport`, `gate`, `llm`, `converter`, `scripter`, `narrator`,
    `log`) as parameters, we can construct `AdkPipeline` from them. 
- The pre-routing (messageless, start command, album skip) is currently done
  before `_route_after_gate`. The orchestrator can also do it. To minimise
  behavioural changes and keep parity, delegate the whole update to the
  orchestrator (which will handle all cases identically). So the modified
  `handle_update` becomes: build pipeline from deps and return
  `pipeline.handle(update, chat_id=..., session=..., transport=..., gate=..., llm=..., converter=..., scripter=..., narrator=..., log=log)`.
  But also we need to handle the exact same early returns (e.g. messageless
  returns SKIPPED). The orchestrator will do that - so yes.
- Also ensure start command handling is identical (the orchestrator must call
  `reset_chat` via the same logic). The existing `reset_chat` is still
  available; orchestrator can call it.
- Keep `reset_chat`, `_prompt`, `_send`, etc. as they are (they're used by
  stage code too).

Run `scripts/test` + `scripts/hooks` — all 802 existing tests must still pass.

## 6. Architecture diagrams + docs (TG6)

Green:
- Create `SPECS/ARCHITECTURE.md` with Mermaid diagrams:
  - System overview (Telegram → polling → bouncer → ADK orchestrator → 4 agents →
    existing ports → Gemini/Telegram)
  - ADK agent graph (DocumentaryOrchestrator with sub-agents)
  - Sequence diagram for generation handoff (interview complete → converter →
    narrator-with-scripting)
- Update `SPECS/TECH.md`: add "Google ADK Orchestration (Phase 8)" section
  describing the thin layer, custom BaseAgents wrapping existing ports, 4-agent
  mapping (Narrator folds scripting), Runner-based live orchestration, state
  contract (ADK state carries inputs only; SessionStore is canonical), async
  via `asyncio.run` per update, dependency on google-adk.
- Update `SPECS/MISSION.md`: note ADK orchestration is now implemented (not
  deferred).
- Update `SPECS/ROADMAP.md`: mark Phase 8 complete after verification.
- Update `README.md`: mention Google ADK orchestration in architecture/stack
  (genuine ADK layer, custom BaseAgents over typed ports; keep existing
  description accurate).
- Run `scripts/test` + `scripts/hooks`.

## 7. Verification (TG7)

Green (verifier steps):
- `scripts/test` — all tests pass (802+ baseline; no new failures).
- `scripts/hooks` — green (ruff, mypy on new module + changes).
- Full suite with sockets patched to raise — still green.
- Manual smoke: verify bot imports and can start with ADK installed (google-adk
  import succeeds; AdkPipeline builds).
- Docs reconciled (all files updated). Category 1 rubric items addressed.
- Live E2E sanity (user will test) — behaviour preserved.

## Verification notes (filled at verification)

- Phase 8 delivered as specified: 4 custom BaseAgents + DocumentaryOrchestrator +
  AdkPipeline; bouncer delegates to ADK live path; SessionStore remains
  canonical; Mermaidor diagrams added; google-adk added to requirements.txt.
- All existing tests pass; new module typechecks (mypy); ruff clean.
- No test touches network; offline suite green.