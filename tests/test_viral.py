from src.publisher.tiktok import build_caption
from src.transcription.whisper_transcribe import Word
from src.viral.gemini import (
    clamp_trim, clean_hook, fallback_hook, has_first_person, hook_problems,
    normalize_hashtags, pick_hook, snap_to_words, too_similar,
)


def test_first_person_fr_en():
    assert has_first_person("J'ai trouvé un secret", "fr")
    assert has_first_person("On a cassé GTA 6", "fr")
    assert not has_first_person("Il trouve un secret dans GTA 6", "fr")
    assert has_first_person("I found a secret", "en")
    assert not has_first_person("He found a secret in GTA 6", "en")
    # "mon" dans un mot ne compte pas
    assert not has_first_person("Le monde de Vice City", "fr")


def test_clean_hook():
    assert clean_hook('"Il casse GTA 6" 😱 #gta6.') == "Il casse GTA 6"


def test_hook_problems():
    assert hook_problems("Il vole un jet dans GTA 6", "fr") == []
    assert "1re personne" in hook_problems("Je vole un jet", "fr")
    assert "début interdit" in hook_problems("Le pire bug de GTA 6", "fr")
    assert hook_problems("Mot", "fr")  # trop court


def test_too_similar():
    assert too_similar("Quand Jason découvre Vice City", ["Quand Lucia vole une voiture"])
    assert not too_similar("Un jet volé en plein ciel", ["Quand Lucia vole une voiture"])


def test_pick_hook_prefers_best_valid():
    hooks = ["J'ai volé un jet", "Il vole un jet dans GTA 6", "Quand les flics arrivent"]
    assert pick_hook(hooks, 0, "fr", []) == "Il vole un jet dans GTA 6"
    assert pick_hook(hooks, 2, "fr", []) == "Quand les flics arrivent"
    # tout est en 1re personne => rien
    assert pick_hook(["Je gagne", "On perd tout"], 0, "fr", []) is None


def test_normalize_hashtags():
    tags = normalize_hashtags(["gta6", "#Squeezie", "#fyp", "#Vice City!"], "Grand Theft Auto VI", "Squeezie")
    assert tags[0] == "#GTA6"
    assert tags[1] == "#squeezie"
    assert len(tags) <= 5
    assert len({t.lower() for t in tags}) == len(tags)


def test_clamp_trim():
    assert clamp_trim(0.4, 30, 30) == (0.0, 30)             # micro-retouches ignorées
    assert clamp_trim(25, 30, 30) == (15.0, 30)              # jamais > 50 % coupé au début
    s, e = clamp_trim(3, 5, 30)                              # trop court => allongé
    assert e - s >= 8
    assert clamp_trim("x", None, 40) == (0.0, 40)            # entrée invalide


def test_snap_to_words():
    words = [Word(1.0, 1.5, "salut"), Word(1.6, 2.4, "tout")]
    assert snap_to_words(1.2, 2.0, words, 10) == (0.9, 2.55)


def test_fallback_hook():
    assert fallback_hook("il casse tout 😂", "Grand Theft Auto VI", "fr") == "Il casse tout"
    assert "GTA 6" in fallback_hook("JE SUIS MORT", "Grand Theft Auto VI", "fr")


def test_build_caption():
    cap = build_caption("Il vole un jet", ["#GTA6", "#squeezie"], "Squeezie vole un jet à Vice City dans GTA 6")
    assert cap.splitlines()[0] == "Il vole un jet"
    assert "Vice City" in cap.splitlines()[1]
    assert cap.endswith("#GTA6 #squeezie")
