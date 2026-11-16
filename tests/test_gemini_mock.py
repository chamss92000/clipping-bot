"""analyze_clip avec un faux client Gemini : repli de modèle, parsing, choix du titre, trim."""
import json
from types import SimpleNamespace

import pytest
from google.genai import errors

from config import settings
from src.viral import gemini


class _FakeModels:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        out = self.script[model]
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(parsed=None, text=json.dumps(out))


@pytest.fixture
def fake(monkeypatch, tmp_path):
    payload = {
        "what_happens": "Le streamer vole un jet et s'écrase sur Vice City.",
        "score": 82, "start": 3.2, "end": 27.0,
        "hooks": [
            {"text": "J'ai volé un jet", "formula": "aveu"},
            {"text": "Il vole un jet dans GTA 6 😱", "formula": "situation"},
            {"text": "Quand le jet tombe sur Vice City", "formula": "quand"},
        ],
        "best": 0,
        "description": "Squeezie vole un jet dans GTA 6 et finit à Vice City",
        "hashtags": ["#gta6", "#jet", "#fyp", "#squeezie"],
    }
    script = {
        "model-a": errors.ClientError(404, {"error": {"code": 404, "message": "not found"}}),
        "model-b": payload,
    }
    models = _FakeModels(script)
    monkeypatch.setattr("google.genai.Client", lambda api_key: SimpleNamespace(models=models))
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "x")
    monkeypatch.setattr(settings, "GEMINI_MODELS", ["model-a", "model-b"])
    monkeypatch.setattr(settings, "GEMINI_SEND_VIDEO", False)
    monkeypatch.setattr(settings, "GEMINI_MIN_INTERVAL_S", 0.0)
    return models


def test_analyze_clip_fallback_and_selection(fake):
    b = gemini.analyze_clip(
        "unused.mp4", 30.0, title="t", creator="Squeezie",
        game="Grand Theft Auto VI", lang="fr", recent_hooks=[],
    )
    assert fake.calls == ["model-a", "model-b"]
    assert b.model == "model-b"
    assert b.hook == "Il vole un jet dans GTA 6"     # "J'ai…" rejeté (1re personne)
    assert b.score == 82
    assert (b.start, b.end) == (3.2, 27.0)
    assert b.hashtags[:2] == ["#GTA6", "#squeezie"]
    assert "Vice City" in b.description


def test_analyze_clip_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(settings, "GEMINI_SEND_VIDEO", False)
    b = gemini.analyze_clip("x.mp4", 30.0, title="gros fail en voiture", creator="a",
                            game="Grand Theft Auto VI", lang="fr")
    assert b.score is None and b.hook == "Gros fail en voiture"
