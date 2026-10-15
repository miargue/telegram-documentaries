# Validation — ADK Orchestration (Phase 8)

## 0. Preconditions

- [ ] On branch `feature/2026-10-15-adk-orchestration`, based on `main` at `cb337a9`.
- [ ] `scripts/test` green on the tree (802 passed baseline), `scripts/hooks` green.

## 1. Dependencies

- [ ] `google-adk` is installed (`pip show google-adk` shows Version 2.12.0).
- [ ] `requirements.txt` contains `google-adk==2.12.0`.

## 2. ADK layer implementation

- [ ] `telegram_documentaries/adk_orchestration.py` exists.
- [ ] `BouncerAgent`, `InterviewerAgent`, `ConverterAgent`, `NarratorAgent` subclass
      `google.adk.agents.BaseAgent`.
- [ ] `DocumentaryOrchestrator` is a `BaseAgent` with the 4 agents as sub-agents.
- [ ] `AdkPipeline` class exists with `handle(...) -> bool | Skipped`.
- [ ] Agents delegate to existing ports/stage functions (custom BaseAgents, no
      ADK LlmAgent model calls).
- [ ] Scripting is folded into `NarratorAgent` (per mapping).
- [ ] SessionStore remains the canonical source of truth; ADK state carries only
      ephemeral inputs (`chat_id`, `update`, `result`).

## 3. Live path wiring

- [ ] `bouncer.handle_update` delegates to `AdkPipeline` (not the legacy direct
      routing).
- [ ] `handle_update` signature and return types unchanged.
- [ ] Pre-routing cases preserved (messageless, start command, album skip).
- [ ] Phase routing and post-handoff (convert → script → audio) preserved.

## 4. Behaviour parity & tests

- [ ] All existing unit tests pass (`tests/unit/test_bouncer.py`,
      `tests/unit/test_session.py`, `tests/unit/test_polling.py`, etc.).
- [ ] All existing component tests pass (`tests/component/*`).
- [ ] Total test count >= 802 (no regressions). No test touches the network.
- [ ] `scripts/test` green, `scripts/hooks` green.
- [ ] Full suite with every socket entry point patched to raise is green.

## 5. Architecture diagrams & docs

- [ ] `SPECS/ARCHITECTURE.md` exists with Mermaid diagrams (system overview, agent
      graph, sequence for generation handoff).
- [ ] `SPECS/TECH.md` has "Google ADK Orchestration (Phase 8)" section with
      diagrams and the thin-layer description.
- [ ] `SPECS/MISSION.md` updated (ADK orchestration implemented).
- [ ] `SPECS/ROADMAP.md` Phase 8 marked complete.
- [ ] `README.md` updated (ADK orchestration mentioned).

## 6. Acceptance

- [ ] Live bot can be started (imports work, AdkPipeline builds).
- [ ] End-to-end behaviour matches Phase 7 verified flow (manual check).
- [ ] No secrets logged; redaction unchanged.
- [ ] Category 1 rubric gap closed (diagrams + 4 agents + ADK orchestration documented).
