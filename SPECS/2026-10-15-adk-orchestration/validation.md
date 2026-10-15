# Validation — ADK Orchestration (Phase 8)

## 0. Preconditions

- [x] On branch `feature/2026-10-15-adk-orchestration`, based on `main` at `cb337a9`.
- [x] `scripts/test` green on the tree (802 passed baseline), `scripts/hooks` green.

## 1. Dependencies

- [x] `google-adk` is installed (`pip show google-adk` shows Version 2.12.0;
      `google-genai` 2.31.0 arrives transitively).
- [x] `requirements.txt` contains `google-adk==2.12.0`.

## 2. ADK layer implementation

- [x] `telegram_documentaries/adk_orchestration.py` exists.
- [x] `BouncerAgent`, `InterviewerAgent`, `ConverterAgent`, `NarratorAgent` subclass
      `google.adk.agents.BaseAgent` (asserted in
      `tests/unit/test_adk_orchestration.py::test_stage_agents_subclass_base_agent`).
- [x] `DocumentaryOrchestrator` is a `BaseAgent` with the 4 agents as sub-agents
      (`test_orchestrator_is_a_base_agent_with_four_sub_agents`).
- [x] `AdkPipeline` class exists with `handle(...) -> bool | Skipped`.
- [x] Agents delegate to existing ports/stage functions (custom BaseAgents, no
      ADK LlmAgent model calls): `_run_async_impl` calls the injected
      `handle_photo` / `handle_interview_update` / `handle_conversion` /
      `handle_scripting` / `handle_narrating` and the typed ports. Delegation is
      asserted with injected spies (`test_bouncer_agent_delegates_photo_to_injected_handler`,
      `test_interviewer_agent_delegates_to_injected_callable`,
      `test_converter_agent_delegates_to_injected_callable`).
- [x] Scripting is folded into `NarratorAgent` (`test_narrator_agent_folds_scripting_then_narration`,
      `test_narrator_agent_stops_when_scripting_fails`).
- [x] SessionStore remains the canonical source of truth; ADK state carries only
      ephemeral inputs (`chat_id`, `update`, `result`). The `"skipped"` encoding
      round-trips to the `SKIPPED` singleton (`test_pipeline_messageless_update_returns_the_skipped_singleton`).

## 3. Live path wiring

- [x] `bouncer.handle_update` delegates to `AdkPipeline` (not the legacy direct
      routing): it builds the pipeline from its injected deps and returns
      `pipeline.handle(update, chat_id=...)`.
- [x] `handle_update` signature and return types unchanged (`bool | Skipped`); all
      existing direct call sites and tests compile and pass unchanged.
- [x] Pre-routing cases preserved (messageless, start command, album skip) — all
      802 pre-existing tests pass through the ADK path.
- [x] Phase routing and post-handoff (convert → script → audio) preserved
      (`test_pipeline_full_chain_completion_delivers_every_artifact` plus the
      component `test_full_gateway_flow.py` chain).

## 4. Behaviour parity & tests

- [x] All existing unit tests pass (`tests/unit/test_bouncer.py`,
      `tests/unit/test_session.py`, `tests/unit/test_polling.py`, etc.).
- [x] All existing component tests pass (`tests/component/*`).
- [x] Total test count >= 802 — **821 passed** (802 baseline + 19 new). No test
      touches the network.
- [x] `scripts/test` green, `scripts/hooks` green (compileall + smoke import +
      ruff + mypy: `Success: no issues found in 45 source files`).
- [x] Full suite with every socket entry point patched to raise is green
      (`socket.socket.connect`, `connect_ex`, `create_connection`,
      `getaddrinfo` all patched to raise: 821 passed).

## 5. Architecture diagrams & docs

- [x] `SPECS/ARCHITECTURE.md` exists with Mermaid diagrams (system overview, agent
      graph, sequence for generation handoff).
- [x] `SPECS/TECH.md` has "Google ADK Orchestration (Phase 8)" section with a
      Mermaid agent graph and the thin-layer description.
- [x] `SPECS/MISSION.md` updated (ADK orchestration implemented).
- [x] `SPECS/ROADMAP.md` Phase 8 marked complete.
- [x] `README.md` updated (ADK orchestration mentioned; layout, deps, test counts).

