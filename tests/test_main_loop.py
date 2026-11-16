"""Boucle de main.run : notation de N clips, montage des mieux notés, cache des autres."""
import main
from config import settings
from src.models import Platform, VideoCandidate
from src.storage.drive import State
from src.viral.gemini import ClipBrief


class _Storage:
    _root = "root"

    def __init__(self):
        self.state = State()
        self.uploads = []

    def load_state(self):
        return self.state

    def save_state(self, state):
        pass

    def named_root(self, name):
        return "fr-root"

    def upload(self, path, name, **kw):
        self.uploads.append(name)

    def cleanup_old(self, *a, **k):
        pass


def test_best_of_n(monkeypatch, tmp_path):
    scores = {"a": 60, "b": 95, "c": 30, "d": 80, "e": 70, "f": None}
    cands = [
        VideoCandidate(platform=Platform.TWITCH, source_id=k, url=k, title=k, creator=k,
                       score=90 - i, extra={"language": "fr", "game_name": "Grand Theft Auto VI"})
        for i, k in enumerate(scores)
    ]
    storage = _Storage()
    rendered = []

    monkeypatch.setattr(main, "detect_all", lambda: cands)
    monkeypatch.setattr("src.storage.drive.DriveStorage.from_settings", classmethod(lambda cls: storage))
    monkeypatch.setattr(settings, "validate", lambda blocks: None)
    monkeypatch.setattr(settings, "MARKETS", ["fr"])
    monkeypatch.setattr(settings, "CLIPS_PER_MARKET", 2)
    monkeypatch.setattr(settings, "BRIEFS_PER_MARKET", 5)
    monkeypatch.setattr(settings, "CLIP_MIN_SCORE", 55.0)
    monkeypatch.setattr(settings, "NOTIFY_EMAIL_ENABLE", False)
    monkeypatch.setattr(settings, "WORK_DIR", tmp_path)
    monkeypatch.setattr(
        main, "_brief_clip",
        lambda cand, state, market, recent: (ClipBrief(hook=f"hook {cand.source_id}", score=scores[cand.source_id]), None),
    )

    def fake_render(cand, brief, dl, storage_, state, market, root):
        rendered.append(cand.source_id)
        state.published.append({"uid": cand.uid, "hook": brief.hook, "market": market, "clip": "x.mp4"})
        return "ok"

    monkeypatch.setattr(main, "_render_clip", fake_render)

    assert main.run() == 0
    # a,b,c,d,e notés : c rejeté (30) ; shortlist = a,b,d,e ; top 2 = b (95), d (80)
    assert rendered == ["b", "d"]
    st = storage.state
    assert st.is_processed("twitch:b") and st.is_processed("twitch:d") and st.is_processed("twitch:c")
    assert not st.is_processed("twitch:a")
    # a et e : bons mais non montés => brief en cache pour le cycle suivant
    assert set(st.briefs) == {"twitch:a", "twitch:e"}
    # f : pas de note LLM => ni monté, ni grillé, ni mis en cache
    assert not st.is_processed("twitch:f") and "twitch:f" not in st.briefs
    assert "rapport.md" in storage.uploads
