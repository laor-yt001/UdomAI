import types
import unittest
from unittest.mock import patch

import converter


class WhisperWord:
    def __init__(self, start, end, word):
        self.start = start
        self.end = end
        self.word = word


class WhisperSegment:
    def __init__(self, start, end, text, words=None):
        self.start = start
        self.end = end
        self.text = text
        self.words = words


class FakeWhisperModel:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def transcribe(self, _path, **options):
        self.calls.append(options)
        return iter(next(self.results)), types.SimpleNamespace()


class DubbingTranscriptionTests(unittest.TestCase):
    def test_groups_words_into_natural_phrase_lengths(self):
        words = [
            {"start": 0.0, "end": 0.3, "word": "Hello"},
            {"start": 0.4, "end": 0.7, "word": "there."},
            {"start": 1.0, "end": 1.2, "word": "How"},
            {"start": 1.3, "end": 1.5, "word": "are"},
            {"start": 1.6, "end": 1.9, "word": "you?"},
            {"start": 4.0, "end": 4.3, "word": "Goodbye"},
        ]

        result = converter._group_whisper_words(words)

        self.assertEqual([item["text"] for item in result], [
            "Hello there.",
            "How are you?",
            "Goodbye",
        ])
        self.assertEqual(result[0]["start"], 0.0)
        self.assertEqual(result[0]["end"], 0.7)

    def test_uses_vad_and_stronger_beam_search(self):
        model = FakeWhisperModel([[
            WhisperSegment(
                0.0,
                1.0,
                "Hello there.",
                [WhisperWord(0.0, 0.4, "Hello"), WhisperWord(0.5, 1.0, "there.")],
            )
        ]])
        with patch.object(converter, "_get_whisper_model", return_value=model):
            result = converter.transcribe_with_whisper_timestamps("video.mp4")

        self.assertEqual(result[0]["text"], "Hello there.")
        self.assertTrue(model.calls[0]["vad_filter"])
        self.assertEqual(model.calls[0]["beam_size"], 5)
        self.assertEqual(model.calls[0]["vad_parameters"]["speech_pad_ms"], 300)

    def test_retries_without_vad_when_vad_finds_no_speech(self):
        model = FakeWhisperModel([
            [],
            [WhisperSegment(0.0, 1.0, "Quiet speech.", None)],
        ])
        with patch.object(converter, "_get_whisper_model", return_value=model):
            result = converter.transcribe_with_whisper_timestamps("video.mp4")

        self.assertEqual(result[0]["text"], "Quiet speech.")
        self.assertTrue(model.calls[0]["vad_filter"])
        self.assertFalse(model.calls[1]["vad_filter"])

    def test_voice_preset_uses_pitch_not_zero_crossing_rate(self):
        fake_librosa = types.SimpleNamespace(
            load=lambda *_args, **_kwargs: ([0.1] * 8000, 16000),
            yin=lambda *_args, **_kwargs: [110.0, 120.0, 125.0, 130.0],
        )
        with patch.dict("sys.modules", {"librosa": fake_librosa}):
            self.assertEqual(converter.detect_voice_presentation_from_audio("voice.wav"), "male")

    def test_auto_whisper_model_is_medium_and_large_v3_is_supported(self):
        self.assertEqual(converter._normalize_whisper_model(), "medium")
        self.assertEqual(converter._normalize_whisper_model("large"), "large-v3")


if __name__ == "__main__":
    unittest.main()
