# Requirements — ADK Orchestration (Phase 8)

## Context

- **Phase 7 (Resilience) is merged to `main`** (PR #8, merge commit `cb337a9`):
  `scripts/test` = 802 passed, `scripts/hooks` green, no test touches the network.
- **The pipeline works** (live Telegram E2E verified): portrait → interview
  (5 turns) → hybrid portrait → narration script (81 words) → MP3 audio message.
- **Teacher rubric** (6 categories, 100 points): **Category 1 (Architectural
  Blueprint, 15 pts)** requires: complete `/SPECS` (MISSION/TECH/ROADMAP)
  documenting **all 4 agents and Google ADK orchestration**; points lost if specs
  incomplete, diagrams missing, or agent definitions partial. The codebase
  currently ships with a typed-port (Gemini REST) implementation and **Google
  ADK is deferred** in `SPECS/TECH.md`.
- **ADK decision (user-approved 2026-10-15):** add a **thin, genuine Google ADK
  orchestration layer + Mermaid architecture diagrams**. Express the orchestration
  as real ADK agents wrapping the existing tested ports (Bouncer/Interviewer/
  Converter/Scripter/Narrator), keep the working long-poll path intact as
  runtime, add diagrams to `SPECS/TECH.md`. Adding `google-adk` as a runtime
  dependency is accepted.
- **Locked decisions (user):**
  1. **ADK drives the live pipeline** — an ADK Runner + orchestrator becomes the
     actual driver of the pipeline after a photo passes the gate (gate →
     interview turn → convert → script → narrate). Telegram long-polling stays
     as the transport.
  2. **Custom BaseAgents over existing ports** — each agent is a custom
     `google.adk.agents.BaseAgent` subclass whose `_run_async_impl` delegates to
     the existing typed ports/stage callables (`VisionGate`/`InterviewLLM`/
     `ConverterLLM`/`ScripterLLM`/`NarratorLLM` and the stage functions
     `handle_conversion`, `handle_scripting`, `handle_narrating`,
     `start_interview`, `handle_interview_update`). This keeps **one** Gemini REST
     implementation (redaction, schemas, timeouts), and keeps the test suite
     fully offline.
  3. **Four agents (mapped to rubric)** — `BouncerAgent`, `InterviewerAgent`,
     `ConverterAgent`, `NarratorAgent`. The **NarratorAgent** folds scripting into
     itself (performs script generation/delivery and narration synthesis/delivery
     as needed) so it encapsulates the Scripter + Narrator stages. This matches
     the rubric's "all 4 agents".

- **Branch:** `feature/2026-10-15-adk-orchestration` (created off `main` at
  `cb337a9`).
- **Risk posture:** thin layer; the runtime path becomes ADK-orchestrated but all
  the heavy work (Gemini calls, validation, redaction, state) remains in the
  existing tested code. The per-update invocation uses `asyncio.run` around
  `Runner.run_async` with a single ADK session per `chat_id`.

## Goal

Add a **genuine, thin Google ADK multi-agent orchestration layer** that drives
the live Telegram gateway, while preserving the existing verified behaviour and
test coverage. Specifically:
- Define 4 custom `BaseAgent`s (Bouncer, Interviewer, Converter, Narrator-with-scripting) and a root `DocumentaryOrchestrator` (custom `BaseAgent`).
- Provide a `Runner`-based driver (`AdkPipeline`) that the bouncer delegates to for the live pipeline.
- Wire `bouncer.handle_update` to delegate to the ADK orchestrator (replacing the legacy direct routing; no dual path).
- Keep `SessionStore` as the single source of truth; pass inputs via ADK session state as JSON-serialisable values.
- Add **Mermaid architecture diagrams** in `SPECS/TECH.md` and a new
  `SPECS/ARCHITECTURE.md`.
- Add `google-adk` as a runtime dependency; update docs (`README`, MISSION, TECH, ROADMAP) so the rubric's Category 1 is satisfied.

## Architecture design (thin, low-risk)

### Agent mapping (4 agents)
1. **`BouncerAgent`** — gates the photo intake and performs the pre-routing checks (command handling is done by the orchestrator; gate/photo decisions live here).
2. **`InterviewerAgent`** — owns interview turn-taking (`start_interview`, `handle_interview_update`).
3. **`ConverterAgent`** — performs conversion (`handle_conversion`).
4. **`NarratorAgent`** — performs scripting + TTS (`handle_scripting` followed by `handle_narrating` as needed, or the full script+audio sequence). Scripting is folded in per decision #3.

### Root orchestrator
`DocumentaryOrchestrator` is a custom `BaseAgent` (sub-agents: the 4 above). Its `_run_async_impl`:
- Reads `chat_id`, `update` (JSON-safe, `Update.model_validate` from dumped form), and current phase from session state.
- Implements the same routing logic as `bouncer._route_after_gate` (and the core gate/photo flow when appropriate), delegating to sub-agents for the stage work. It is the router (not just a sequencer) because routing is conditional.
- Stores the operation result (`bool | Skipped`) in `ctx.session.state["result"]`.

### Runtime wiring
- `telegram_documentaries/adk_orchestration.py` (new):
  - `BouncerAgent`, `InterviewerAgent`, `ConverterAgent`, `NarratorAgent` (all subclass `BaseAgent`, take injected stage callables/resources in `__init__`).
  - `DocumentaryOrchestrator` (root).
  - `AdkPipeline` class:
    - Holds `Runner`, `InMemorySessionService`, `orchestrator`.
    - `handle(update: Update, *, chat_id: int, session: SessionStore, transport: Transport, gate: VisionGate, llm: InterviewLLM, converter: ConverterLLM, scripter: ScripterLLM, narrator: NarratorLLM | None, log: logging.Logger) -> bool | Skipped` (sync). 
    - For each call: ensure an ADK session exists for `user_id=str(chat_id)`, `session_id=str(chat_id)`. Run `asyncio.run(_drain(runner.run_async(..., state_delta={"chat_id": chat_id, "update": update.model_dump(mode="json")}, user_id=str(chat_id), session_id=str(chat_id))))`. Retrieve `result` from the final session state (or the last yielded event's state). Return it (preserving types `bool | Skipped`).
- `bouncer.handle_update` is modified to delegate to `AdkPipeline.handle(...)` (the live path). The method signature remains the same (so existing call sites/tests that call it directly continue to work). The behaviour remains identical.

### State contract
- `SessionStore` remains the **single source of truth** for all conversation state (photo, interview, dossier, script, narration, flags, phase). ADK session state carries only **ephemeral inputs** needed to run the orchestrator per update: `chat_id` (int), `update` (JSON-safe dict), and `result` (the return value). ADK state is never used as the canonical conversation store.
- Injected dependencies (`session`, `transport`, `gate`, `llm`, `converter`, `scripter`, `narrator`, `log`) are process-side objects passed to agents at construction (not serialised). This preserves testability and the existing fake seams.

### Async considerations
- The existing pipeline is synchronous and single-threaded. We run the async ADK path via `asyncio.run(...)` per update. Agents call the existing blocking stage functions inside `_run_async_impl` (blocking the event loop briefly) — this is acceptable for single-threaded operation and matches the current bounded synchronous model.
- A small helper `_drain(agen)` consumes the async generator fully.
- Creating Runner/session_service once per process (in `AdkPipeline.__init__`).

## Dependencies

- Add `google-adk==2.12.0` to `requirements.txt` (runtime dependency). `google-genai` is pulled transitively by google-adk (already installed in the environment; pin compatible version is fine).
- No other runtime deps.

## Scope (in)

- New module `telegram_documentaries/adk_orchestration.py` with the 4 agents, `DocumentaryOrchestrator`, `AdkPipeline`.
- Modify `bouncer.py`: import and construct `AdkPipeline` (or have `handle_update` build/use it), delegate to it. Keep the public API of `handle_update` unchanged.
- Add `SPECS/ARCHITECTURE.md` with Mermaid diagrams (system overview, agent graph, sequence for the generation handoff).
- Update `SPECS/TECH.md`: add "Google ADK Orchestration (Phase 8)" section, architecture diagrams (Mermaid), and note that the live pipeline is ADK-orchestrated (custom BaseAgents over existing ports).
- Update `SPECS/MISSION.md`, `SPECS/ROADMAP.md`, `README.md` to reflect ADK orchestration is now implemented (not deferred).
- Add minimal tests as needed (unit tests for the ADK orchestrator components) without breaking existing tests. Existing unit/component tests that call `bouncer.handle_update` must continue to pass (behaviour preserved).
- Ensure `scripts/test` and `scripts/hooks` remain green.

## Out of scope (YAGNI)

- Replacing Gemini REST with ADK LlmAgent calls (we use custom BaseAgents delegating to existing ports).
- Converting the polling loop to async (we keep `asyncio.run` per update).
- Dual-path routing (no fallback path; ADK drives the live pipeline).
- Webhooks, multi-threading, queueing, or off-loop execution.
- Changing the pipeline semantics (phases, state, timeouts, redaction, logging contracts).

## Non-negotiables

- **Behaviour-preserving:** the ADK-orchestrated path must produce identical outcomes to the legacy path for all existing test cases (same return values, same state transitions, same logging events where relevant).
- **Single source of truth:** `SessionStore` is canonical. No conversation state moves to ADK state alone.
- **Offline tests:** no test contacts Telegram or Gemini. All fakes remain injectable; the ADK layer does not introduce network calls in tests.
- **Single-threaded:** bounded synchronous stage calls inside async methods (acceptable, per update).
- **Redaction:** unchanged (still in `gemini.py`).
- **No secrets logged:** never log photo bytes, audio bytes, dossier/script content, or tokens.
- **Keep imports safe:** google-adk is a hard dependency; importing the package must not break the environment if installed (and the module must import cleanly).
- **Backward compatibility of the public API:** `bouncer.handle_update` signature and return types unchanged.

## Acceptance criteria

1. `google-adk` is installed and imported; `requirements.txt` includes it.
2. The live pipeline is driven by ADK: `bouncer.handle_update` delegates to `AdkPipeline`. The 4 agents + root orchestrator exist and are genuinely ADK agents (subclass `BaseAgent`).
3. Behaviour is preserved: all existing unit and component tests still pass (802+ unchanged in spirit). No test introduces network.
4. Architecture diagrams present: Mermaid diagrams in `SPECS/TECH.md` and `SPECS/ARCHITECTURE.md` showing the ADK agent graph and the system overview.
5. Docs reconciled: `SPECS/MISSION.md`, `SPECS/TECH.md`, `SPECS/ROADMAP.md`, `README.md` reflect ADK orchestration is implemented (Category 1 satisfied).
6. `scripts/test` and `scripts/hooks` are green.
7. Live end-to-end still works (manual verification by user).
