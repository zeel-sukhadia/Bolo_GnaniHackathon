from app import gnani


def test_every_voice_in_use_has_a_gender():
    for v in [*gnani.VOICES.values(), *gnani.CONTACT_VOICES.values()]:
        assert v in gnani.VOICE_GENDER, v


def test_gender_rule_follows_the_voice():
    assert "female voice" in gnani.gender_rule("gu-IN")            # agent: Falak
    assert "male voice" in gnani.gender_rule("gu-IN", "Veera")     # demo contact
    assert "female" not in gnani.gender_rule("gu-IN", "Veera")
    assert gnani.gender_rule("xx-XX") == ""
