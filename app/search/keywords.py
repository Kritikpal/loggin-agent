"""Turns a natural-language question into the keyword leg's tsquery text. Pure: no I/O.

Safety: the tsquery text is built ONLY from lexemes that match SAFE_LEXEME (lowercase letters, digits and the joiners
. _ / -, starting and ending with a letter or digit), each wrapped in single quotes. Quotes, backslashes, tsquery
operators and everything else in the question are dropped, so the text can never change the query's structure. It is
still passed to PostgreSQL as a bound parameter, never spliced into SQL.

Matching is OR-style: an event matches when it contains ANY lexeme; ranking (not matching) rewards events that contain
more of them. to_tsquery('simple', ...) re-parses each quoted lexeme with the same parser that built search_tsv, so
compound tokens such as 'tomcat/11.0.12' or 'org.postgresql.driver' are normalised exactly like the indexed text.
"""

import re

MAX_LEXEMES = 32  # a 2000-char question cannot blow up the tsquery
MIN_LEXEME_CHARS = 2
SAFE_LEXEME = re.compile(r"[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?")
_TOKEN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?")
_JOINERS = re.compile(r"[._/-]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# Question words and glue that say nothing about a log event. Domain words (error, failed, start...) are kept.
STOP_WORDS = frozenset("""
a about after all also am an and any are as at be been before being both but by can could did do does doing done
during each for from had has have having he her here how i if in into is it its just me more most my no nor not now
of off on once only or other our out over own same she should show so some such than that the their them then there
these they this those through to too under until up very was we were what when where which while who whom why will
with would you your tell explain mean means happened happen please give find get let us
""".split())


def keyword_lexemes(question: str) -> tuple[str, ...]:
    """The distinct lexemes of a question, in order: each token whole (lowercased) and, when it is an identifier
    (camelCase, snake_case, dotted, slashed, hyphenated), its parts as well. Stop words and 1-char lexemes dropped."""
    lexemes: list[str] = []
    for token in _TOKEN.findall(question):
        lexemes.append(token.lower())
        parts = [p for chunk in _JOINERS.split(token) for p in _CAMEL.findall(chunk)]
        if len(parts) > 1:
            lexemes += [p.lower() for p in parts]
    kept = (lex for lex in lexemes if len(lex) >= MIN_LEXEME_CHARS and lex not in STOP_WORDS and SAFE_LEXEME.fullmatch(lex))
    return tuple(dict.fromkeys(kept))[:MAX_LEXEMES]


def tsquery_text(lexemes: tuple[str, ...]) -> str | None:
    """`'a' | 'b' | ...` for to_tsquery('simple', :q), or None when there is nothing to match (no keyword leg)."""
    safe = [lex for lex in lexemes if SAFE_LEXEME.fullmatch(lex)]
    return " | ".join(f"'{lex}'" for lex in safe) or None
