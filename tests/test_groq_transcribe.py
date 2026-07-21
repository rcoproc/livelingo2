"""Unit tests for Groq STT (mocked HTTP + in-memory WAV via soundfile)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from livelingo.groq_transcribe import GroqSTTError, GroqTranscriber


def test_transcribe_empty(mock_cfg):
    t = GroqTranscriber(mock_cfg, log=lambda *_: None)
    assert t.transcribe(None) == ""
    assert t.transcribe(np.array([], dtype=np.float32)) == ""


def test_encode_wav_roundtrip(mock_cfg):
    t = GroqTranscriber(mock_cfg, log=lambda *_: None)
    audio = np.zeros(1600, dtype=np.float32)  # 0.1s @ 16kHz
    wav = t._encode_wav(audio)
    assert isinstance(wav, (bytes, bytearray))
    assert len(wav) > 44  # WAV header
    assert wav[:4] == b"RIFF"


def test_transcribe_success(mock_cfg):
    with patch.object(GroqTranscriber, "_encode_wav", return_value=b"fake-wav"):
        t = GroqTranscriber(mock_cfg, log=lambda *_: None)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"text": "  hello from groq  "}
        t.session = MagicMock()
        t.session.post.return_value = resp
        audio = np.zeros(1600, dtype=np.float32)
        assert t.transcribe(audio) == "hello from groq"
        t.session.post.assert_called_once()


def test_transcribe_401(mock_cfg):
    with patch.object(GroqTranscriber, "_encode_wav", return_value=b"x"):
        t = GroqTranscriber(mock_cfg, log=lambda *_: None)
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "nope"
        t.session = MagicMock()
        t.session.post.return_value = resp
        with pytest.raises(GroqSTTError, match="401"):
            t.transcribe(np.zeros(100, dtype=np.float32))


def test_transcribe_network_error(mock_cfg):
    import requests as req_lib

    with patch.object(GroqTranscriber, "_encode_wav", return_value=b"x"):
        t = GroqTranscriber(mock_cfg, log=lambda *_: None)
        t.session = MagicMock()
        t.session.post.side_effect = req_lib.Timeout("slow")
        with pytest.raises(GroqSTTError, match="network"):
            t.transcribe(np.zeros(100, dtype=np.float32))
