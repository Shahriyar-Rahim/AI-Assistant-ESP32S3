import asyncio

from app.services import knowledge
from app.services.speech_text import clean_for_speech


def test_clean_for_speech_strips_symbols():
    out = clean_for_speech("**Hello** there: list;\n* one\n* two [1] https://x.com `code` 😀")
    for bad in ("*", "`", "[", "http", ";", "😀"):
        assert bad not in out
    assert "Hello" in out


def test_calculator_and_units():
    assert "36" in knowledge.calculate("what is 12 times 3")
    assert knowledge.calculate("what is the capital of france") is None
    assert "8.05" in knowledge.convert_units("convert 5 miles to km")


def test_fact_lookup():
    assert "Paris" in knowledge.lookup_fact("what is the capital of france")
    assert knowledge.lookup_fact("zzz qqq unknowable") is None


def test_web_answer_rejects_opinion():
    assert asyncio.run(knowledge.web_answer("what is the future of cp in this ai era")) is None
