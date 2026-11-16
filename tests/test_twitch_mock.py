"""twitch.detect avec une fausse API Helix."""
from config import settings
from src.detection import twitch


def _clip(i, game_id, lang="fr", views=500, dur=30.0):
    return {"id": f"c{i}", "url": f"https://clips.twitch.tv/c{i}", "title": f"clip {i}",
            "broadcaster_name": f"s{i}", "view_count": views, "duration": dur,
            "created_at": "2026-11-16T08:00:00Z", "language": lang, "game_id": game_id,
            "video_id": "", "vod_offset": None}


def test_detect_by_game(monkeypatch):
    calls = []

    def fake_get(path, params):
        calls.append((path, params))
        if path == "games":
            return {"data": [{"id": "6", "name": "Grand Theft Auto VI"},
                             {"id": "5", "name": "Grand Theft Auto V"}]}
        if path == "clips" and params["game_id"] == "6":
            if "after" not in params:
                return {"data": [_clip(1, "6"), _clip(2, "6", lang="es")],
                        "pagination": {"cursor": "p2"}}
            return {"data": [_clip(3, "6", views=10), _clip(4, "6", dur=3.0), _clip(5, "6", lang="en")],
                    "pagination": {}}
        if path == "clips" and params["game_id"] == "5":
            return {"data": [_clip(6, "5", views=100000)], "pagination": {}}
        raise AssertionError(path)

    monkeypatch.setattr(twitch, "_helix_get", fake_get)
    monkeypatch.setattr(settings, "GAMES_PRIMARY", ["Grand Theft Auto VI"])
    monkeypatch.setattr(settings, "GAMES_SECONDARY", ["Grand Theft Auto V"])
    monkeypatch.setattr(settings, "TWITCH_LANGUAGES", ["fr", "en"])
    monkeypatch.setattr(settings, "TWITCH_CLIP_MIN_VIEWS", 100)
    monkeypatch.setattr(settings, "TARGET_STREAMERS_TWITCH", [])
    monkeypatch.setattr(settings, "TWITCH_MIN_POOL_PER_LANG", 0)  # seuil fixe pour ce test

    res = twitch.detect()
    ids = {c.source_id for c in res}
    # c2 (espagnol), c3 (10 vues), c4 (3 s) filtrés ; pagination suivie (c5)
    assert ids == {"c1", "c5", "c6"}
    by_id = {c.source_id: c for c in res}
    assert by_id["c1"].extra["primary_game"] is True
    assert by_id["c6"].extra["game_name"] == "Grand Theft Auto V"
    # bonus prioritaire appliqué
    base = twitch.clip_trend_score(500, "2026-11-16T08:00:00Z")
    assert by_id["c1"].score == round(base + settings.PRIMARY_GAME_BOOST, 2)


def test_adaptive_threshold(monkeypatch):
    monkeypatch.setattr(settings, "TWITCH_CLIP_MIN_VIEWS", 100)
    monkeypatch.setattr(settings, "TWITCH_CLIP_FLOOR_VIEWS", 10)
    monkeypatch.setattr(settings, "TWITCH_MIN_POOL_PER_LANG", 3)
    clips = [_clip(i, "6", lang="en", views=500) for i in range(5)]
    clips += [_clip(10 + i, "6", lang="fr", views=v) for i, v in enumerate([40, 35, 20, 5])]
    kept, min_views, reasons = twitch.select_clips(clips, {"fr", "en"})
    # pas assez de FR à 100 vues => seuil abaissé (100 -> 33 -> 11) jusqu'à avoir 3 FR
    assert min_views == 11
    assert sum(1 for c in kept if c["language"] == "fr") == 3
    assert reasons == {"vues": 1}
