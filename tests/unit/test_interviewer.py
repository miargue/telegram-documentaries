"""Behaviour of the InterviewLLM port and its Gemini REST adapter.

``urlopen`` is injected, so every case runs without a live API or a socket and
the tests can assert on the outgoing request body: the researcher persona in
``systemInstruction``, the labelled Q&A transcript as a single ``user``
content, and the JSON schema. Every failure must come back as an
:class:`InterviewError` whose message is redacted of the API key — the same
guarantee the vision adapter provides.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import traceback
import urllib.error
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast

import pytest
from conftest import (
    FakeLLM,
    FakeTransport,
    _RecordingHandler,
    document_update,
    messageless_update,
    photo_update,
    real_invalid_url,
    text_update,
)

from telegram_documentaries.defaults import DEFAULT_QUESTION_COUNT, MAX_ANSWER_LENGTH
from telegram_documentaries.interviewer import (
    APOLOGY_REPLY,
    ASK_PROMPT,
    DOSSIER_SCHEMA,
    EMPTY_TRANSCRIPT_NOTE,
    OBSERVATION_COMPLETE_REPLY,
    QUESTION_SCHEMA,
    SYNTHESIZE_PROMPT,
    GeminiInterviewer,
    InterviewError,
    handle_update,
    start_interview,
)
from telegram_documentaries.logging_config import SKIPPED
from telegram_documentaries.models import Update
from telegram_documentaries.session import Dossier, Phase, QaPair, SessionStore
from telegram_documentaries.transport import (
    MAX_MESSAGE_LENGTH,
    TelegramApiError,
    message_length,
)

SECRET_KEY = "SECRET-GEMINI-KEY-CANARY"


class FakeResponse:
    """Stand-in for the object returned by ``urllib.request.urlopen``."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class RecordingUrlopen:
    """Records requests and replays scripted responses (in order)."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, bytes | None, float]] = []
        self.methods: list[str] = []

    def __call__(self, request: Any, timeout: float) -> FakeResponse:
        self.requests.append((request.full_url, request.data, timeout))
        self.methods.append(request.get_method())
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def make_interviewer(
    responses: list[Any],
    *,
    api_key: str = SECRET_KEY,
    **kwargs: Any,
) -> tuple[GeminiInterviewer, RecordingUrlopen]:
    opener = RecordingUrlopen(responses)
    interviewer = GeminiInterviewer(api_key, urlopen=opener, **kwargs)
    return interviewer, opener


def gemini_body(text: str) -> bytes:
    """A ``generateContent`` response whose only candidate is ``text``."""
    payload = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def question_body(question: str = "Where does the subject sleep?") -> bytes:
    return gemini_body(json.dumps({"question": question}))


def dossier_body(
    *,
    summary: str = "A very dramatic sleeper.",
    animal: str = "House cat",
    reason: str | None = "Owns the hammock.",
) -> bytes:
    payload: dict[str, Any] = {
        "summary": summary,
        "suggested_animal": animal,
    }
    if reason is not None:
        payload["animal_reason"] = reason
    return gemini_body(json.dumps(payload))


def two_pairs() -> list[QaPair]:
    return [
        QaPair(question="Where does it sleep?", answer="In a hammock"),
        QaPair(question="What does it eat?", answer="Salted crackers"),
    ]


# ---------------------------------------------------------------- next_question

def test_next_question_returns_the_question_string() -> None:
    interviewer, _ = make_interviewer([FakeResponse(question_body("where?"))])

    question = interviewer.next_question([])

    assert question == "where?"


def test_next_question_carries_the_persona_as_system_instruction() -> None:
    interviewer, opener = make_interviewer([FakeResponse(question_body())])

    interviewer.next_question([])

    body = json.loads(opener.requests[0][1] or b"{}")
    # The persona is a top-level ``systemInstruction`` — never a user turn, so
    # it cannot create a consecutive-user run (which Gemini rejects).
    assert body["systemInstruction"] == {"parts": [{"text": ASK_PROMPT}]}


def test_next_question_embeds_the_labelled_transcript_in_one_user_turn() -> None:
    interviewer, opener = make_interviewer([FakeResponse(question_body())])

    interviewer.next_question(two_pairs())

    body = json.loads(opener.requests[0][1] or b"{}")
    contents = body["contents"]
    # Exactly one ``user`` content: the transcript is embedded as a labelled
    # block, never as alternating user/model turns (the role inversion bug).
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    prompt = contents[0]["parts"][0]["text"]
    # Interviewer questions and subject answers are clearly labelled, in order.
    assert "Q1: Where does it sleep?" in prompt
    assert "A1: In a hammock" in prompt
    assert "Q2: What does it eat?" in prompt
    assert "A2: Salted crackers" in prompt
    assert prompt.index("Q1:") < prompt.index("A1:") < prompt.index("Q2:")
    # The block ends with the ask instruction.
    assert prompt.endswith(
        f"Ask question 3 of {DEFAULT_QUESTION_COUNT} - exactly one question."
    )
    # The persona lives only in ``systemInstruction``, never in the prompt.
    assert "investigative" not in prompt
    assert body["systemInstruction"] == {"parts": [{"text": ASK_PROMPT}]}

    # A schema, not a regex, constrains the model output.
    generation = body["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseSchema"] == QUESTION_SCHEMA


def test_next_question_with_an_empty_transcript_asks_question_one() -> None:
    interviewer, opener = make_interviewer([FakeResponse(question_body())])

    interviewer.next_question([])

    body = json.loads(opener.requests[0][1] or b"{}")
    contents = body["contents"]
    # Empty transcript → a *single* user content: an explicit "not started"
    # marker followed by the ask instruction.
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    prompt = contents[0]["parts"][0]["text"]
    assert EMPTY_TRANSCRIPT_NOTE in prompt
    assert prompt.endswith(
        f"Ask question 1 of {DEFAULT_QUESTION_COUNT} - exactly one question."
    )
    assert body["systemInstruction"] == {"parts": [{"text": ASK_PROMPT}]}


def test_next_question_uses_the_default_interview_model() -> None:
    interviewer, opener = make_interviewer([FakeResponse(question_body())])

    interviewer.next_question([])

    assert (
        "/v1beta/models/gemini-3.1-flash-lite:generateContent"
        in opener.requests[0][0]
    )


def test_next_question_rejects_an_empty_question() -> None:
    # Telegram cannot send an empty message; an empty question would loop the
    # user through an apology forever, so it must fail loudly for a fresh draw.
    interviewer, _ = make_interviewer([FakeResponse(question_body(""))])

    with pytest.raises(InterviewError):
        interviewer.next_question([])


def test_next_question_rejects_a_whitespace_only_question() -> None:
    interviewer, _ = make_interviewer([FakeResponse(question_body("   "))])

    with pytest.raises(InterviewError):
        interviewer.next_question([])


def test_next_question_rejects_a_question_over_the_telegram_limit() -> None:
    # Telegram rejects sendMessage over 4096 UTF-16 code units, so an over-long
    # model question would loop apologise-and-re-ask forever.
    interviewer, _ = make_interviewer(
        [FakeResponse(question_body("x" * (MAX_MESSAGE_LENGTH + 1)))]
    )

    with pytest.raises(InterviewError):
        interviewer.next_question([])


def test_next_question_rejects_a_short_string_that_exceeds_the_utf16_limit() -> None:
    # 2049 emoji is only 2049 Python characters — well under a ``len()`` check —
    # but 4098 UTF-16 code units, which Telegram rejects. The guard must count
    # code units, not characters.
    over = "😀" * (MAX_MESSAGE_LENGTH // 2 + 1)
    assert len(over) <= MAX_MESSAGE_LENGTH
    interviewer, _ = make_interviewer([FakeResponse(question_body(over))])

    with pytest.raises(InterviewError):
        interviewer.next_question([])


def test_next_question_accepts_a_question_at_the_telegram_limit() -> None:
    # Boundary: exactly the limit is still deliverable, so it must be accepted.
    interviewer, _ = make_interviewer(
        [FakeResponse(question_body("x" * MAX_MESSAGE_LENGTH))]
    )

    assert message_length(interviewer.next_question([])) == MAX_MESSAGE_LENGTH


def test_next_question_accepts_an_astral_question_at_the_utf16_limit() -> None:
    at_limit = "😀" * (MAX_MESSAGE_LENGTH // 2)
    interviewer, _ = make_interviewer([FakeResponse(question_body(at_limit))])

    assert interviewer.next_question([]) == at_limit


# --------------------------------------------------------------------- summarize

def test_summarize_returns_a_dossier() -> None:
    interviewer, _ = make_interviewer([FakeResponse(dossier_body())])

    dossier = interviewer.summarize(two_pairs())

    assert isinstance(dossier, Dossier)
    assert dossier.summary == "A very dramatic sleeper."
    assert dossier.suggested_animal == "House cat"
    assert dossier.animal_reason == "Owns the hammock."


def test_summarize_accepts_a_dossier_without_an_animal_reason() -> None:
    interviewer, _ = make_interviewer(
        [FakeResponse(dossier_body(reason=None))]
    )

    dossier = interviewer.summarize(two_pairs())

    assert dossier.suggested_animal == "House cat"
    assert dossier.animal_reason is None


@pytest.mark.parametrize("pair_count", [2, 5])
def test_summarize_sends_the_persona_categories_and_schema(
    pair_count: int,
) -> None:
    transcript = [
        QaPair(question=f"Q{number}", answer=f"a{number}")
        for number in range(1, pair_count + 1)
    ]
    interviewer, opener = make_interviewer([FakeResponse(dossier_body())])

    interviewer.summarize(transcript)

    body = json.loads(opener.requests[0][1] or b"{}")
    assert body["systemInstruction"] == {"parts": [{"text": SYNTHESIZE_PROMPT}]}
    contents = body["contents"]
    # Exactly one ``user`` content holding the labelled transcript — no
    # user/model alternation, for any transcript length.
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    prompt = contents[0]["parts"][0]["text"]
    for index, pair in enumerate(transcript, start=1):
        assert f"Q{index}: {pair.question}" in prompt
        assert f"A{index}: {pair.answer}" in prompt
    # The persona is only in ``systemInstruction``, never in the prompt.
    assert "investigative" not in prompt
    assert prompt.endswith(
        "Now build the behavioural dossier as the requested JSON."
    )

    generation = body["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseSchema"] == DOSSIER_SCHEMA


def test_summarize_with_an_empty_transcript_ends_with_one_user_turn() -> None:
    interviewer, opener = make_interviewer([FakeResponse(dossier_body())])

    interviewer.summarize([])

    body = json.loads(opener.requests[0][1] or b"{}")
    contents = body["contents"]
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    prompt = contents[0]["parts"][0]["text"]
    assert EMPTY_TRANSCRIPT_NOTE in prompt
    assert prompt.endswith(
        "Now build the behavioural dossier as the requested JSON."
    )
    assert body["systemInstruction"] == {"parts": [{"text": SYNTHESIZE_PROMPT}]}


# ------------------------------------------------------------------ failures

def test_malformed_candidate_output_becomes_an_interview_error() -> None:
    interviewer, _ = make_interviewer([FakeResponse(gemini_body("nope"))])

    with pytest.raises(InterviewError):
        interviewer.next_question([])


def test_schema_invalid_question_becomes_an_interview_error() -> None:
    interviewer, _ = make_interviewer(
        [FakeResponse(gemini_body('{"answer": "not a question"}'))]
    )

    with pytest.raises(InterviewError):
        interviewer.next_question([])


def test_schema_invalid_dossier_becomes_an_interview_error() -> None:
    interviewer, _ = make_interviewer(
        [FakeResponse(gemini_body('{"suggested_animal": 42}'))]
    )

    with pytest.raises(InterviewError):
        interviewer.summarize(two_pairs())


def test_http_failure_becomes_an_interview_error() -> None:
    http_error = urllib.error.HTTPError(
        "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent",
        500,
        "Server Error",
        cast(Any, None),
        io.BytesIO(b'{"error": {"code": 500, "message": "backend exploded"}}'),
    )
    interviewer, _ = make_interviewer([http_error])

    with pytest.raises(InterviewError) as exc_info:
        interviewer.next_question([])

    message = str(exc_info.value)
    assert "500" in message
    assert "backend exploded" in message


def test_network_failure_becomes_an_interview_error() -> None:
    interviewer, _ = make_interviewer([urllib.error.URLError("connection refused")])

    with pytest.raises(InterviewError):
        interviewer.next_question([])


def test_error_payload_becomes_an_interview_error() -> None:
    body = json.dumps(
        {"error": {"code": 400, "message": "API key not valid"}}
    ).encode()
    interviewer, _ = make_interviewer([FakeResponse(body)])

    with pytest.raises(InterviewError, match="API key not valid"):
        interviewer.next_question([])


# ------------------------------------------------------------- secret guarantee

def test_api_key_is_redacted_from_a_raised_interview_error() -> None:
    body = json.dumps(
        {"error": {"message": f"invalid key {SECRET_KEY}"}}
    ).encode()
    interviewer, _ = make_interviewer([FakeResponse(body)])

    with pytest.raises(InterviewError) as exc_info:
        interviewer.next_question([])

    assert SECRET_KEY not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def _interview_failures() -> dict[str, Callable[[], BaseException]]:
    """One fresh, key-bearing failure per normalised interviewer branch."""
    endpoint = f"https://x/v1beta/models/m:generateContent?key={SECRET_KEY}"
    return {
        "invalid_url": lambda: real_invalid_url(
            f"/v1beta/models/m:generateContent?key={SECRET_KEY}\ncontrol"
        ),
        "url_error": lambda: urllib.error.URLError(
            f"connection refused: {endpoint}"
        ),
        "os_error": lambda: OSError(f"timed out talking to {endpoint}"),
        "http_error": lambda: urllib.error.HTTPError(
            endpoint,
            500,
            f"Server Error {SECRET_KEY}",
            cast(Any, None),
            io.BytesIO(
                json.dumps({"error": {"message": f"backend {SECRET_KEY}"}}).encode()
            ),
        ),
        "non_urllib": lambda: ValueError(f"unknown url type: {endpoint}"),
    }


def _format_exception(exc_info: Any) -> str:
    import logging

    from telegram_documentaries.logging_config import JsonFormatter

    record = logging.LogRecord(
        name="interviewer.redaction",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=exc_info,
    )
    return JsonFormatter().format(record)


@pytest.mark.parametrize("method", ["next_question", "summarize"])
@pytest.mark.parametrize("label", sorted(_interview_failures()))
def test_every_normalised_interview_failure_redacts_the_api_key(
    label: str,
    method: str,
) -> None:
    interviewer, _ = make_interviewer([_interview_failures()[label]()])

    with pytest.raises(InterviewError) as exc_info:
        if method == "next_question":
            interviewer.next_question([])
        else:
            interviewer.summarize(two_pairs())

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    json_line = _format_exception((type(error), error, error.__traceback__))

    assert SECRET_KEY not in str(error), f"{label} leaked in str(exc)"
    assert SECRET_KEY not in formatted, f"{label} leaked via traceback"
    assert SECRET_KEY not in json_line, f"{label} leaked via JsonFormatter"
    # The raw cause is suppressed so the key-bearing original never appears in
    # a formatted traceback.
    assert error.__cause__ is None, f"{label} leaked via __cause__"
    assert error.__suppress_context__ is True, f"{label} leaked via __context__"


def test_api_key_with_a_control_character_is_still_redacted() -> None:
    key = "SECRET-GEMINI-KEY-\nCANARY"
    interviewer, _ = make_interviewer(
        [real_invalid_url(f"/v1beta/models/m:generateContent?key={key}")],
        api_key=key,
    )

    with pytest.raises(InterviewError) as exc_info:
        interviewer.next_question([])

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )

    assert "CANARY" not in str(error)
    assert "CANARY" not in formatted
    assert error.__cause__ is None


def test_unexpected_urlopen_error_is_normalised_and_redacted() -> None:
    interviewer, _ = make_interviewer(
        [ValueError(f"unknown url type: https://x?key={SECRET_KEY}")]
    )

    with pytest.raises(InterviewError) as exc_info:
        interviewer.next_question([])

    message = str(exc_info.value)
    traceback_text = "".join(traceback.format_exception(*sys.exc_info()))
    assert "ValueError" in message
    assert SECRET_KEY not in message
    assert SECRET_KEY not in traceback_text


def test_empty_api_key_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        GeminiInterviewer("")


# ------------------------------------------------------------- interview routing

def _update(payload: dict[str, Any]) -> Update:
    return Update.model_validate(payload)


@contextmanager
def _capture(
    logger_name: str,
) -> Iterator[tuple[logging.Logger, list[logging.LogRecord]]]:
    records: list[logging.LogRecord] = []
    logger = logging.getLogger(logger_name)
    handler = _RecordingHandler(records)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield logger, records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _events(records: list[logging.LogRecord]) -> list[str]:
    return [str(getattr(record, "event", "")) for record in records]


def test_unreadable_http_error_body_warns_on_the_interviewer_logger() -> None:
    # The shared client's diagnostics must be attributed to this adapter's
    # logger (mirroring vision.py), not to telegram_documentaries.gemini.
    class UnreadableBody:
        def read(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise OSError("dropped connection")

        def close(self) -> None:
            return None

    http_error = urllib.error.HTTPError(
        "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent",
        500,
        "Server Error",
        cast(Any, {}),
        cast(Any, UnreadableBody()),
    )
    interviewer, _ = make_interviewer([http_error])

    with _capture("telegram_documentaries.interviewer") as (_logger, records):
        with pytest.raises(InterviewError):
            interviewer.next_question([])

    assert any(
        record.levelno == logging.WARNING
        and getattr(record, "event", None) == "http_error_body_unreadable"
        for record in records
    )


def _passed_store(chat_id: int = 1) -> SessionStore:
    store = SessionStore()
    store.record_pass(chat_id=chat_id, photo=b"PORTRAIT-BYTES")
    return store


def _start_via_update(
    transport: FakeTransport,
    store: SessionStore,
    llm: FakeLLM,
    *,
    chat_id: int = 1,
    update_id: int = 100,
    logger: logging.Logger | None = None,
) -> Any:
    """Drive the gate_passed -> interview start transition via the router."""
    return handle_update(
        _update(text_update(update_id, chat_id=chat_id, text="ready")),
        transport,
        session=store,
        llm=llm,
        logger=logger,
    )


class FailNextSendTransport(FakeTransport):
    """Fake transport that fails the next ``sendMessage`` call once.

    ``call`` is overridden (not the transport itself patched) so mypy stays
    happy and the failure can be armed *between* normal sends — e.g. after Q1
    is out, so the answer's next-question dispatch fails exactly once.
    """

    def __init__(self, exception: BaseException | None = None) -> None:
        super().__init__()
        self._pending: BaseException | None = exception

    def arm(self, exception: BaseException) -> None:
        self._pending = exception

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "sendMessage" and self._pending is not None:
            error, self._pending = self._pending, None
            raise error
        return super().call(method, params)


def test_start_interview_sends_q1_and_advances_to_interviewing() -> None:
    store = _passed_store()
    llm = FakeLLM()
    transport = FakeTransport()

    start_interview(transport, 1, session=store, llm=llm)

    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        "Q1"
    ]
    assert llm.next_calls == [[]]
    state = store.get(1)
    assert state.phase is Phase.interviewing
    assert state.interview == []
    # The asked question is stored so re-asks never need the model again.
    assert state.pending_question == "Q1"


def test_start_interview_asks_one_and_only_one_question() -> None:
    store = _passed_store()
    llm = FakeLLM()
    transport = FakeTransport()

    start_interview(transport, 1, session=store, llm=llm)

    assert len(transport.calls_for("sendMessage")) == 1


def test_start_interview_ask_failure_apologises_and_stays_retryable() -> None:
    store = _passed_store()
    llm = FakeLLM()
    llm.next_errors = [InterviewError("model offline")]
    transport = FakeTransport()

    with _capture("interviewer.start_failure") as (logger, records):
        ok = start_interview(transport, 1, session=store, llm=llm, logger=logger)

    assert ok is False
    assert transport.calls_for("sendMessage")[0]["text"] == APOLOGY_REPLY
    assert store.get(1).phase is Phase.gate_passed
    assert "interview_failed" in _events(records)


def test_start_interview_send_failure_stays_retryable() -> None:
    store = _passed_store()
    llm = FakeLLM()
    transport = FakeTransport(
        fail_on={"sendMessage": TelegramApiError("sendMessage", "blocked")}
    )

    ok = start_interview(transport, 1, session=store, llm=llm)

    assert ok is False
    assert store.get(1).phase is Phase.gate_passed
    assert store.get(1).interview == []


def test_start_interview_logs_started_and_first_question_events() -> None:
    store = _passed_store()
    llm = FakeLLM()
    transport = FakeTransport()

    with _capture("interviewer.capture") as (logger, records):
        start_interview(transport, 1, session=store, llm=llm, logger=logger)

    events = _events(records)
    assert "interview_started" in events
    question_events = [
        record
        for record in records
        if getattr(record, "event", None) == "question_asked"
    ]
    assert question_events
    assert getattr(question_events[0], "question_number", None) == 1


def test_gate_passed_update_retries_interview_start() -> None:
    store = _passed_store()
    llm = FakeLLM()
    transport = FakeTransport()

    sent = _start_via_update(transport, store, llm)

    assert sent is True
    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        "Q1"
    ]
    assert store.get(1).phase is Phase.interviewing


def test_first_answer_is_recorded_and_question_two_is_asked() -> None:
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FakeTransport()
    _start_via_update(transport, store, llm)

    sent = handle_update(
        _update(text_update(101, chat_id=1, text="A dramatic amount")),
        transport,
        session=store,
        llm=llm,
    )

    assert sent is True
    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        "Q1",
        "Q2",
    ]
    assert store.get(1).interview == [
        QaPair(question="Q1", answer="A dramatic amount")
    ]
    assert llm.next_calls[-1] == [
        QaPair(question="Q1", answer="A dramatic amount")
    ]


def test_five_answers_complete_the_interview_and_store_the_dossier() -> None:
    dossier = Dossier(
        summary="Snores loudly and claims the hammock.",
        suggested_animal="House cat",
        animal_reason="Sleeps in the sun like a professional.",
    )
    store = _passed_store()
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    transport = FakeTransport()

    results = [_start_via_update(transport, store, llm, update_id=200)]
    with _capture("telegram_documentaries.interviewer") as (_logger, records):
        for index, answer in enumerate(
            ["a1", "a2", "a3", "a4", "a5"], start=1
        ):
            before = len(transport.calls_for("sendMessage"))
            results.append(
                handle_update(
                    _update(text_update(200 + index, chat_id=1, text=answer)),
                    transport,
                    session=store,
                    llm=llm,
                )
            )
            # Exactly one message per user turn: one question (or the report).
            assert len(transport.calls_for("sendMessage")) == before + 1

    replies = [call["text"] for call in transport.calls_for("sendMessage")]
    assert replies[0] == "Q1"
    assert replies[1:5] == ["Q2", "Q3", "Q4", "Q5"]
    assert "House cat" in replies[5]
    assert "Snores loudly" in replies[5]
    assert all(results)

    # Every answer — including the fifth, committed alongside the dossier —
    # emits ``answer_recorded`` with its question number.
    answer_numbers = [
        getattr(record, "question_number", None)
        for record in records
        if getattr(record, "event", None) == "answer_recorded"
    ]
    assert answer_numbers == [1, 2, 3, 4, 5]

    state = store.get(1)
    assert state.phase is Phase.done
    assert state.dossier == dossier
    assert state.interview == [
        QaPair(question=f"Q{number}", answer=f"a{number}")
        for number in range(1, 6)
    ]
    assert llm.summarize_calls == [
        [
            QaPair(question=f"Q{number}", answer=f"a{number}")
            for number in range(1, 6)
        ]
    ]


def test_oversized_dossier_report_is_truncated_and_the_interview_completes() -> None:
    # Regression: a model summary past Telegram's 4096-unit limit used to make
    # the report send fail forever, leaving the 5th answer uncommitted and the
    # interview permanently wedged at four pairs. The send chokepoint now
    # bounds the text, so the report lands and the session completes.
    dossier = Dossier(
        summary="s" * (MAX_MESSAGE_LENGTH * 3),
        suggested_animal="Cat",
        animal_reason="r" * (MAX_MESSAGE_LENGTH * 2),
    )
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"], dossiers=[dossier])
    transport = FakeTransport()
    _start_via_update(transport, store, llm)
    for index in range(1, 6):
        sent = handle_update(
            _update(text_update(680 + index, chat_id=1, text=f"a{index}")),
            transport,
            session=store,
            llm=llm,
        )
        assert sent is True

    assert all(
        message_length(call["text"]) <= MAX_MESSAGE_LENGTH
        for call in transport.calls_for("sendMessage")
    )
    state = store.get(1)
    assert state.phase is Phase.done
    assert state.dossier == dossier
    assert state.interview == [
        QaPair(question=f"Q{number}", answer=f"a{number}")
        for number in range(1, 6)
    ]


def test_a_sixth_answer_after_completion_is_a_light_no_op() -> None:
    # The transcript can never exceed five pairs: once done, every further
    # message is the light observation-complete reply and touches nothing.
    dossier = Dossier(summary="S", suggested_animal="Cat")
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"], dossiers=[dossier])
    transport = FakeTransport()
    _start_via_update(transport, store, llm)
    for index in range(5):
        handle_update(
            _update(text_update(650 + index, chat_id=1, text=f"a{index + 1}")),
            transport,
            session=store,
            llm=llm,
        )
    assert store.get(1).phase is Phase.done
    next_calls_before = len(llm.next_calls)
    summarize_calls_before = len(llm.summarize_calls)

    sent = handle_update(
        _update(text_update(660, chat_id=1, text="a sixth answer")),
        transport,
        session=store,
        llm=llm,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == (
        OBSERVATION_COMPLETE_REPLY
    )
    state = store.get(1)
    assert state.phase is Phase.done
    assert len(state.interview) == 5
    assert state.dossier == dossier
    # No model call can even be attempted for an answer past the cap.
    assert len(llm.next_calls) == next_calls_before
    assert len(llm.summarize_calls) == summarize_calls_before


def test_photo_mid_interview_reasks_the_current_question_without_advancing() -> None:
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FakeTransport()
    _start_via_update(transport, store, llm)
    handle_update(
        _update(text_update(300, chat_id=1, text="a1")),
        transport,
        session=store,
        llm=llm,
    )
    handle_update(
        _update(text_update(301, chat_id=1, text="a2")),
        transport,
        session=store,
        llm=llm,
    )
    before = list(store.get(1).interview)
    next_calls_before = len(llm.next_calls)

    sent = handle_update(
        _update(photo_update(302, chat_id=1)),
        transport,
        session=store,
        llm=llm,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == "Q3"
    assert store.get(1).interview == before
    assert store.get(1).phase is Phase.interviewing
    # A re-ask re-sends the stored question: no LLM call, so the user can
    # never be shown a differently-phrased version of the same question.
    assert len(llm.next_calls) == next_calls_before


def test_pending_question_survives_non_answer_re_asks_until_answered() -> None:
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FakeTransport()
    _start_via_update(transport, store, llm)

    handle_update(
        _update(photo_update(320, chat_id=1)),
        transport,
        session=store,
        llm=llm,
    )
    handle_update(
        _update(text_update(321, chat_id=1, text="   ")),
        transport,
        session=store,
        llm=llm,
    )

    assert store.get(1).pending_question == "Q1"
    assert store.get(1).interview == []
    # Only the opening ask has cost a model call so far.
    assert llm.next_calls == [[]]

    # Committing the answer advances the pending question to the next one.
    handle_update(
        _update(text_update(322, chat_id=1, text="a1")),
        transport,
        session=store,
        llm=llm,
    )
    assert store.get(1).pending_question == "Q2"


def test_each_answer_costs_exactly_one_next_question_call() -> None:
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FakeTransport()
    _start_via_update(transport, store, llm)

    handle_update(
        _update(text_update(330, chat_id=1, text="a1")),
        transport,
        session=store,
        llm=llm,
    )
    handle_update(
        _update(text_update(331, chat_id=1, text="a2")),
        transport,
        session=store,
        llm=llm,
    )

    # One call per answer (for the *next* question) — never a second call to
    # re-derive the question the user is currently looking at.
    assert llm.next_calls == [
        [],
        [QaPair(question="Q1", answer="a1")],
        [
            QaPair(question="Q1", answer="a1"),
            QaPair(question="Q2", answer="a2"),
        ],
    ]


@pytest.mark.parametrize("kind", ["document", "sticker", "voice"])
def test_non_text_content_mid_interview_reasks_the_current_question(
    kind: str,
) -> None:
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FakeTransport()
    _start_via_update(transport, store, llm)
    handle_update(
        _update(text_update(310, chat_id=1, text="a1")),
        transport,
        session=store,
        llm=llm,
    )
    before = list(store.get(1).interview)

    sent = handle_update(
        _update(document_update(311, chat_id=1, kind=kind)),
        transport,
        session=store,
        llm=llm,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == "Q2"
    assert store.get(1).interview == before


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_text_mid_interview_reasks_the_current_question(text: str) -> None:
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FakeTransport()
    _start_via_update(transport, store, llm)
    before = list(store.get(1).interview)

    sent = handle_update(
        _update(text_update(312, chat_id=1, text=text)),
        transport,
        session=store,
        llm=llm,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == "Q1"
    assert store.get(1).interview == before


def test_long_answer_is_truncated_before_it_reaches_the_transcript() -> None:
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FakeTransport()
    _start_via_update(transport, store, llm)

    with _capture("interviewer.truncate") as (logger, records):
        sent = handle_update(
            _update(text_update(303, chat_id=1, text="x" * 500)),
            transport,
            session=store,
            llm=llm,
            logger=logger,
        )

    assert sent is True
    assert store.get(1).interview == [
        QaPair(question="Q1", answer="x" * MAX_ANSWER_LENGTH)
    ]
    assert "answer_truncated" in _events(records)


def test_next_question_send_failure_does_not_commit_and_reasks() -> None:
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FailNextSendTransport()
    _start_via_update(transport, store, llm)
    transport.arm(TelegramApiError("sendMessage", "dispatch blocked"))

    sent = handle_update(
        _update(text_update(304, chat_id=1, text="a1")),
        transport,
        session=store,
        llm=llm,
    )

    assert sent is False
    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        "Q1",
        APOLOGY_REPLY,
        "Q1",
    ]
    assert store.get(1).interview == []
    assert store.get(1).phase is Phase.interviewing


def test_next_question_ask_failure_apologises_and_retries_next_message() -> None:
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FakeTransport()
    _start_via_update(transport, store, llm)
    llm.next_errors = [InterviewError("stream hiccup")]

    with _capture("interviewer.ask_failure") as (logger, records):
        sent = handle_update(
            _update(text_update(305, chat_id=1, text="a1")),
            transport,
            session=store,
            llm=llm,
            logger=logger,
        )

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == APOLOGY_REPLY
    assert store.get(1).interview == []
    assert "interview_failed" in _events(records)

    # The next message retries cleanly and the answer lands.
    handle_update(
        _update(text_update(306, chat_id=1, text="a1")),
        transport,
        session=store,
        llm=llm,
    )
    assert store.get(1).interview == [QaPair(question="Q1", answer="a1")]
    assert transport.calls_for("sendMessage")[-1]["text"] == "Q2"


def test_synthesis_failure_apologises_keeps_interviewing_and_retries() -> None:
    dossier = Dossier(summary="S", suggested_animal="Cat", animal_reason="R")
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"], dossiers=[dossier])
    llm.summarize_errors = [InterviewError("synthesis tripped")]
    transport = FakeTransport()
    _start_via_update(transport, store, llm)
    for index in range(4):
        handle_update(
            _update(text_update(400 + index, chat_id=1, text=f"a{index + 1}")),
            transport,
            session=store,
            llm=llm,
        )

    with _capture("interviewer.synth_failure") as (logger, records):
        sent = handle_update(
            _update(text_update(404, chat_id=1, text="a5")),
            transport,
            session=store,
            llm=llm,
            logger=logger,
        )

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == APOLOGY_REPLY
    assert store.get(1).phase is Phase.interviewing
    assert len(store.get(1).interview) == 4
    assert "interview_failed" in _events(records)

    # The next message retries synthesis and completes the interview.
    handle_update(
        _update(text_update(405, chat_id=1, text="a5")),
        transport,
        session=store,
        llm=llm,
    )
    assert store.get(1).phase is Phase.done
    assert len(store.get(1).interview) == 5
    assert "Cat" in transport.calls_for("sendMessage")[-1]["text"]


def test_dossier_send_failure_is_retried_on_the_next_message() -> None:
    dossier = Dossier(summary="S", suggested_animal="Cat", animal_reason="R")
    store = _passed_store()
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier, dossier],  # one per summarize attempt
    )
    transport = FailNextSendTransport()
    _start_via_update(transport, store, llm)
    for index in range(4):
        handle_update(
            _update(text_update(410 + index, chat_id=1, text=f"a{index + 1}")),
            transport,
            session=store,
            llm=llm,
        )
    transport.arm(TelegramApiError("sendMessage", "report blocked"))

    sent = handle_update(
        _update(text_update(414, chat_id=1, text="a5")),
        transport,
        session=store,
        llm=llm,
    )

    assert sent is False
    # The user is never left in silence: a failed report is followed by a
    # best-effort apology, and the session stays retryable.
    assert transport.calls_for("sendMessage")[-1]["text"] == APOLOGY_REPLY
    assert store.get(1).phase is Phase.interviewing
    assert len(store.get(1).interview) == 4

    handle_update(
        _update(text_update(415, chat_id=1, text="a5")),
        transport,
        session=store,
        llm=llm,
    )
    assert store.get(1).phase is Phase.done
    assert len(store.get(1).interview) == 5


def test_dossier_created_logs_the_animal_but_not_the_summary() -> None:
    dossier = Dossier(
        summary="SECRET-SUMMARY-USER-TEXT",
        suggested_animal="Capybara",
        animal_reason="Zen about everything.",
    )
    store = _passed_store()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"], dossiers=[dossier])
    transport = FakeTransport()

    with _capture("interviewer.complete") as (logger, records):
        _start_via_update(transport, store, llm, logger=logger)
        for index, answer in enumerate(["a1", "a2", "a3", "a4", "a5"], start=1):
            handle_update(
                _update(text_update(420 + index, chat_id=1, text=answer)),
                transport,
                session=store,
                llm=llm,
                logger=logger,
            )

    assert "dossier_created" in _events(records)
    dossier_records = [
        record
        for record in records
        if getattr(record, "event", None) == "dossier_created"
    ]
    assert getattr(dossier_records[0], "suggested_animal", None) == "Capybara"
    assert all(
        "SECRET-SUMMARY-USER-TEXT" not in str(record.getMessage())
        for record in records
    )


def test_done_phase_replies_lightly_without_touching_state() -> None:
    store = SessionStore()
    store.start_interview(chat_id=9)
    for number in range(1, 6):
        store.record_interview_answer(9, f"Q{number}", f"a{number}")
    dossier = Dossier(summary="S", suggested_animal="Cat")
    store.appoint_dossier(9, dossier)
    llm = FakeLLM()
    transport = FakeTransport()

    sent = handle_update(
        _update(text_update(500, chat_id=9, text="hello again")),
        transport,
        session=store,
        llm=llm,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == (
        OBSERVATION_COMPLETE_REPLY
    )
    assert store.get(9).phase is Phase.done
    assert store.get(9).dossier == dossier
    assert len(store.get(9).interview) == 5
    assert llm.next_calls == []
    assert llm.summarize_calls == []


def test_messageless_update_is_skipped_without_asking_the_llm() -> None:
    store = SessionStore()
    llm = FakeLLM()
    transport = FakeTransport()

    sent = handle_update(
        _update(messageless_update(600)),
        transport,
        session=store,
        llm=llm,
    )

    assert sent is SKIPPED
    assert transport.calls_for("sendMessage") == []
    assert llm.next_calls == []


def test_unknown_phase_update_is_skipped_with_an_interview_skipped_log() -> None:
    # A chat that was never gated (awaiting_photo) reaches the Interviewer's
    # unknown-phase branch: a silent no-op is not enough, the skip is logged.
    store = SessionStore()
    llm = FakeLLM()
    transport = FakeTransport()

    with _capture("interviewer.unknown_phase") as (logger, records):
        sent = handle_update(
            _update(text_update(610, chat_id=5, text="hello")),
            transport,
            session=store,
            llm=llm,
            logger=logger,
        )

    assert sent is SKIPPED
    assert "interview_skipped" in _events(records)
    assert transport.calls_for("sendMessage") == []
    assert llm.next_calls == []


def test_interviewing_state_without_a_pending_question_fails_loud() -> None:
    # Prevention: the "interviewing implies a stored pending question"
    # invariant is guarded, not asserted away. A corrupted state must never
    # fall back to the model — it logs the break, apologises and stays put.
    store = _passed_store()
    store.get(1).phase = Phase.interviewing  # simulate corrupted state
    llm = FakeLLM()
    transport = FakeTransport()

    with _capture("interviewer.orphan") as (logger, records):
        sent = handle_update(
            _update(text_update(620, chat_id=1, text="an answer")),
            transport,
            session=store,
            llm=llm,
            logger=logger,
        )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == APOLOGY_REPLY
    assert store.get(1).interview == []
    assert llm.next_calls == []
    assert llm.summarize_calls == []
    assert "interview_failed" in _events(records)


def test_unexpected_error_is_caught_and_degrades_politely() -> None:
    store = _passed_store()
    llm = FakeLLM()
    llm.next_errors = [RuntimeError("boom")]
    transport = FakeTransport()

    sent = _start_via_update(transport, store, llm)

    assert sent is False
    assert transport.calls_for("sendMessage")[0]["text"] == APOLOGY_REPLY
    assert store.get(1).phase is Phase.gate_passed


def test_keyboard_interrupt_is_not_swallowed() -> None:
    store = _passed_store()
    llm = FakeLLM()
    transport = FakeTransport(fail_on={"sendMessage": KeyboardInterrupt()})

    with pytest.raises(KeyboardInterrupt):
        _start_via_update(transport, store, llm)


def test_two_chats_never_share_an_interview() -> None:
    store = SessionStore()
    store.record_pass(chat_id=1, photo=b"P")
    store.record_pass(chat_id=2, photo=b"P")
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FakeTransport()

    _start_via_update(transport, store, llm, chat_id=1, update_id=700)
    _start_via_update(transport, store, llm, chat_id=2, update_id=701)
    handle_update(
        _update(text_update(702, chat_id=2, text="answer from chat 2")),
        transport,
        session=store,
        llm=llm,
    )

    assert store.get(1).interview == []
    assert store.get(2).interview == [
        QaPair(question="Q1", answer="answer from chat 2")
    ]
