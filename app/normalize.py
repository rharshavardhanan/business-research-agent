"""Canonical forms for every value that will ever be compared.

Dedupe correctness lives here. If two spellings of the same phone number do not
collapse to one string, the primary dedupe key fails and duplicates reach the
user's lead sheet - which is the exact problem this tool exists to prevent.
"""

import re

# Tokens dropped when comparing business names. Deliberately conservative:
# these are structural/legal words, never words that distinguish one business
# from another. "Care", "Smile", "Dental" are NOT here - dropping them would
# merge genuinely different clinics.
GENERIC_SUFFIXES = {
    "clinic",
    "clinics",
    "centre",
    "center",
    "hospital",
    "hospitals",
    "pvt",
    "private",
    "ltd",
    "limited",
    "llp",
    "inc",
    "co",
    "company",
    "the",
    "and",
}


def normalize_phone(raw: str | None) -> str | None:
    """Canonicalise an Indian mobile number to +91XXXXXXXXXX.

    Returns None for anything that is not a valid Indian mobile - including
    landlines, which have no reliable national format and make a poor dedupe
    key. A None phone simply falls through to the next dedupe tier.
    """
    # ponytail: India-only. Swap in `phonenumbers` if this ever leaves +91.
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        return None
    return "+91" + digits


def normalize_url(raw: str | None) -> str | None:
    """Strip scheme, `www.`, and trailing slash so URL variants compare equal."""
    if not raw:
        return None
    url = raw.strip()
    url = re.sub(r"^https?://", "", url, flags=re.I)
    url = re.sub(r"^www\.", "", url, flags=re.I)
    url = url.rstrip("/")
    if not url:
        return None
    # Lowercase the host only; paths can be case-sensitive on some servers.
    host, slash, path = url.partition("/")
    return host.lower() + slash + path


def _merge_initialisms(tokens: list[str]) -> list[str]:
    """Collapse runs of single-character tokens: ["a","b","c"] -> ["abc"].

    "A.B.C. Dental" and "ABC Dental" are the same business; punctuation
    stripping alone leaves them three tokens apart.
    """
    out: list[str] = []
    run: list[str] = []
    for tok in tokens:
        if len(tok) == 1:
            run.append(tok)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(tok)
    if run:
        out.append("".join(run))
    return out


def normalize_name(raw: str) -> str:
    """Lowercase, strip punctuation, drop structural words.

    Comparison only - the display name is never mutated by this.
    """
    if not raw:
        return ""
    tokens = _merge_initialisms(re.sub(r"[^a-z0-9]+", " ", raw.lower()).split())
    kept = [t for t in tokens if t not in GENERIC_SUFFIXES]
    # If a name is *entirely* generic ("The Clinic"), keep the original tokens
    # rather than collapsing every such business to the empty string.
    return " ".join(kept or tokens)


def normalize_address(raw: str | None) -> str | None:
    """Lowercase, strip punctuation, collapse whitespace."""
    if not raw:
        return None
    collapsed = " ".join(re.sub(r"[^a-z0-9]+", " ", raw.lower()).split())
    return collapsed or None
