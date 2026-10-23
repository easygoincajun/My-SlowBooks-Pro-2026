"""A company a Server Edition server cannot open now (#358) answers one of
the server's own sentences, looked up, never the exception's text itself:
what two code-scanning alerts on 2.25.0 asked for."""

from app.services import company_file
from app.services.served_companies import (
    ENCRYPTED_ON_ITS_OWN_KEY,
    MISSING,
    NOT_SERVED,
    OTHER_VERSION,
    CompanyUnavailable,
    sentence_of,
)


def test_the_exception_carries_its_sentence():
    exc = CompanyUnavailable(MISSING)
    assert exc.sentence == MISSING and str(exc) == MISSING


def test_every_known_sentence_comes_back_as_itself():
    for sentence in (
        NOT_SERVED,
        MISSING,
        OTHER_VERSION,
        ENCRYPTED_ON_ITS_OWN_KEY,
        company_file.UNAVAILABLE_MESSAGE,
        company_file.NO_KEY_MESSAGE,
        company_file.WRONG_KEY_MESSAGE,
    ):
        assert sentence_of(CompanyUnavailable(sentence)) is sentence


def test_anything_else_is_never_echoed():
    assert (
        sentence_of(CompanyUnavailable("Traceback (most recent call last)"))
        == NOT_SERVED
    )
    assert sentence_of(CompanyUnavailable("")) == NOT_SERVED
