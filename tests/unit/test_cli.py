"""Behaviour of the CLI entrypoint (`python -m telegram_documentaries`).

Phase 2 wiring: the CLI must resolve both secrets (``TELEGRAM_BOT_TOKEN`` and
``GEMINI_API_KEY``), build one ``SessionStore`` and one ``VisionGate``, and
thread them into the poll loop. No test contacts Telegram or Gemini — the
transport and the gate factory are injected fakes.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FakeConverter,
    FakeGate,
    FakeLLM,
    FakeNarrator,
    FakeScripter,
    FakeTransport,
    photo_update,
    text_update,
)

from telegram_documentaries import defaults, polling
from telegram_documentaries.bouncer import PROMPT_PHOTO, SUCCESS_REPLY
from telegram_documentaries.cli import main, positive_int
from telegram_documentaries.converter import ConverterLLM
from telegram_documentaries.interviewer import InterviewLLM
from telegram_documentaries.logging_config import JsonFormatter
from telegram_documentaries.narrator import NarratorLLM
from telegram_documentaries.scripter import ScripterLLM
from telegram_documentaries.session import Dossier
from telegram_documentaries.transport import TelegramApiError
from telegram_documentaries.vision import HumanVerdict, VisionGate

API_KEY = "test-gemini-key"


class _FlakyGetUpdatesTransport(FakeTransport):
    """Fails ``getUpdates`` once, then serves batches.

    Proves the CLI rides out a *transient* network blip instead of exiting (the
    polling loop retries with backoff; only a bad-token ``getMe`` probe exits).
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.attempts = 0

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "getUpdates":
            self.attempts += 1
            if self.attempts == 1:
                raise TelegramApiError("getUpdates", "network down")
        return super().call(method, params)


def _refuse_transport(token: str) -> Any:
    raise AssertionError(f"transport must not be built: {token}")


def _refuse_gate(api_key: str, model: str) -> VisionGate:
    raise AssertionError(f"gate must not be built: {api_key} / {model}")


def test_main_returns_config_error_when_token_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="telegram_documentaries.cli"):
        exit_code = main(
            ["--max-polls", "1"],
            environ={"GEMINI_API_KEY": API_KEY},
            env_file=tmp_path / "absent.env",
            transport_factory=_refuse_transport,
            gate_factory=_refuse_gate,
        )

    assert exit_code == 2
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert any(
        getattr(record, "event", None) == "config_error"
        for record in caplog.records
    )


def test_main_returns_config_error_when_api_key_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Missing GEMINI_API_KEY must fail fast at startup (exit 2), before the
    # transport is even constructed — the gate can never run keyless.
    with caplog.at_level(logging.ERROR, logger="telegram_documentaries.cli"):
        exit_code = main(
            ["--max-polls", "1"],
            environ={"TELEGRAM_BOT_TOKEN": "token"},
            env_file=tmp_path / "absent.env",
            transport_factory=_refuse_transport,
            gate_factory=_refuse_gate,
        )

    assert exit_code == 2
    assert any(
        getattr(record, "event", None) == "config_error"
        for record in caplog.records
    )


