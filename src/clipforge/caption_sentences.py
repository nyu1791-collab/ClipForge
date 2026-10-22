"""Sentence ends and sentence starts from morphological analysis (fugashi, UniDic)."""

# Recognition writes almost no full stop, so a sentence end is found from the word forms:
# です, ます and だ in the final form, た after them, and sentence-final particles (ね, よ, か ...).
_TAGGER = None


def _tagger():
    """Morphological analyser (fugashi with the UniDic dictionary), made once."""
    global _TAGGER
    if _TAGGER is None:
        import fugashi

        _TAGGER = fugashi.Tagger()
    return _TAGGER


def _ends_sentence(morpheme, before):
    """True when a morpheme ends a sentence, given the morpheme before it."""
    form = morpheme.feature
    final = (getattr(form, "cForm", "") or "").startswith("終止形")
    # です and ます end a sentence in any form here: the particle check has already let a
    # sentence go on, and without punctuation the tagger often reads them as attributive.
    if form.pos1 == "助動詞" and form.lemma in ("です", "ます"):
        return True
    if form.pos1 == "助動詞" and final and form.lemma == "だ":
        return True
    if form.pos1 == "助動詞" and final and form.lemma == "た" and before is not None:
        return before.feature.lemma in ("です", "ます", "だ")
    return form.pos1 == "助詞" and form.pos2 == "終助詞"


def sentence_ends(tokens):
    """Indices of the tokens after which a sentence ends.

    A particle or auxiliary that follows means the sentence goes on, so ですよね is not cut
    between です and よね.
    """
    text, ends = "", {}
    for index, (_, _, raw) in enumerate(tokens):
        piece = raw.strip()
        if piece:
            text += piece
            ends[len(text)] = index
    morphemes = list(_tagger()(text))
    found, offset = set(), 0
    for position, morpheme in enumerate(morphemes):
        offset += len(morpheme.surface)
        if offset not in ends:
            continue
        following = morphemes[position + 1] if position + 1 < len(morphemes) else None
        if following is not None and following.feature.pos1 in ("助詞", "助動詞"):
            continue
        before = morphemes[position - 1] if position else None
        if _ends_sentence(morpheme, before):
            found.add(ends[offset])
    return found


def orphan_start(text):
    """Why a caption opens badly: "punctuation" or "particle" (a sentence-final particle), or None."""
    if text[:1] in ("。", "、", "！", "？"):
        return "punctuation"
    morphemes = [m for m in _tagger()(text) if m.feature.pos1 != "補助記号"]
    if (
        morphemes
        and morphemes[0].feature.pos1 == "助詞"
        and morphemes[0].feature.pos2 == "終助詞"
    ):
        return "particle"
    return None
