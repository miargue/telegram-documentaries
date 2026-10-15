# Architecture — The Telegram Documentaries

This document is the visual companion to `SPECS/TECH.md`. It shows the live
runtime path (Phase 8: the pipeline is driven by a genuine **Google ADK** layer)
and the agent graph that wraps the existing typed ports.

The guiding rule: **`SessionStore` is the single source of truth**, and the ADK
layer is *thin* — every agent delegates to the same tested stage functions and
Gemini REST adapters that existed before Phase 8. No Gemini or transport logic
is re-implemented in the ADK layer.

## 1. System overview

```mermaid
flowchart TD
    TG[Telegram Bot API] -- "getUpdates (long poll)" --> Polling[polling.run_polling]
    Polling -->|"one Update at a time"| Bouncer["bouncer.handle_update"]
    Bouncer -->|"builds from injected deps"| Pipeline[AdkPipeline]
    Pipeline --> Runner["google.adk Runner"]
    Runner --> Orchy["DocumentaryOrchestrator (root agent)"]

    Orchy --> BA[BouncerAgent]
    Orchy --> IA[InterviewerAgent]
    Orchy --> CA[ConverterAgent]
    Orchy --> NA["NarratorAgent (script + audio)"]

    BA --> VisionPort[VisionGate]
    BA --> Handler["media.fetch_photo / bouncer helpers"]
    IA --> InterviewPort[InterviewLLM]
    CA --> ConverterPort[ConverterLLM]
    NA --> ScripterPort[ScripterLLM]
    NA --> NarratorPort[NarratorLLM]

    VisionPort --> Gemini[["Gemini REST / Interactions API"]]
    InterviewPort --> Gemini
    ConverterPort --> Gemini
    ScripterPort --> Gemini
    NarratorPort --> Gemini

    Handler --> Transport[UrllibTransport]
    Orchy -.-> Transport
    Transport -->|sendMessage / sendPhoto / sendAudio| TG

    BA --> Store[(SessionStore)]
    IA --> Store
    CA --> Store
    NA --> Store
    Store -.->|"canonical chat state (photo, interview, dossier, script, audio, flags, phase)"| Orchy
```

The ADK session state carries only the **ephemeral per-update inputs** —
`chat_id` (int), `update` (JSON-safe `Update.model_dump`) and the encoded
`result` — never the canonical conversation. Injected dependencies (`session`,
`transport`, `gate`, `llm`, `converter`, `scripter`, `narrator`, `log`) are
process-side objects handed to the agents at construction time; they are never
serialised.

## 2. ADK agent graph

`DocumentaryOrchestrator` is the root `google.adk.agents.BaseAgent`; the four
stage agents are its `sub_agents`. Each `_run_async_impl` reads the ephemeral
inputs from `ctx.session.state` and calls the existing stage function behind a
narrow typed port. The **NarratorAgent folds the Scripter and Narrator stages**
(script generation/delivery then TTS synthesis/delivery) into one agent, so the
four-agent graph matches all five pipeline stages.

```mermaid
flowchart TD
    subgraph Orchestrator["DocumentaryOrchestrator (root BaseAgent) — router"]
        B["BouncerAgent<br/>messageless / start / album / awaiting_photo gate"]
        I["InterviewerAgent<br/>interview turn-taking"]
        C["ConverterAgent<br/>handle_conversion"]
        N["NarratorAgent<br/>handle_scripting → handle_narrating"]
    end

    B -->|"awaiting_photo: prompt / gate photo"| VG["VisionGate.classify → HumanVerdict"]
    B -->|"past gate: route marker"| I
    I -->|"InterviewLLM.next_question / summarize"| IL["InterviewLLM"]
    I -->|"interview complete"| C
    C -->|"ConverterLLM.hybridize"| CL["ConverterLLM"]
    C -->|"portrait delivered"| N
    N -->|"ScripterLLM.write_script"| SL["ScripterLLM"]
    N -->|"NarratorLLM.synthesize"| NL["NarratorLLM"]
```

Routing (not just sequencing) lives in the orchestrator: it mirrors
`bouncer.handle_update` pre-routing and `bouncer._route_after_gate`, delegating
each stage to its sub-agent and writing the overall `bool | Skipped` to
`ctx.session.state["result"]`.

## 3. Generation handoff (sequence)

When an interview turn **completes** the interview, the dossier report, the
hybrid portrait, the narration script and the MP3 audio message all follow in a
**single update** — no user input in between. The orchestrator drives the chain
by invoking one sub-agent after another on the shared session.

```mermaid
sequenceDiagram
    autonumber
    participant O as DocumentaryOrchestrator
    participant I as InterviewerAgent
    participant C as ConverterAgent
    participant N as NarratorAgent
    participant S as SessionStore
    participant G as Gemini
    participant T as Telegram

    O->>I: run_async(interviewing update)
    I->>G: summarize(transcript)
    G-->>I: Dossier
    I->>S: appoint_dossier(chat_id) → phase=done
    I->>T: sendMessage(dossier report)
    I-->>O: stage_result = True

    Note over O: phase=done & !portrait_sent
    O->>C: run_async()
    C->>G: hybridize(photo, dossier)
    G-->>C: GeneratedImage
    C->>T: sendPhoto(hybrid portrait)
    C->>S: mark_portrait_sent(chat_id)
    C-->>O: stage_result = True

    Note over O: portrait_sent & !script_sent
    O->>N: run_async()
    N->>G: write_script(dossier)
    G-->>N: narration script
    N->>T: sendMessage(script)
    N->>S: store_script + mark_script_sent
    Note over N: script_sent & !narrator_sent
    N->>G: synthesize(script)  (Interactions API)
    G-->>N: MP3 AudioPayload
    N->>T: sendAudio(narration.mp3)
    N->>S: store_narration + mark_narrator_sent
    N-->>O: stage_result = True

    O-->>T: (all messages already delivered)
```

## 4. Async & session model

- **One `asyncio.run` per update.** The gateway is synchronous and
  single-threaded; `AdkPipeline.handle` runs the async ADK path once via
  `asyncio.run`, which matches the existing bounded-synchronous stage model
  (the Converter's ~120 s call included).
- **One ADK session per `chat_id`.** `InMemorySessionService` is created once in
  `AdkPipeline.__init__`; a session (`user_id=str(chat_id)`,
  `session_id=str(chat_id)`) is created on first use and reused thereafter.
- **Skip encoding.** ADK copies the session on the way in/out of the runner, so
  the `SKIPPED` sentinel's identity does not survive the round-trip. A deliberate
  skip is encoded as the JSON-safe string `"skipped"` and decoded back to the
  `SKIPPED` singleton before `handle` returns, keeping the `bool | Skipped`
  contract intact.

## 5. Where to look

- Runtime orchestration: `telegram_documentaries/adk_orchestration.py`.
- Entry point / delegation: `bouncer.handle_update` (constructs an
  `AdkPipeline` from its injected dependencies and calls `handle`).
- Canonical state: `telegram_documentaries/session.py`.
- Ports + Gemini adapters: `vision.py`, `interviewer.py`, `converter.py`,
  `scripter.py`, `narrator.py` (all over the shared `gemini.py` client).
- Technical contract + phase details: `SPECS/TECH.md`.