def test_main_builds_the_gate_from_the_api_key_and_default_model(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(batches=[[text_update(1, chat_id=5)], []])
    built: list[tuple[str, str]] = []
    gate = FakeGate()

    def gate_factory(api_key: str, model: str) -> VisionGate:
        built.append((api_key, model))
        return gate

    exit_code = main(
        ["--max-polls", "2"],
        environ={"TELEGRAM_BOT_TOKEN": "token", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: transport,
        gate_factory=gate_factory,
    )

    assert exit_code == 0
    assert built == [(API_KEY, "gemini-3.1-flash-lite")]


def test_main_uses_the_vision_model_override(tmp_path: Path) -> None:
    built: list[tuple[str, str]] = []

    def gate_factory(api_key: str, model: str) -> VisionGate:
        built.append((api_key, model))
        return FakeGate()

    main(
        ["--max-polls", "1"],
        environ={
            "TELEGRAM_BOT_TOKEN": "token",
            "GEMINI_API_KEY": API_KEY,
            "GEMINI_VISION_MODEL": "gemini-custom",
        },
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
        gate_factory=gate_factory,
    )

    assert built == [(API_KEY, "gemini-custom")]


def test_main_threads_the_gate_into_the_poll_loop(tmp_path: Path) -> None:
    # A photo that the injected gate accepts proves the gate built at startup
    # is the one the handler actually calls.
    transport = FakeTransport(
        batches=[[photo_update(1, chat_id=5)], []],
        files={"abc123": ("photos/a.jpg", b"portrait")},
    )
    gate = FakeGate([HumanVerdict(is_human=True)])

    exit_code = main(
        ["--max-polls", "2"],
        environ={"TELEGRAM_BOT_TOKEN": "token", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: transport,
        gate_factory=lambda api_key, model: gate,
        interview_factory=lambda api_key, model: FakeLLM(),
    )

    assert exit_code == 0
    assert gate.calls == 1
    assert [message["text"] for message in transport.calls_for("sendMessage")] == [
        SUCCESS_REPLY,
        "Q1",
    ]


def test_main_builds_the_interviewer_from_the_api_key_and_default_model(
    tmp_path: Path,
) -> None:
    built: list[tuple[str, str]] = []

    def interview_factory(api_key: str, model: str) -> InterviewLLM:
        built.append((api_key, model))
        return FakeLLM()

    exit_code = main(
        ["--max-polls", "1"],
        environ={"TELEGRAM_BOT_TOKEN": "token", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
        gate_factory=lambda api_key, model: FakeGate(),
        interview_factory=interview_factory,
    )

    assert exit_code == 0
    assert built == [(API_KEY, "gemini-3.1-flash-lite")]


def test_main_uses_the_interview_model_override(tmp_path: Path) -> None:
    built: list[tuple[str, str]] = []

    def interview_factory(api_key: str, model: str) -> InterviewLLM:
        built.append((api_key, model))
        return FakeLLM()

    main(
        ["--max-polls", "1"],
        environ={
            "TELEGRAM_BOT_TOKEN": "token",
            "GEMINI_API_KEY": API_KEY,
            "GEMINI_INTERVIEW_MODEL": "gemini-interview-custom",
        },
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
        gate_factory=lambda api_key, model: FakeGate(),
        interview_factory=interview_factory,
    )

    assert built == [(API_KEY, "gemini-interview-custom")]


def test_main_threads_the_interviewer_into_the_poll_loop(tmp_path: Path) -> None:
    # A portrait passing the gate must reach *this* interviewer: the success
    # line is immediately followed by question 1 from the injected adapter.
    transport = FakeTransport(
        batches=[[photo_update(1, chat_id=5)], []],
        files={"abc123": ("photos/a.jpg", b"portrait")},
    )
    gate = FakeGate([HumanVerdict(is_human=True)])
    llm = FakeLLM()

    exit_code = main(
        ["--max-polls", "2"],
        environ={"TELEGRAM_BOT_TOKEN": "token", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: transport,
        gate_factory=lambda api_key, model: gate,
        interview_factory=lambda api_key, model: llm,
    )

    assert exit_code == 0
    assert [message["text"] for message in transport.calls_for("sendMessage")] == [
        SUCCESS_REPLY,
        "Q1",
    ]
    assert llm.next_calls == [[]]


def test_main_runs_the_bouncer_loop_with_the_configured_token(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=env-file-token\n"
        f"GEMINI_API_KEY={API_KEY}\n",
        encoding="utf-8",
    )
    transport = FakeTransport(batches=[[text_update(1, chat_id=5)], []])
    seen_tokens: list[str] = []

    def factory(token: str) -> FakeTransport:
        seen_tokens.append(token)
        return transport

    exit_code = main(
        ["--max-polls", "2"],
        environ={},
        env_file=env_file,
        transport_factory=factory,
        gate_factory=lambda api_key, model: FakeGate(),
    )

    assert exit_code == 0
    assert seen_tokens == ["env-file-token"]
    replies = transport.calls_for("sendMessage")
    assert [reply["text"] for reply in replies] == [PROMPT_PHOTO]
    assert len(transport.calls_for("getUpdates")) == 2


def test_main_reads_token_from_environment_first(tmp_path: Path) -> None:
    transport = FakeTransport(batches=[[]])
    seen_tokens: list[str] = []

    def factory(token: str) -> FakeTransport:
        seen_tokens.append(token)
        return transport

    exit_code = main(
        ["--max-polls", "1"],
        environ={"TELEGRAM_BOT_TOKEN": "env-token", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=factory,
        gate_factory=lambda api_key, model: FakeGate(),
    )

    assert exit_code == 0
    assert seen_tokens == ["env-token"]


def test_main_installs_structured_logging(tmp_path: Path) -> None:
    main(
        ["--max-polls", "1"],
        environ={"TELEGRAM_BOT_TOKEN": "t", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
        gate_factory=lambda api_key, model: FakeGate(),
    )

    root = logging.getLogger()
    assert any(
        isinstance(handler.formatter, JsonFormatter) for handler in root.handlers
    )


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_positive_int_type_rejects_non_positive_or_non_integer(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        positive_int(value)


def test_positive_int_type_accepts_one_and_above() -> None:
    assert positive_int("1") == 1
    assert positive_int("7") == 7


def test_main_rejects_max_polls_zero_with_json_usage_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # '0' used to be accepted and then silently no-op (the loop ran zero times
    # and exited 0), hiding typos in smoke runs.
    with caplog.at_level(logging.ERROR, logger="telegram_documentaries.cli"):
        with pytest.raises(SystemExit) as exc_info:
            main(["--max-polls", "0"], environ={}, env_file=None)

    assert exc_info.value.code == 2
    assert any(
        getattr(record, "event", None) == "cli_usage_error"
        for record in caplog.records
    )


def test_main_rejects_unknown_log_level() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--log-level", "CHATTY"], environ={}, env_file=None)

    assert exc_info.value.code == 2


def test_main_turns_keyboard_interrupt_into_a_clean_structured_stop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    transport = FakeTransport(fail_on={"getUpdates": KeyboardInterrupt()})

    with caplog.at_level(logging.INFO, logger="telegram_documentaries.cli"):
        exit_code = main(
            ["--max-polls", "1"],
            environ={"TELEGRAM_BOT_TOKEN": "t", "GEMINI_API_KEY": API_KEY},
            env_file=tmp_path / "absent.env",
            transport_factory=lambda token: transport,
            gate_factory=lambda api_key, model: FakeGate(),
        )

    assert exit_code == 130  # 128 + SIGINT, documented in the README
    stops = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "gateway_stopped"
    ]
    assert stops, "Ctrl-C must emit the same structured gateway_stopped event"
    assert getattr(stops[0], "reason", None) == "keyboard_interrupt"
    assert not any(record.exc_info for record in caplog.records), (
        "a deliberate Ctrl-C stop must not dump a raw traceback"
    )


def test_main_probes_get_me_first_and_logs_the_bot_username(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    transport = FakeTransport(
        batches=[[]], me={"id": 42, "username": "docs_bot", "first_name": "Docs"}
    )

    with caplog.at_level(logging.INFO, logger="telegram_documentaries.cli"):
        exit_code = main(
            ["--max-polls", "1"],
            environ={"TELEGRAM_BOT_TOKEN": "t", "GEMINI_API_KEY": API_KEY},
            env_file=tmp_path / "absent.env",
            transport_factory=lambda token: transport,
            gate_factory=lambda api_key, model: FakeGate(),
        )

    assert exit_code == 0
    assert transport.calls[0][0] == "getMe", "getMe must run before the poll loop"
    starting = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "gateway_starting"
    ]
    assert starting
    assert getattr(starting[0], "bot_username") == "docs_bot"


def test_main_exits_non_zero_when_the_get_me_probe_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    transport = FakeTransport(
        fail_on={"getMe": TelegramApiError("getMe", "Unauthorized")}
    )

    with caplog.at_level(logging.ERROR, logger="telegram_documentaries.cli"):
        exit_code = main(
            ["--max-polls", "1"],
            environ={"TELEGRAM_BOT_TOKEN": "bad-token", "GEMINI_API_KEY": API_KEY},
            env_file=tmp_path / "absent.env",
            transport_factory=lambda token: transport,
            gate_factory=lambda api_key, model: FakeGate(),
        )

    assert exit_code == 1
    assert any(
        getattr(record, "event", None) == "get_me_failed"
        for record in caplog.records
    )
    # A bad token must surface immediately, never on the first getUpdates.
    assert transport.calls_for("getUpdates") == []


def test_main_turns_keyboard_interrupt_during_the_probe_into_a_clean_stop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    transport = FakeTransport(fail_on={"getMe": KeyboardInterrupt()})

    with caplog.at_level(logging.INFO, logger="telegram_documentaries.cli"):
        exit_code = main(
            ["--max-polls", "1"],
            environ={"TELEGRAM_BOT_TOKEN": "t", "GEMINI_API_KEY": API_KEY},
            env_file=tmp_path / "absent.env",
            transport_factory=lambda token: transport,
            gate_factory=lambda api_key, model: FakeGate(),
        )

    assert exit_code == 130
    assert any(
        getattr(record, "event", None) == "gateway_stopped"
        and getattr(record, "reason", None) == "keyboard_interrupt"
        for record in caplog.records
    )
    assert transport.calls_for("getUpdates") == []


def test_main_rides_out_a_transient_get_updates_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Phase 7: a momentary getUpdates failure no longer kills the gateway — the
    # poll loop retries with backoff and the run completes. Only the startup
    # getMe probe (a bad token) exits non-zero.
    monkeypatch.setattr(polling, "RETRY_BASE_DELAY", 0.0)
    transport = _FlakyGetUpdatesTransport(batches=[[text_update(1, chat_id=5)]])

    exit_code = main(
        ["--max-polls", "1"],
        environ={"TELEGRAM_BOT_TOKEN": "t", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: transport,
        gate_factory=lambda api_key, model: FakeGate(),
    )

    assert exit_code == 0
    # One failed attempt, one retry that succeeded and was handled normally.
    assert transport.attempts == 2
    assert [m["text"] for m in transport.calls_for("sendMessage")] == [PROMPT_PHOTO]


def test_main_builds_the_converter_from_the_api_key_model_and_timeout(
    tmp_path: Path,
) -> None:
    built: list[tuple[str, str, float]] = []

    def converter_factory(
        api_key: str, model: str, timeout: float
    ) -> ConverterLLM:
        built.append((api_key, model, timeout))
        return FakeConverter()

    exit_code = main(
        ["--max-polls", "1"],
        environ={"TELEGRAM_BOT_TOKEN": "token", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
        gate_factory=lambda api_key, model: FakeGate(),
        converter_factory=converter_factory,
    )

    assert exit_code == 0
    assert built == [(API_KEY, "gemini-3.1-flash-image", 120.0)]


def test_main_uses_the_converter_model_and_timeout_overrides(
    tmp_path: Path,
) -> None:
    built: list[tuple[str, str, float]] = []

    def converter_factory(
        api_key: str, model: str, timeout: float
    ) -> ConverterLLM:
        built.append((api_key, model, timeout))
        return FakeConverter()

    main(
        ["--max-polls", "1"],
        environ={
            "TELEGRAM_BOT_TOKEN": "token",
            "GEMINI_API_KEY": API_KEY,
            "GEMINI_CONVERTER_MODEL": "gemini-image-custom",
            "GEMINI_CONVERTER_TIMEOUT": "30",
        },
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
        gate_factory=lambda api_key, model: FakeGate(),
        converter_factory=converter_factory,
    )

    assert built == [(API_KEY, "gemini-image-custom", 30.0)]


def test_main_builds_the_scripter_from_the_api_key_and_default_model(
    tmp_path: Path,
) -> None:
    built: list[tuple[str, str]] = []

    def scripter_factory(api_key: str, model: str) -> ScripterLLM:
        built.append((api_key, model))
        return FakeScripter()

    exit_code = main(
        ["--max-polls", "1"],
        environ={"TELEGRAM_BOT_TOKEN": "token", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
        gate_factory=lambda api_key, model: FakeGate(),
        scripter_factory=scripter_factory,
    )

    assert exit_code == 0
    assert built == [(API_KEY, "gemini-3.1-flash-lite")]


def test_main_uses_the_script_model_override(tmp_path: Path) -> None:
    built: list[tuple[str, str]] = []

    def scripter_factory(api_key: str, model: str) -> ScripterLLM:
        built.append((api_key, model))
        return FakeScripter()

    main(
        ["--max-polls", "1"],
        environ={
            "TELEGRAM_BOT_TOKEN": "token",
            "GEMINI_API_KEY": API_KEY,
            "GEMINI_SCRIPT_MODEL": "gemini-script-custom",
        },
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
        gate_factory=lambda api_key, model: FakeGate(),
        scripter_factory=scripter_factory,
    )

    assert built == [(API_KEY, "gemini-script-custom")]


def test_main_builds_the_narrator_from_the_api_key_model_voice_and_timeout(
    tmp_path: Path,
) -> None:
    built: list[tuple[str, str, str, float]] = []

    def narrator_factory(
        api_key: str, model: str, voice: str, timeout: float
    ) -> NarratorLLM:
        built.append((api_key, model, voice, timeout))
        return FakeNarrator()

    exit_code = main(
        ["--max-polls", "1"],
        environ={"TELEGRAM_BOT_TOKEN": "token", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
        gate_factory=lambda api_key, model: FakeGate(),
        narrator_factory=narrator_factory,
    )

    assert exit_code == 0
    assert built == [
        (
            API_KEY,
            defaults.DEFAULT_NARRATOR_MODEL,
            defaults.DEFAULT_NARRATOR_VOICE,
            90.0,
        )
    ]


def test_main_uses_the_narrator_model_voice_and_timeout_overrides(
    tmp_path: Path,
) -> None:
    built: list[tuple[str, str, str, float]] = []

    def narrator_factory(
        api_key: str, model: str, voice: str, timeout: float
    ) -> NarratorLLM:
        built.append((api_key, model, voice, timeout))
        return FakeNarrator()

    main(
        ["--max-polls", "1"],
        environ={
            "TELEGRAM_BOT_TOKEN": "token",
            "GEMINI_API_KEY": API_KEY,
            "GEMINI_NARRATOR_MODEL": "gemini-tts-custom",
            "GEMINI_NARRATOR_VOICE": "A custom narrator voice",
            "GEMINI_NARRATOR_TIMEOUT": "45",
        },
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
        gate_factory=lambda api_key, model: FakeGate(),
        narrator_factory=narrator_factory,
    )

    assert built == [(API_KEY, "gemini-tts-custom", "A custom narrator voice", 45.0)]


def test_main_delivers_the_portrait_and_script_end_to_end(tmp_path: Path) -> None:
    # One chat walks the whole pipeline in the poll loop: photo → gate → five
    # answers → dossier → hybrid portrait → narration script. The injected
    # converter and scripter are the ones the CLI built, its image is uploaded
    # exactly once, and the script is generated and delivered in the same update.
    converter = FakeConverter()
    scripter = FakeScripter()
    narrator = FakeNarrator()
    transport = FakeTransport(
        batches=[
            [photo_update(1, chat_id=5)],
            [text_update(2, chat_id=5, text="a1")],
            [text_update(3, chat_id=5, text="a2")],
            [text_update(4, chat_id=5, text="a3")],
            [text_update(5, chat_id=5, text="a4")],
            [text_update(6, chat_id=5, text="a5")],
        ],
        files={"abc123": ("photos/a.jpg", b"portrait-bytes")},
    )

    exit_code = main(
        ["--max-polls", "6"],
        environ={"TELEGRAM_BOT_TOKEN": "token", "GEMINI_API_KEY": API_KEY},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: transport,
        gate_factory=lambda api_key, model: FakeGate(
            [HumanVerdict(is_human=True)]
        ),
        interview_factory=lambda api_key, model: FakeLLM(
            dossiers=[Dossier(summary="S", suggested_animal="Capybara")]
        ),
        converter_factory=lambda api_key, model, timeout: converter,
        scripter_factory=lambda api_key, model: scripter,
        narrator_factory=lambda api_key, model, voice, timeout: narrator,
    )

    assert exit_code == 0
    assert len(transport.photos) == 1
    assert transport.photos[0]["image"] == converter.default.data
    assert len(converter.calls) == 1
    assert len(scripter.calls) == 1
    assert len(narrator.calls) == 1
    assert len(transport.audios) == 1
    assert transport.audios[0]["media_type"] == "audio/mpeg"
    assert transport.audios[0]["filename"] == "narration.mp3"
    texts = [message["text"] for message in transport.calls_for("sendMessage")]
    assert texts[-1] == "A dramatic nature documentary script."
