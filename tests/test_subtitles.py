from src.editing.subtitles import build_ass, chunk_words
from src.transcription.whisper_transcribe import Word


def _w(s, e, t):
    return Word(s, e, t)


def test_chunk_breaks_on_pause_sentence_and_length():
    words = [
        _w(0.0, 0.2, "il"), _w(0.25, 0.5, "arrive."), _w(0.55, 0.8, "Regarde"),
        _w(2.0, 2.3, "ça"),                      # pause > 0.6 s
        _w(2.35, 2.9, "extraordinairement"), _w(2.95, 3.4, "incroyable"),  # trop long ensemble
    ]
    chunks = [[w.word for w in c] for c in chunk_words(words, 3, 18)]
    assert chunks == [["il", "arrive."], ["Regarde"], ["ça"], ["extraordinairement"], ["incroyable"]]


def test_build_ass_no_overlap(tmp_path):
    words = [_w(10.0 + i * 0.3, 10.25 + i * 0.3, f"mot{i}") for i in range(7)]
    out = build_ass(words, tmp_path / "s.ass", clip_offset=10.0, clip_duration=1.5)
    lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln.startswith("Dialogue")]
    assert lines, "aucun sous-titre"
    # aucun mot au-delà de la durée du clip
    assert all("MOT6" not in ln for ln in lines)

    def t(s):
        h, m, sec = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)

    spans = [(t(ln.split(",")[1]), t(ln.split(",")[2])) for ln in lines]
    for (s1, e1), (s2, _) in zip(spans, spans[1:]):
        assert e1 <= s2 + 1e-6
