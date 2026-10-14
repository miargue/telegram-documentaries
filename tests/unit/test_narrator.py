"""Behaviour of the Phase 6 Narrator: typed port + Gemini TTS adapter + routing.

``urlopen`` is injected, so every case runs without a live API or a socket and
the tests assert on the outgoing request body: exactly one ``user`` content
holding two text parts — the voice-over guidance (the posh-British deep-voice
persona) and the raw script verbatim — and ``responseModalities: ["AUDIO"]``.
The adapter mechanically enforces the output contract — non-empty ``audio/*``
payload, ≤ ``MAX_NARRATION_BYTES`` — and every failure comes back as a redacted
:class:`NarratorError` (the fifth adapter honouring the single-redaction
guarantee).

``handle_narrating`` is the Narrator's whole turn: for a ``done`` chat whose
script already reached it, synthesise the audio, send it via ``sendVoice``,
then set the terminal flag; a failed *send* re-sends the *stored* bytes with no
re-synthesis, and nothing ever raises into the polling loop.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import traceback
import urllib.error
from collections.abc import Callable
from typing import Any, cast

import pytest
from conftest import (
    FakeNarrator,
    FakeTransport,
    _RecordingHandler,
)

from telegram_documentaries.defaults import (
    DEFAULT_NARRATOR_MODEL,
    DEFAULT_NARRATOR_TIMEOUT,
    DEFAULT_NARRATOR_VOICE,
    MAX_NARRATION_BYTES,
)
from telegram_documentaries.gemini import DEFAULT_BASE_URL, AudioPayload
from telegram_documentaries.narrator import (
    NARRATION_APOLOGY_REPLY,
    NARRATION_RESET_REPLY,
    GeminiNarrator,
    NarratorError,
    handle_narrating,
    validate_audio,
)
from telegram_documentaries.session import Phase, SessionStore
from telegram_documentaries.transport import TelegramApiError

SECRET_KEY = "SECRET-NARRATOR-KEY-CANARY"


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


def make_narrator(
    responses: list[Any],
    *,
    api_key: str = SECRET_KEY,
    **kwargs: Any,
) -> tuple[GeminiNarrator, RecordingUrlopen]:
    opener = RecordingUrlopen(responses)
    narrator = GeminiNarrator(api_key, urlopen=opener, **kwargs)
    return narrator, opener


def audio_body(
    data: bytes = b"OggS\x00opus-audio",
    mime_type: str = "audio/ogg; codecs=opus",
) -> bytes:
    """A ``generateContent`` response whose candidate is one inline audio part."""
    payload = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": base64.b64encode(data).decode("ascii"),
                            }
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def sent_body(opener: RecordingUrlopen) -> dict[str, Any]:
    """The JSON request the narrator actually posted."""
    _url, data, _timeout = opener.requests[0]
    assert data is not None
    return json.loads(data)


SCRIPT = "The caffeinated badger claims its territory and hoards the silver."
AUDIO = b"OggS\x00narration-audio-bytes"


# --------------------------------------------------------- request body

def test_synthesize_posts_a_single_user_turn_with_two_text_parts() -> None:
    narrator, opener = make_narrator([FakeResponse(audio_body())])

    narrator.synthesize(SCRIPT)

    body = sent_body(opener)
    contents = body["contents"]
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    parts = contents[0]["parts"]
    assert len(parts) == 2
    # Part 0 is the voice-over guidance; part 1 is the raw script verbatim.
    assert parts[0]["text"] == DEFAULT_NARRATOR_VOICE
    assert parts[1]["text"] == SCRIPT


def test_synthesize_uses_the_configured_voice() -> None:
    narrator, opener = make_narrator(
        [FakeResponse(audio_body())], voice="a gravelly auntie"
    )

    narrator.synthesize(SCRIPT)

    assert sent_body(opener)["contents"][0]["parts"][0]["text"] == (
        "a gravelly auntie"
    )


def test_synthesize_asks_for_audio_modalities_only() -> None:
    narrator, opener = make_narrator([FakeResponse(audio_body())])

    narrator.synthesize(SCRIPT)

    config = sent_body(opener)["generationConfig"]
    assert config == {"responseModalities": ["AUDIO"]}


def test_synthesize_posts_to_the_generate_content_endpoint() -> None:
    narrator, opener = make_narrator([FakeResponse(audio_body())])

    narrator.synthesize(SCRIPT)

    url, data, timeout = opener.requests[0]
    assert opener.methods[0] == "POST"
    assert url == (
        f"{DEFAULT_BASE_URL}/v1beta/models/{DEFAULT_NARRATOR_MODEL}:"
        f"generateContent?key={SECRET_KEY}"
    )
    assert data is not None
    assert timeout == pytest.approx(DEFAULT_NARRATOR_TIMEOUT)


def test_synthesize_uses_the_default_narrator_model() -> None:
    assert DEFAULT_NARRATOR_MODEL == "gemini-3.1-flash-tts-preview"
    narrator, opener = make_narrator([FakeResponse(audio_body())])

    narrator.synthesize(SCRIPT)

    assert (
        f"/v1beta/models/{DEFAULT_NARRATOR_MODEL}:generateContent"
        in opener.requests[0][0]
    )


def test_synthesize_returns_the_decoded_audio_payload() -> None:
    narrator, _ = make_narrator(
        [FakeResponse(audio_body(b"OGG-BYTES", "audio/ogg"))]
    )

    payload = narrator.synthesize(SCRIPT)

    assert isinstance(payload, AudioPayload)
    assert payload.data == b"OGG-BYTES"
    assert payload.mime_type == "audio/ogg"


def test_synthesize_skips_text_parts_and_takes_the_first_audio_part() -> None:
    payload = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "here is your narration"},
                        {
                            "inlineData": {
                                "mimeType": "audio/ogg",
                                "data": base64.b64encode(b"FIRST").decode("ascii"),
                            }
                        },
                        {
                            "inlineData": {
                                "mimeType": "audio/mpeg",
                                "data": base64.b64encode(b"SECOND").decode("ascii"),
                            }
                        },
                    ],
                }
            }
        ]
    }
    narrator, _ = make_narrator([FakeResponse(json.dumps(payload).encode())])

    result = narrator.synthesize(SCRIPT)

    assert result.data == b"FIRST"
    assert result.mime_type == "audio/ogg"


# ---------------------------------------------------- unusable model output

def test_synthesize_rejects_a_non_audio_mime_type() -> None:
    narrator, _ = make_narrator(
        [FakeResponse(audio_body(b"IMG", "image/png"))]
    )

    with pytest.raises(NarratorError) as exc_info:
        narrator.synthesize(SCRIPT)

    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize("mime", ["audio/wav", "audio/mpeg", "text/plain", ""])
def test_validate_audio_rejects_a_non_ogg_payload(mime: str) -> None:
    # The locked format is OGG, so a valid-looking non-OGG audio payload must
    # not be relabelled as ``audio/ogg`` downstream.
    with pytest.raises(NarratorError) as exc_info:
        validate_audio(AudioPayload(data=AUDIO, mime_type=mime))

    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize(
    "mime", ["audio/ogg", "audio/ogg; codecs=opus", "AUDIO/OGG"]
)
def test_validate_audio_accepts_the_locked_ogg_format(mime: str) -> None:
    payload = AudioPayload(data=AUDIO, mime_type=mime)

    assert validate_audio(payload) is payload


def test_synthesize_rejects_an_oversized_audio_payload() -> None:
    too_big = b"x" * (MAX_NARRATION_BYTES + 1)
    narrator, _ = make_narrator([FakeResponse(audio_body(too_big, "audio/ogg"))])

    with pytest.raises(NarratorError):
        narrator.synthesize(SCRIPT)


def test_synthesize_accepts_a_payload_exactly_at_the_size_limit() -> None:
    at_limit = b"x" * MAX_NARRATION_BYTES
    narrator, _ = make_narrator([FakeResponse(audio_body(at_limit, "audio/ogg"))])

    assert len(narrator.synthesize(SCRIPT).data) == MAX_NARRATION_BYTES


def test_synthesize_rejects_an_empty_audio_payload() -> None:
    narrator, _ = make_narrator([FakeResponse(audio_body(b"", "audio/ogg"))])

    with pytest.raises(NarratorError):
        narrator.synthesize(SCRIPT)


@pytest.mark.parametrize("junk", ["not base64!!", "AAAA!"])
def test_synthesize_rejects_non_base64_audio_data(junk: str) -> None:
    payload = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"inlineData": {"mimeType": "audio/ogg", "data": junk}}],
                }
            }
        ]
    }
    narrator, _ = make_narrator([FakeResponse(json.dumps(payload).encode())])

    with pytest.raises(NarratorError):
        narrator.synthesize(SCRIPT)


def test_synthesize_rejects_a_candidate_without_an_audio_part() -> None:
    payload = {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "all done"}]}}
        ]
    }
    narrator, _ = make_narrator([FakeResponse(json.dumps(payload).encode())])

    with pytest.raises(NarratorError):
        narrator.synthesize(SCRIPT)


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
def test_synthesize_normalises_every_failure(failure: Any) -> None:
    narrator, _ = make_narrator(
        [failure if isinstance(failure, BaseException) else FakeResponse(failure)]
    )

    with pytest.raises(NarratorError) as exc_info:
        narrator.synthesize(SCRIPT)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


def _narrator_failures() -> dict[str, Callable[[], BaseException]]:
    """One fresh, key-bearing failure per normalised narrator branch."""
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
    connection = http.client.HTTPConnection("api.example.org")
    try:
        connection.putrequest("GET", selector)
    except http.client.InvalidURL as exc:
        return exc
    raise AssertionError("selector must make path validation fail")


def _format_exception(exc_info: Any) -> str:
    record = logging.LogRecord(
        name="narrator.redaction",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=exc_info,
    )
    from telegram_documentaries.logging_config import JsonFormatter

    return JsonFormatter().format(record)


@pytest.mark.parametrize("label", sorted(_narrator_failures()))
def test_every_narrator_failure_redacts_the_api_key(label: str) -> None:
    narrator, _ = make_narrator([_narrator_failures()[label]()])

    with pytest.raises(NarratorError) as exc_info:
        narrator.synthesize(SCRIPT)

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


def test_narrator_redacts_a_control_character_key() -> None:
    key = "SECRET-NARRATOR-KEY-\nCANARY"
    narrator, _ = make_narrator(
        [_real_invalid_url(f"/v1beta/models/m:generateContent?key={key}")],
        api_key=key,
    )

    with pytest.raises(NarratorError) as exc_info:
        narrator.synthesize(SCRIPT)

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )

    assert "CANARY" not in str(error)
    assert "CANARY" not in formatted
    assert error.__cause__ is None


def test_empty_api_key_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        GeminiNarrator("")


# ------------------------------------------------------------------ routing

def messages(transport: FakeTransport) -> list[str]:
    return [params["text"] for params in transport.calls_for("sendMessage")]


def narrating_store(
    chat_id: int = 1,
    *,
    done: bool = True,
    portrait_sent: bool = True,
    script: str | None = None,
    script_sent: bool = True,
    narration: bytes | None = None,
    narrator_sent: bool = False,
) -> SessionStore:
    """A session at the narrating stage of the pipeline.

    ``done`` chats hold the dossier and reached the scripting stage; pass
    ``script_sent=True`` to model the normal handoff (the script landed before
    the Narrator runs) and pre-set ``narration`` to model a failed send.
    """
    from telegram_documentaries.session import Dossier

    store = SessionStore()
    if done:
        store.record_pass(chat_id, b"PORTRAIT")
        store.appoint_dossier(
            chat_id, Dossier(summary="S", suggested_animal="Capybara")
        )
    else:
        store.record_pass(chat_id, b"PORTRAIT")
    if portrait_sent:
        store.mark_portrait_sent(chat_id)
    if script is not None:
        store.store_script(chat_id, script)
    if script_sent:
        store.mark_script_sent(chat_id)
    if narration is not None:
        store.store_narration(chat_id, narration)
    if narrator_sent:
        store.mark_narrator_sent(chat_id)
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


def test_handle_narrating_synthesises_and_sends_the_voice_note() -> None:
    session = narrating_store(script=SCRIPT)
    transport = FakeTransport()
    narrator = FakeNarrator([AudioPayload(data=AUDIO, mime_type="audio/ogg")])
    capture = _Capture("narrator.flow")
    try:
        delivered = handle_narrating(
            transport, 1, session=session, narrator=narrator,
            logger=capture.logger,
        )
    finally:
        capture.close()

    assert delivered is True
    assert narrator.calls == [SCRIPT]
    assert len(transport.voices) == 1
    assert transport.voices[0]["audio"] == AUDIO
    assert transport.voices[0]["media_type"] == "audio/ogg"
    assert transport.voices[0]["filename"] == "narration.ogg"
    state = session.get(1)
    assert state.narration == AUDIO
    assert state.narrator_sent is True
    assert _events(capture.records) == [
        "narration_started",
        "narrator_ready",
        "narration_sent",
    ]


def test_handle_narrating_normalises_the_model_mime_before_send_voice() -> None:
    session = narrating_store(script=SCRIPT)
    transport = FakeTransport()
    narrator = FakeNarrator(
        [AudioPayload(data=AUDIO, mime_type="audio/ogg; codecs=opus")]
    )

    delivered = handle_narrating(
        transport, 1, session=session, narrator=narrator
    )

    assert delivered is True
    assert len(transport.voices) == 1
    assert transport.voices[0]["audio"] == AUDIO
    # The locked format is a clean OGG voice note, never the model's decorated
    # ``audio/ogg; codecs=opus`` value.
    assert transport.voices[0]["media_type"] == "audio/ogg"
    assert transport.voices[0]["filename"] == "narration.ogg"
    assert session.get(1).narrator_sent is True


def test_handle_narrating_flag_is_evidence_not_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = narrating_store(script=SCRIPT)
    transport = FakeTransport()
    narrator = FakeNarrator([AudioPayload(data=AUDIO, mime_type="audio/ogg")])
    flag_at_send: list[bool] = []
    original = transport.send_voice

    def spy_send_voice(
        chat_id: int, audio: bytes, *, media_type: str, filename: str = "narration.ogg"
    ) -> dict[str, Any]:
        flag_at_send.append(session.get(1).narrator_sent)
        return original(
            chat_id, audio, media_type=media_type, filename=filename
        )

    monkeypatch.setattr(transport, "send_voice", spy_send_voice)
    try:
        delivered = handle_narrating(
            transport, 1, session=session, narrator=narrator
        )
    finally:
        monkeypatch.undo()

    assert delivered is True
    assert flag_at_send == [False]
    assert session.get(1).narrator_sent is True


def test_handle_narrating_never_logs_the_script_or_audio() -> None:
    distinctive = "RINGWORM-BADGER-NARRATION-CANARY inkwell cutlery"
    audio = b"OggS\x00DISTINCTIVE-AUDIO-CANARY"
    session = narrating_store(script=distinctive)
    transport = FakeTransport()
    narrator = FakeNarrator([AudioPayload(data=audio, mime_type="audio/ogg")])
    capture = _Capture("narrator.quiet")
    try:
        handle_narrating(
            transport, 1, session=session, narrator=narrator,
            logger=capture.logger,
        )
    finally:
        capture.close()

    for record in capture.records:
        assert distinctive not in record.getMessage()
        assert "DISTINCTIVE-AUDIO-CANARY" not in str(record.__dict__)
        for value in record.__dict__.values():
            assert distinctive not in str(value)
            assert "DISTINCTIVE-AUDIO-CANARY" not in str(value)


def test_handle_narrating_ready_event_reports_only_byte_count_and_mime() -> None:
    session = narrating_store(script=SCRIPT)
    transport = FakeTransport()
    narrator = FakeNarrator(
        [AudioPayload(data=AUDIO, mime_type="audio/ogg; codecs=opus")]
    )
    capture = _Capture("narrator.ready")
    try:
        handle_narrating(
            transport, 1, session=session, narrator=narrator,
            logger=capture.logger,
        )
    finally:
        capture.close()

    ready = [
        record for record in capture.records
        if getattr(record, "event", "") == "narrator_ready"
    ]
    assert len(ready) == 1
    extra = ready[0].__dict__
    assert extra["byte_count"] == len(AUDIO)
    # The ready log reports the clean locked-format media type, not the
    # model's decorated value (the drift the send path already normalised).
    assert extra["mime"] == "audio/ogg"
    assert SCRIPT not in str(extra)
    assert "DISTINCTIVE" not in str(extra)


def test_handle_narrating_stores_the_audio_even_when_the_send_fails() -> None:
    session = narrating_store(script=SCRIPT)
    transport = FakeTransport(
        fail_on={"sendVoice": TelegramApiError("sendVoice", "blocked")}
    )
    narrator = FakeNarrator([AudioPayload(data=AUDIO, mime_type="audio/ogg")])
    capture = _Capture("narrator.sendfail")
    try:
        delivered = handle_narrating(
            transport, 1, session=session, narrator=narrator,
            logger=capture.logger,
        )
    finally:
        capture.close()

    assert delivered is False
    state = session.get(1)
    assert state.narration == AUDIO  # captured, so the next update re-sends it
    assert state.narrator_sent is False
    assert messages(transport)[-1] == NARRATION_APOLOGY_REPLY
    assert "narrator_failed" in _events(capture.records)
    assert "narration_sent" not in _events(capture.records)


def test_handle_narrating_resends_the_stored_audio_without_resynthesising() -> None:
    stored = b"OggS\x00stored-audio-verbatim"
    session = narrating_store(script=SCRIPT, narration=stored)
    transport = FakeTransport()
    # Any call would be a bug: a failed delivery must not re-synthesise.
    narrator = FakeNarrator([NarratorError("must never be called")])
    capture = _Capture("narrator.resend")
    try:
        delivered = handle_narrating(
            transport, 1, session=session, narrator=narrator,
            logger=capture.logger,
        )
    finally:
        capture.close()

    assert delivered is True
    assert narrator.calls == []
    assert len(transport.voices) == 1
    assert transport.voices[0]["audio"] == stored
    state = session.get(1)
    assert state.narration == stored
    assert state.narrator_sent is True
    # A resend is still a *delivery*: logged as sent, not as a fresh synthesis.
    assert "narrator_ready" not in _events(capture.records)
    assert "narration_started" not in _events(capture.records)
    assert "narration_sent" in _events(capture.records)


def test_handle_narrating_stored_resend_uses_the_clean_ogg_media_type() -> None:
    stored = b"OggS\x00stored-audio-verbatim"
    session = narrating_store(script=SCRIPT, narration=stored)
    transport = FakeTransport()
    narrator = FakeNarrator([NarratorError("must never be called")])

    delivered = handle_narrating(
        transport, 1, session=session, narrator=narrator
    )

    assert delivered is True
    assert narrator.calls == []  # stored bytes re-sent, never re-synthesised
    assert len(transport.voices) == 1
    assert transport.voices[0]["audio"] == stored
    assert transport.voices[0]["media_type"] == "audio/ogg"


def test_handle_narrating_stored_resend_never_logs_the_audio() -> None:
    stored = b"OggS\x00stored-audio-verbatim-CANARY"
    session = narrating_store(script=SCRIPT, narration=stored)
    transport = FakeTransport()
    narrator = FakeNarrator([])
    capture = _Capture("narrator.resend.quiet")
    try:
        handle_narrating(
            transport, 1, session=session, narrator=narrator,
            logger=capture.logger,
        )
    finally:
        capture.close()

    for record in capture.records:
        assert "stored-audio-verbatim-CANARY" not in str(record.__dict__)
        assert "stored-audio-verbatim-CANARY" not in record.getMessage()


def test_handle_narrating_apologises_when_a_resend_fails_too() -> None:
    stored = b"OggS\x00stored-audio"
    session = narrating_store(script=SCRIPT, narration=stored)
    transport = FakeTransport(
        fail_on={"sendVoice": TelegramApiError("sendVoice", "blocked")}
    )
    narrator = FakeNarrator([])
    capture = _Capture("narrator.resendfail")
    try:
        delivered = handle_narrating(
            transport, 1, session=session, narrator=narrator,
            logger=capture.logger,
        )
    finally:
        capture.close()

    assert delivered is False
    state = session.get(1)
    assert state.narration == stored
    assert state.narrator_sent is False
    assert messages(transport)[-1] == NARRATION_APOLOGY_REPLY
    assert "narrator_failed" in _events(capture.records)


def test_handle_narrating_apologises_and_stays_retryable_on_synthesis_failure() -> (
    None
):
    session = narrating_store(script=SCRIPT)
    transport = FakeTransport()
    narrator = FakeNarrator([NarratorError("model refused to speak")])
    capture = _Capture("narrator.genfail")
    try:
        delivered = handle_narrating(
            transport, 1, session=session, narrator=narrator,
            logger=capture.logger,
        )
    finally:
        capture.close()

    assert delivered is False
    state = session.get(1)
    assert state.narration is None
    assert state.narrator_sent is False
    assert messages(transport) == [NARRATION_APOLOGY_REPLY]
    assert "narrator_failed" in _events(capture.records)
    assert "narrator_ready" not in _events(capture.records)


def test_handle_narrating_apologises_on_an_invalid_synthesised_payload() -> None:
    session = narrating_store(script=SCRIPT)
    transport = FakeTransport()
    # The adapter must never hand back an unusable payload; a fake that does is
    # still treated as a degraded synthesis (nothing stored, no flag).
    narrator = FakeNarrator([AudioPayload(data=b"", mime_type="image/png")])
    delivered = handle_narrating(transport, 1, session=session, narrator=narrator)

    assert delivered is False
    assert session.get(1).narrator_sent is False
    assert messages(transport) == [NARRATION_APOLOGY_REPLY]


def test_handle_narrating_never_raises_on_an_unexpected_synthesis_error() -> None:
    session = narrating_store(script=SCRIPT)
    transport = FakeTransport()
    narrator = FakeNarrator([RuntimeError("kaput")])

    delivered = handle_narrating(
        transport, 1, session=session, narrator=narrator
    )

    assert delivered is False
    assert session.get(1).narrator_sent is False


def test_handle_narrating_lets_keyboard_interrupt_escape() -> None:
    session = narrating_store(script=SCRIPT)
    narrator = FakeNarrator([KeyboardInterrupt()])

    with pytest.raises(KeyboardInterrupt):
        handle_narrating(
            FakeTransport(), 1, session=session, narrator=narrator
        )


def test_handle_narrating_lets_system_exit_escape() -> None:
    # Like KeyboardInterrupt, SystemExit must never be swallowed as a degraded
    # synthesis: a shutdown request has to propagate to the process.
    session = narrating_store(script=SCRIPT)
    narrator = FakeNarrator([SystemExit(1)])

    with pytest.raises(SystemExit):
        handle_narrating(
            FakeTransport(), 1, session=session, narrator=narrator
        )


def test_handle_narrating_skips_a_chat_that_is_not_done() -> None:
    store = narrating_store(done=False, script=None, script_sent=False)
    transport = FakeTransport()
    narrator = FakeNarrator()
    capture = _Capture("narrator.skip")
    try:
        delivered = handle_narrating(
            transport, 1, session=store, narrator=narrator, logger=capture.logger
        )
    finally:
        capture.close()

    assert delivered is False
    assert transport.calls == []
    assert narrator.calls == []
    assert "narration_skipped" in _events(capture.records)


def test_handle_narrating_skips_a_chat_whose_script_was_not_sent() -> None:
    store = narrating_store(script=None, script_sent=False)
    transport = FakeTransport()
    narrator = FakeNarrator()
    capture = _Capture("narrator.noscriptsent")
    try:
        delivered = handle_narrating(
            transport, 1, session=store, narrator=narrator, logger=capture.logger
        )
    finally:
        capture.close()

    assert delivered is False
    assert transport.calls == []
    assert "narration_skipped" in _events(capture.records)


def test_handle_narrating_skips_a_chat_whose_narration_was_already_sent() -> None:
    store = narrating_store(
        script=SCRIPT, narration=AUDIO, narrator_sent=True
    )
    transport = FakeTransport()
    narrator = FakeNarrator()
    capture = _Capture("narrator.alreadysent")
    try:
        delivered = handle_narrating(
            transport, 1, session=store, narrator=narrator, logger=capture.logger
        )
    finally:
        capture.close()

    assert delivered is False
    assert transport.calls == []
    assert transport.voices == []
    assert narrator.calls == []
    assert "narration_skipped" in _events(capture.records)


def test_handle_narrating_resets_and_reprompts_when_the_script_is_missing() -> None:
    # Invariant break: done + script_sent but no stored script. Fail loud,
    # reset and re-prompt rather than synthesise from nothing.
    store = narrating_store(script=None, script_sent=True)
    transport = FakeTransport()
    narrator = FakeNarrator()
    capture = _Capture("narrator.nostoredscript")
    try:
        delivered = handle_narrating(
            transport, 1, session=store, narrator=narrator, logger=capture.logger
        )
    finally:
        capture.close()

    assert delivered is False
    assert store.get(1).phase is Phase.awaiting_photo
    assert messages(transport) == [NARRATION_RESET_REPLY]
    failures = [r for r in capture.records if r.levelno >= logging.ERROR]
    assert failures, "a broken session must be logged loudly"
    assert "narrator_failed" in _events(capture.records)
    assert narrator.calls == []