## 6. Acceptance

- [x] Live bot can be started (imports work, `AdkPipeline` builds — asserted by
      `scripts/hooks` smoke import and `test_pipeline_builds_one_runner_and_spawns_sessions_per_chat`).
- [ ] End-to-end behaviour matches Phase 7 verified flow (manual live check — user).
- [x] No secrets logged; redaction unchanged (`gemini.py` untouched).
- [x] Category 1 rubric gap closed (diagrams + 4 agents + ADK orchestration documented).

## Results

Delivered on `feature/2026-10-15-adk-orchestration`:

- **New:** `telegram_documentaries/adk_orchestration.py` — `_StageAgent` base,
  `BouncerAgent`, `InterviewerAgent`, `ConverterAgent`, `NarratorAgent` (folds
  scripting), `DocumentaryOrchestrator` (root router) and `AdkPipeline` (`Runner`
  + `InMemorySessionService`, one session per `chat_id`, `asyncio.run` per
  update, JSON-safe `"skipped"` encoding decoded back to the `SKIPPED` sentinel).
- **New tests:** `tests/unit/test_adk_orchestration.py`, `tests/unit/test_adk_pipeline.py` (19).
- **Modified:** `bouncer.py` (delegates to `AdkPipeline`; `_route_after_gate` and
  the injected helpers retained as the routing reference the orchestrator
  mirrors), `requirements.txt` (`google-adk==2.12.0`).
- **Docs:** new `SPECS/ARCHITECTURE.md`; updated `SPECS/TECH.md`,
  `SPECS/MISSION.md`, `SPECS/ROADMAP.md`, `README.md`.

Verification: `scripts/test` — **821 passed**; `scripts/hooks` — all checks
passed; full suite green with all socket entry points patched to raise.
Behaviour parity is proven by the 802 pre-existing unit/component tests running
unchanged through the new ADK path.

## Verifier re-run (2026-10-15)

Independently re-verified against the working tree on
`feature/2026-10-15-adk-orchestration` (implementation uncommitted; spec commit
`385af58`):

- `./scripts/test` → **821 passed** in 13.07s.
- `./scripts/hooks` → compileall + smoke import + ruff (`All checks passed!`) +
  mypy (`Success: no issues found in 45 source files`).
- Socket-patched full suite (all of `socket.socket.connect`, `connect_ex`,
  `socket.create_connection`, `socket.getaddrinfo` replaced with a raising
  function) → **821 passed**; the four patched entry points were each asserted to
  raise, so the patch demonstrably fires rather than silently no-op'ing.
- `git diff main -- tests/` is empty (no pre-existing test modified);
  19 new `def test_` functions confirmed (12 in `test_adk_orchestration.py`,
  7 in `test_adk_pipeline.py`), i.e. 802 baseline + 19 = 821.
- `telegram_documentaries/gemini.py` unchanged vs `main`; `requirements.txt` diff
  is a single line (`google-adk==2.12.0`); `google-genai` 2.31.0 present
  transitively.
- Docs reconciled: `SPECS/ARCHITECTURE.md` (3 Mermaid diagrams), `SPECS/TECH.md`
  §"Google ADK Orchestration (Phase 8)" (agent table + Mermaid graph),
  `SPECS/MISSION.md`, `SPECS/ROADMAP.md` Phase 8 "Status: complete", `README.md`
  (status, deps, layout, test total 821).

**Deviations / observations**

- `bouncer._route_after_gate` (and its helper imports) is no longer referenced by
  production code or tests — `handle_update` now delegates wholly to
  `AdkPipeline`. It is intentionally retained as the documented routing reference
  the orchestrator mirrors (see §"Modified" above). No functional impact; ruff
  does not flag it.
- The plan marked an explicit `bouncer.handle_update`-uses-`AdkPipeline` unit test
  as optional; none was added. Behaviour parity is instead proven by the 802
  unchanged tests exercising the ADK path.
- §6 final item (live end-to-end behaviour on real Telegram/Gemini) remains
  user-owned and is intentionally unchecked.
