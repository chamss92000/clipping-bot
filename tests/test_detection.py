from src.detection import cap_per_creator, dedupe_moments
from src.detection.games import game_hashtag, match_game
from src.models import Platform, VideoCandidate


def _c(cid, creator="streamer", video_id="v1", offset=100.0, score=50.0, created="2026-11-16T10:00:00Z"):
    return VideoCandidate(
        platform=Platform.TWITCH, source_id=cid, url=f"https://clips.twitch.tv/{cid}",
        title="t", creator=creator, score=score, published_at=created,
        extra={"video_id": video_id, "vod_offset": offset},
    )


def test_match_game_exact_not_prefix():
    assert match_game("Grand Theft Auto VI").info.name == "Grand Theft Auto VI"
    assert match_game("GTA 6").info.name == "Grand Theft Auto VI"
    # GTA V ne doit pas capturer GTA VI et inversement
    assert match_game("Grand Theft Auto V").info.name == "Grand Theft Auto V"
    assert match_game("Just Chatting") is None
    assert match_game("") is None


def test_primary_flag():
    assert match_game("Grand Theft Auto VI").primary is True
    assert match_game("Grand Theft Auto V").primary is False


def test_game_hashtag():
    assert game_hashtag("Grand Theft Auto VI") == "#GTA6"
    assert game_hashtag("Some Indie Game") == "#SomeIndieGame"


def test_dedupe_same_moment_keeps_first():
    a = _c("a", offset=100, score=90)
    b = _c("b", offset=120, score=80)          # même VOD, 20 s plus loin => doublon
    c = _c("c", offset=600, score=70)          # autre moment
    d = _c("d", creator="other", offset=100)   # autre streamer
    kept, dupes = dedupe_moments([a, b, c, d], window_s=45)
    assert [x.source_id for x in kept] == ["a", "c", "d"]
    assert [x.source_id for x in dupes] == ["b"]


def test_dedupe_falls_back_on_creation_time():
    a = _c("a", video_id="", offset=None, created="2026-11-16T10:00:00Z")
    b = _c("b", video_id="", offset=None, created="2026-11-16T10:00:30Z")
    c = _c("c", video_id="", offset=None, created="2026-11-16T12:00:00Z")
    kept, _ = dedupe_moments([a, b, c], window_s=45)
    assert [x.source_id for x in kept] == ["a", "c"]


def test_cap_per_creator():
    cands = [_c(str(i), offset=i * 1000) for i in range(4)] + [_c("x", creator="other")]
    kept, dropped = cap_per_creator(cands, 2)
    assert [x.source_id for x in kept] == ["0", "1", "x"]
    assert len(dropped) == 2
