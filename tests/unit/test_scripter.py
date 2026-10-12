"""Behaviour of the Phase 5 Scripter: typed port + Gemini REST adapter.

``urlopen`` is injected, so every case runs without a live API or a socket and
the tests assert on the outgoing request body: the *revered, dramatic British
wildlife documentary narrator* persona lives only in ``systemInstruction``,
exactly one ``user`` content cites the dossier, and the JSON ``responseSchema``
is ``{"script": str}``. The adapter mechanically enforces the output contract —
non-empty, ≤ ``MAX_SCRIPT_WORDS``, no blank-line paragraph break — and every
failure comes back as a redacted :class:`ScriptError` (the fourth adapter
honouring the single-redaction guarantee).
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import traceback
import urllib.error
from collections.abc import Callable
from typing import Any, cast

import pytest
from conftest import FakeScripter, FakeTransport, _RecordingHandler

from telegram_documentaries.gemini import DEFAULT_BASE_URL
from telegram_documentaries.scripter import (
    MAX_SCRIPT_WORDS,
    SCRIPT_APOLOGY_REPLY,
    SCRIPT_DIRECTIVE,
    SCRIPT_PERSONA,
    SCRIPT_RESET_REPLY,
    SCRIPT_SCHEMA,
    GeminiScripter,
    ScriptError,
    handle_scripting,
    word_count,
)
from telegram_documentaries.session import Dossier, Phase, SessionStore
from telegram_documentaries.transport import (
    MAX_MESSAGE_LENGTH,
    TelegramApiError,
    message_length,
)

SECRET_KEY = "SECRET-SCRIPTER-KEY-CANARY"


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


def make_scripter(
    responses: list[Any],
    *,
    api_key: str = SECRET_KEY,
    **kwargs: Any,
) -> tuple[GeminiScripter, RecordingUrlopen]:
    opener = RecordingUrlopen(responses)
    client = GeminiScripter(api_key, urlopen=opener, **kwargs)
    return client, opener


def script_body(text: str) -> bytes:
    """A ``generateContent`` response whose candidate is ``{"script": text}``."""
    payload = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": json.dumps({"script": text})}],
                },
                "finishReason": "STOP",
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def dossier() -> Dossier:
    return Dossier(
        summary="Sleeps eighteen hours a day and hoards shiny cutlery.",
        suggested_animal="caffeinated badger",
        animal_reason="Nocturnal, territorial and permanently over-caffeinated.",
    )


def sent_body(opener: RecordingUrlopen) -> dict[str, Any]:
    """The JSON request the scripter actually posted."""
    _url, data, _timeout = opener.requests[0]
    assert data is not None
    return json.loads(data)


def ninety_words() -> str:
    return " ".join(f"word{number}" for number in range(1, MAX_SCRIPT_WORDS + 1))


# --------------------------------------------------------- request body

def test_write_script_posts_a_single_user_turn_citing_the_dossier() -> None:
    client, opener = make_scripter([FakeResponse(script_body("A narration."))])
    subject = dossier()

    client.write_script(subject)

    body = sent_body(opener)
    contents = body["contents"]
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    directive = contents[0]["parts"][0]["text"]
    # The dossier fields the narrator needs all ride in this one content.
    assert subject.suggested_animal in directive
    assert subject.summary in directive
    assert subject.animal_reason in directive
    assert isinstance(subject.animal_reason, str)
    # The persona lives only in ``systemInstruction``, never in a turn.
    assert SCRIPT_PERSONA not in directive


def test_write_script_keeps_the_persona_out_of_the_turns() -> None:
    client, opener = make_scripter([FakeResponse(script_body("A narration."))])

    client.write_script(dossier())

    body = sent_body(opener)
    system = body["systemInstruction"]["parts"][0]["text"]
    user_text = body["contents"][0]["parts"][0]["text"]

    assert system == SCRIPT_PERSONA
    assert system.strip()
    assert SCRIPT_PERSONA not in user_text


def test_write_script_persona_is_the_revered_british_narrator() -> None:
    # The persona carries the whole Phase 5 activity contract: the narrator,
    # the funny-animal framing, the one-paragraph ≤90-word shape and the
    # voice-ready no-markdown rule.
    lowered = SCRIPT_PERSONA.lower()
    for fragment in (
        "british",
        "documentary narrator",
        "one paragraph",
        "90 words",
        "no markdown",
        "voice",
    ):
        assert fragment in lowered, f"persona must say {fragment!r}"
    assert "revered" in lowered


def test_write_script_asks_for_json_with_the_script_schema() -> None:
    client, opener = make_scripter([FakeResponse(script_body("A narration."))])

    client.write_script(dossier())

    config = sent_body(opener)["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"] == SCRIPT_SCHEMA
    expected = {
        "type": "object",
        "properties": {"script": {"type": "string"}},
        "required": ["script"],
    }
    assert SCRIPT_SCHEMA == expected


def test_write_script_omits_the_animal_reason_when_absent() -> None:
    client, opener = make_scripter([FakeResponse(script_body("A narration."))])
    subject = Dossier(summary="S", suggested_animal="Capybara")

    client.write_script(subject)

    directive = sent_body(opener)["contents"][0]["parts"][0]["text"]
    assert subject.suggested_animal in directive
    assert subject.summary in directive
    assert "animal_reason" not in directive


def test_write_script_posts_to_the_generate_content_endpoint() -> None:
    client, opener = make_scripter([FakeResponse(script_body("A narration."))])

    client.write_script(dossier())

    url, data, timeout = opener.requests[0]
    assert opener.methods[0] == "POST"
    assert url == (
        f"{DEFAULT_BASE_URL}/v1beta/models/gemini-3.1-flash-lite:"
        f"generateContent?key={SECRET_KEY}"
    )
    assert data is not None
    assert timeout == pytest.approx(30.0)


def test_write_script_uses_the_default_script_model() -> None:
    import telegram_documentaries.defaults as defaults

    assert defaults.DEFAULT_SCRIPT_MODEL == "gemini-3.1-flash-lite"
    client, opener = make_scripter([FakeResponse(script_body("A narration."))])

    client.write_script(dossier())

    assert (
        "/v1beta/models/gemini-3.1-flash-lite:generateContent"
        in opener.requests[0][0]
    )


def test_write_script_returns_the_stripped_script_string() -> None:
    client, _ = make_scripter(
        [FakeResponse(script_body("  A dramatic narration.  "))]
    )

    script = client.write_script(dossier())

    assert script == "A dramatic narration."


# ------------------------------------------------------------- word counter

def test_word_count_splits_on_whitespace() -> None:
    assert word_count("one two three") == 3
    assert word_count("one two three  \n four") == 4


def test_word_count_counts_an_emoji_without_spaces_as_one_word() -> None:
    # A single run of unspaced emoji is one whitespace-split token.
    assert word_count("snores 😴😴😴") == 2


def test_word_count_counts_a_hyphenated_compound_as_one_word() -> None:
    assert word_count("over-caffeinated badger") == 2


def test_word_count_of_an_empty_string_is_zero() -> None:
    assert word_count("") == 0
    assert word_count("   ") == 0


# ---------------------------------------------------- unusable model output

def test_write_script_rejects_an_empty_script() -> None:
    client, _ = make_scripter([FakeResponse(script_body(""))])

    with pytest.raises(ScriptError):
        client.write_script(dossier())


def test_write_script_rejects_a_whitespace_only_script() -> None:
    client, _ = make_scripter([FakeResponse(script_body("   \n  "))])

    with pytest.raises(ScriptError):
        client.write_script(dossier())


def test_write_script_rejects_a_script_over_the_word_limit() -> None:
    too_long = " ".join(f"word{number}" for number in range(1, MAX_SCRIPT_WORDS + 2))
    client, _ = make_scripter([FakeResponse(script_body(too_long))])

    with pytest.raises(ScriptError):
        client.write_script(dossier())


def test_write_script_accepts_a_script_exactly_at_the_word_limit() -> None:
    client, _ = make_scripter([FakeResponse(script_body(ninety_words()))])

    assert word_count(client.write_script(dossier())) == MAX_SCRIPT_WORDS


def test_write_script_rejects_a_blank_line_paragraph_break() -> None:
    client, _ = make_scripter(
        [FakeResponse(script_body("Para one ends here.\n\nPara two."))]
    )

    with pytest.raises(ScriptError):
        client.write_script(dossier())


def test_write_script_accepts_a_single_line_break_within_a_paragraph() -> None:
    # Soft line wrapping is not a paragraph break; only a blank line is.
    client, _ = make_scripter([FakeResponse(script_body("Line one.\nLine two."))])

    assert client.write_script(dossier()) == "Line one.\nLine two."


def test_write_script_rejects_a_whitespace_only_blank_line() -> None:
    # A line of spaces/tabs is still a blank line: the paragraph-break guard
    # must not be fooled by invisible whitespace.
    client, _ = make_scripter(
        [FakeResponse(script_body("Para one ends here.\n \t \nPara two."))]
    )

    with pytest.raises(ScriptError):
        client.write_script(dossier())


def test_write_script_rejects_a_crlf_blank_line() -> None:
    # Windows-style line endings must be caught the same way as ``\n`` breaks.
    client, _ = make_scripter(
        [FakeResponse(script_body("Para one.\r\n\r\nPara two."))]
    )

    with pytest.raises(ScriptError):
        client.write_script(dossier())


def test_write_script_accepts_a_crlf_single_line_break() -> None:
    # A CRLF soft wrap is not a paragraph break either.
    client, _ = make_scripter(
        [FakeResponse(script_body("Line one.\r\nLine two."))]
    )

    assert client.write_script(dossier()) == "Line one.\r\nLine two."


# ----------------------------------------------------------------- failures

def test_schema_invalid_output_becomes_a_script_error() -> None:
    payload = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": '{"narration": "no script key"}'}],
                },
                "finishReason": "STOP",
            }
        ]
    }
    client, _ = make_scripter([FakeResponse(json.dumps(payload).encode())])

    with pytest.raises(ScriptError):
        client.write_script(dossier())


def test_malformed_candidate_output_becomes_a_script_error() -> None:
    payload = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "not json"}]},
                "finishReason": "STOP",
            }
        ]
    }
    client, _ = make_scripter([FakeResponse(json.dumps(payload).encode())])

    with pytest.raises(ScriptError):
        client.write_script(dossier())


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection refused"),
        urllib.error.HTTPError(
            "https://generativelanguage.googleapis.com/v1beta/models/x:"
            "generateContent",
            500,
            "Server Error",
            cast(Any, None),
            io.BytesIO(b'{"error": {"message": "backend exploded"}}'),
        ),
        http.client.IncompleteRead(b"half"),
        TimeoutError("timed out"),
        b"not json at all",
        json.dumps({"candidates": []}).encode(),
        json.dumps({"error": {"message": "API key not valid"}}).encode(),
    ],
)
def test_write_script_normalises_every_failure(failure: Any) -> None:
    client, _ = make_scripter(
        [failure if isinstance(failure, BaseException) else FakeResponse(failure)]
    )

    with pytest.raises(ScriptError) as exc_info:
        client.write_script(dossier())

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


def _script_failures() -> dict[str, Callable[[], BaseException]]:
    """One fresh, key-bearing failure per normalised scripter branch."""
    endpoint = f"https://x/v1beta/models/m:generateContent?key={SECRET_KEY}"
    return {
        "invalid_url": lambda: _real_invalid_url(
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


def _real_invalid_url(selector: str) -> http.client.InvalidURL:
    """Produce a genuine ``http.client.InvalidURL`` via real path validation."""
    connection = http.client.HTTPConnection("api.example.org")
    try:
        connection.putrequest("GET", selector)
    except http.client.InvalidURL as exc:
        return exc
    raise AssertionError("selector must make path validation fail")


def _format_exception(exc_info: Any) -> str:
    record = logging.LogRecord(
        name="scripter.redaction",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=exc_info,
    )
    from telegram_documentaries.logging_config import JsonFormatter

    return JsonFormatter().format(record)


@pytest.mark.parametrize("label", sorted(_script_failures()))
def test_every_script_failure_redacts_the_api_key(label: str) -> None:
    client, _ = make_scripter([_script_failures()[label]()])

    with pytest.raises(ScriptError) as exc_info:
        client.write_script(dossier())

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    json_line = _format_exception((type(error), error, error.__traceback__))

    assert SECRET_KEY not in str(error), f"{label} leaked in str(exc)"
    assert SECRET_KEY not in formatted, f"{label} leaked via traceback"
    assert SECRET_KEY not in json_line, f"{label} leaked via JsonFormatter"
    assert error.__cause__ is None, f"{label} leaked via __cause__"
    assert error.__suppress_context__ is True, f"{label} leaked via __context__"


def test_scripter_redacts_a_control_character_key() -> None:
    key = "SECRET-SCRIPTER-KEY-\nCANARY"
    client, _ = make_scripter(
        [_real_invalid_url(f"/v1beta/models/m:generateContent?key={key}")],
        api_key=key,
    )

    with pytest.raises(ScriptError) as exc_info:
        client.write_script(dossier())

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )

    assert "CANARY" not in str(error)
    assert "CANARY" not in formatted
    assert error.__cause__ is None


def test_unexpected_urlopen_error_is_normalised_and_redacted() -> None:
    client, _ = make_scripter(
        [ValueError(f"unknown url type: https://x?key={SECRET_KEY}")]
    )

    with pytest.raises(ScriptError) as exc_info:
        client.write_script(dossier())

    assert "ValueError" in str(exc_info.value)
    assert SECRET_KEY not in str(exc_info.value)


def test_empty_api_key_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        GeminiScripter("")


def test_script_directive_constants_are_defined() -> None:
    assert SCRIPT_DIRECTIVE.strip()
    assert isinstance(SCRIPT_PERSONA, str)
    assert isinstance(SCRIPT_SCHEMA, dict)


# ------------------------------------------------------------------ routing
#
# ``handle_scripting`` is the Scripter's whole turn: generate the narration,
# send it, then set the terminal flag. Every failure degrades to an apology
# (or a reset-and-reprompt for a broken session) and never raises into the
# polling loop.

SCRIPT = "The caffeinated badger claims its territory and hoards the silver."


def messages(transport: FakeTransport) -> list[str]:
    return [params["text"] for params in transport.calls_for("sendMessage")]


def scripting_store(
    chat_id: int = 1,
    *,
    done: bool = True,
    portrait_sent: bool = True,
    script: str | None = None,
    script_sent: bool = False,
    subject: Dossier | None = None,
) -> SessionStore:
    """A session at the scripting stage of the pipeline.

    ``done`` chats hold the dossier the Scripter consumes (an invariant break
    is modelled by passing ``subject`` such that the dossier is missing while
    ``done`` is still True — callers mutate ``phase`` directly for that).
    """
    store = SessionStore()
    if done:
        store.appoint_dossier(
            chat_id, dossier() if subject is None else subject
        )
    if portrait_sent:
        store.mark_portrait_sent(chat_id)
    if script is not None:
        store.store_script(chat_id, script)
    if script_sent:
        store.mark_script_sent(chat_id)
    return store


class _Capture:
    """Attach a recording handler to a throwaway logger for one call."""

    def __init__(self, name: str, *, level: int = logging.INFO) -> None:
        self.logger = logging.getLogger(name)
        self.records: list[logging.LogRecord] = []
        self.handler = _RecordingHandler(self.records)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(level)

    def close(self) -> None:
        self.logger.removeHandler(self.handler)


def _events(records: list[logging.LogRecord]) -> list[str]:
    return [str(getattr(record, "event", "")) for record in records]


def test_handle_scripting_generates_and_delivers_the_script() -> None:
    session = scripting_store()
    transport = FakeTransport()
    scripter = FakeScripter([SCRIPT])
    capture = _Capture("scripter.flow")
    try:
        delivered = handle_scripting(
            transport, 1, session=session, scripter=scripter,
            logger=capture.logger,
        )
    finally:
        capture.close()

    assert delivered is True
    assert messages(transport) == [SCRIPT]
    assert scripter.calls == [session.get(1).dossier]
    state = session.get(1)
    assert state.script == SCRIPT
    assert state.script_sent is True
    assert _events(capture.records) == [
        "script_started",
        "script_ready",
        "script_sent",
    ]


def test_handle_scripting_flag_is_evidence_not_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = scripting_store()
    transport = FakeTransport()
    scripter = FakeScripter([SCRIPT])
    flag_at_send: list[bool] = []
    original_call = transport.call

    def spy_call(method: str, params: Any = None) -> dict[str, Any]:
        if method == "sendMessage":
            flag_at_send.append(session.get(1).script_sent)
        return original_call(method, params)

    monkeypatch.setattr(transport, "call", spy_call)
    try:
        delivered = handle_scripting(
            transport, 1, session=session, scripter=scripter
        )
    finally:
        monkeypatch.undo()

    assert delivered is True
    assert flag_at_send == [False]
    assert session.get(1).script_sent is True


def test_handle_scripting_never_logs_the_script_or_dossier_text() -> None:
    distinctive = "RINGWORM-BADGER-NARRATION-CANARY inkwell cutlery"
    session = scripting_store()
    transport = FakeTransport()
    scripter = FakeScripter([distinctive])
    capture = _Capture("scripter.quiet")

    try:
        handle_scripting(
            transport, 1, session=session, scripter=scripter,
            logger=capture.logger,
        )
    finally:
        capture.close()

    for record in capture.records:
        message = record.getMessage()
        assert distinctive not in message
        assert "Sleeps eighteen hours" not in message
        for key, value in record.__dict__.items():
            text = str(value)
            assert distinctive not in text


def test_handle_scripting_ready_event_reports_only_word_count_and_animal() -> None:
    session = scripting_store()
    transport = FakeTransport()
    scripter = FakeScripter([SCRIPT])
    capture = _Capture("scripter.ready")

    try:
        handle_scripting(
            transport, 1, session=session, scripter=scripter,
            logger=capture.logger,
        )
    finally:
        capture.close()

    ready = [
        record for record in capture.records
        if getattr(record, "event", "") == "script_ready"
    ]
    assert len(ready) == 1
    extra = ready[0].__dict__
    assert extra["word_count"] == word_count(SCRIPT)
    assert extra["suggested_animal"] == "caffeinated badger"
    assert SCRIPT not in str(extra)


def test_handle_scripting_stores_the_script_even_when_the_send_fails() -> None:
    session = scripting_store()
    transport = FakeTransport(
        fail_on={"sendMessage": TelegramApiError("sendMessage", "blocked")}
    )
    scripter = FakeScripter([SCRIPT])
    capture = _Capture("scripter.sendfail")
    try:
        delivered = handle_scripting(
            transport, 1, session=session, scripter=scripter,
            logger=capture.logger,
        )
    finally:
        capture.close()

    assert delivered is False
    state = session.get(1)
    assert state.script == SCRIPT  # captured, so the next update re-sends it
    assert state.script_sent is False
    # The failed attempt is recorded, then the best-effort apology.
    assert messages(transport)[0] == SCRIPT
    assert messages(transport)[-1] == SCRIPT_APOLOGY_REPLY
    assert "script_failed" in _events(capture.records)
    assert "script_sent" not in _events(capture.records)


def test_handle_scripting_resends_the_stored_script_without_retrying() -> None:
    store = scripting_store(script="The stored narration, verbatim.")
    transport = FakeTransport()
    # Any call would be a bug: a failed delivery must not re-draw from the
    # model (the re-ask pattern guarantees one narration per subject).
    scripter = FakeScripter([ScriptError("must never be called")])
    capture = _Capture("scripter.resend")
    try:
        delivered = handle_scripting(
            transport, 1, session=store, scripter=scripter,
            logger=capture.logger,
        )
    finally:
        capture.close()

    assert delivered is True
    assert scripter.calls == []
    assert messages(transport) == ["The stored narration, verbatim."]
    state = store.get(1)
    assert state.script == "The stored narration, verbatim."
    assert state.script_sent is True
    # A resend is still a *delivery*: logged as sent, not as a fresh script.
    assert "script_ready" not in _events(capture.records)
    assert "script_sent" in _events(capture.records)


def test_handle_scripting_resend_logs_a_debug_marker_without_the_script() -> None:
    stored = "The stored narration, verbatim."
    store = scripting_store(script=stored)
    transport = FakeTransport()
    scripter = FakeScripter([])
    capture = _Capture("scripter.resend.debug", level=logging.DEBUG)
    try:
        delivered = handle_scripting(
            transport, 1, session=store, scripter=scripter,
            logger=capture.logger,
        )
    finally:
        capture.close()

    events = _events(capture.records)
    assert delivered is True
    # The re-send path is distinguishable in the logs but is not a generation.
    assert "script_resend" in events
    assert "script_started" not in events
    assert "script_ready" not in events
    # The stored narration is user-derived content and must never be logged.
    for record in capture.records:
        assert stored not in record.getMessage()
        assert stored not in str(record.__dict__)


def test_handle_scripting_apologises_when_a_resend_fails_too() -> None:
    store = scripting_store(script="The stored narration, verbatim.")
    transport = FakeTransport(
        fail_on={"sendMessage": TelegramApiError("sendMessage", "blocked")}
    )
    scripter = FakeScripter([])
    capture = _Capture("scripter.resendfail")
    try:
        delivered = handle_scripting(
            transport, 1, session=store, scripter=scripter,
            logger=capture.logger,
        )
    finally:
        capture.close()

    assert delivered is False
    state = store.get(1)
    assert state.script == "The stored narration, verbatim."
    assert state.script_sent is False
    assert messages(transport)[-1] == SCRIPT_APOLOGY_REPLY
    assert "script_failed" in _events(capture.records)


def test_handle_scripting_apologises_and_stays_retryable_on_generation_failure() -> (
    None
):
    session = scripting_store()
    transport = FakeTransport()
    scripter = FakeScripter([ScriptError("model refused to narrate")])
    capture = _Capture("scripter.genfail")
    try:
        delivered = handle_scripting(
            transport, 1, session=session, scripter=scripter,
            logger=capture.logger,
        )
    finally:
        capture.close()

    assert delivered is False
    state = session.get(1)
    assert state.script is None
    assert state.script_sent is False
    assert messages(transport) == [SCRIPT_APOLOGY_REPLY]
    assert "script_failed" in _events(capture.records)
    assert "script_ready" not in _events(capture.records)


def test_handle_scripting_never_raises_on_an_unexpected_generation_error() -> None:
    session = scripting_store()
    transport = FakeTransport()
    scripter = FakeScripter([RuntimeError("kaput")])

    delivered = handle_scripting(
        transport, 1, session=session, scripter=scripter
    )

    assert delivered is False
    assert session.get(1).script_sent is False


def test_handle_scripting_lets_keyboard_interrupt_escape() -> None:
    session = scripting_store()
    scripter = FakeScripter([KeyboardInterrupt()])

    with pytest.raises(KeyboardInterrupt):
        handle_scripting(
            FakeTransport(), 1, session=session, scripter=scripter
        )


def test_handle_scripting_skips_a_chat_that_is_not_done() -> None:
    store = scripting_store(done=False)
    transport = FakeTransport()
    scripter = FakeScripter()
    capture = _Capture("scripter.skip")
    try:
        delivered = handle_scripting(
            transport, 1, session=store, scripter=scripter, logger=capture.logger
        )
    finally:
        capture.close()

    assert delivered is False
    assert transport.calls == []
    assert scripter.calls == []
    assert "script_skipped" in _events(capture.records)


def test_handle_scripting_skips_a_chat_whose_portrait_was_not_sent() -> None:
    store = scripting_store(portrait_sent=False)
    transport = FakeTransport()
    scripter = FakeScripter()
    capture = _Capture("scripter.noportrait")
    try:
        delivered = handle_scripting(
            transport, 1, session=store, scripter=scripter, logger=capture.logger
        )
    finally:
        capture.close()

    assert delivered is False
    assert transport.calls == []
    assert "script_skipped" in _events(capture.records)


def test_handle_scripting_skips_a_chat_whose_script_was_already_delivered() -> None:
    store = scripting_store(script="Already spoken.", script_sent=True)
    transport = FakeTransport()
    scripter = FakeScripter()
    capture = _Capture("scripter.alreadysent")
    try:
        delivered = handle_scripting(
            transport, 1, session=store, scripter=scripter, logger=capture.logger
        )
    finally:
        capture.close()

    assert delivered is False
    assert transport.calls == []
    assert scripter.calls == []
    assert "script_skipped" in _events(capture.records)


def test_handle_scripting_resets_and_reprompts_when_the_dossier_is_missing() -> None:
    store = SessionStore()
    state = store.record_pass(1, b"portrait")
    state.phase = Phase.done  # invariant break: done without a dossier
    store.mark_portrait_sent(1)
    transport = FakeTransport()
    scripter = FakeScripter()
    capture = _Capture("scripter.nodossier")
    try:
        delivered = handle_scripting(
            transport, 1, session=store, scripter=scripter, logger=capture.logger
        )
    finally:
        capture.close()

    assert delivered is False
    assert store.get(1).phase is Phase.awaiting_photo
    assert messages(transport) == [SCRIPT_RESET_REPLY]
    failures = [r for r in capture.records if r.levelno >= logging.ERROR]
    assert failures, "a broken session must be logged loudly"
    assert "script_failed" in _events(capture.records)


def test_handle_scripting_truncates_an_oversized_script_before_sending() -> None:
    # Regression guard for the over-long outbound-text bug class: the model is
    # capped at 90 *words*, but a word can be far longer than 4096 code units,
    # so the send chokepoint must bound the message regardless.
    word = "s" * 200
    oversized = " ".join([word] * 40)  # 8000 code units, 40 "words"
    assert len(oversized) > MAX_MESSAGE_LENGTH
    session = scripting_store()
    transport = FakeTransport()
    scripter = FakeScripter([oversized])

    delivered = handle_scripting(
        transport, 1, session=session, scripter=scripter
    )

    assert delivered is True
    calls = transport.calls_for("sendMessage")
    assert len(calls) == 1
    assert message_length(calls[0]["text"]) <= MAX_MESSAGE_LENGTH
    state = session.get(1)
    # The *stored* script stays raw (the Phase 6 narrator synthesises it);
    # only the delivered copy is bounded.
    assert state.script == oversized
    assert state.script_sent is True
