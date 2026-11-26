"""PR references on the command line (docs/interface.md: Objects, Values).

A PR is named by forgectl's canonical reference ``host/<project path>#N`` or by an https PR URL
(``https://host/<project path>/pull/N``, ``.../-/merge_requests/N``), which this module turns into
the reference. Nothing here asks the host: a name is only checked for shape. The argument is
untrusted text, so it must be one line without control characters before it is looked at.
"""

from __future__ import annotations

import re
from typing import Final

from .errors import CliError

_HOST: Final = (
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
)
_PATH: Final = r"(?:/(?!\.\.?(?:/|#|$))[A-Za-z0-9._-]+)+"
_NUMBER: Final = r"[1-9][0-9]{0,9}"
# the same grammar as schemas/defs.json#/$defs/pr
PR_REF_RE: Final = re.compile(f"{_HOST}{_PATH}#{_NUMBER}")
_URL_RE: Final = re.compile(
    rf"https://({_HOST})((?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+?)(?:/-)?/(?:pull|merge_requests)"
    rf"/({_NUMBER})/?"
)
REPO_ID_RE: Final = re.compile(f"{_HOST}{_PATH}")
_MAX_CHARS: Final = 2048


def _checked(text: str) -> str:
    if (
        not text
        or len(text) > _MAX_CHARS
        or any(not ch.isprintable() or ch.isspace() for ch in text)
    ):
        raise CliError("usage", "a PR reference is one line of printable text without spaces")
    return text


def parse_pr(text: str) -> str:
    """The canonical reference of ``text`` (a reference or an https URL); exit 2 ``usage`` for
    anything else."""
    text = _checked(text)
    if PR_REF_RE.fullmatch(text):
        return text
    url = _URL_RE.fullmatch(text)
    if url is not None:
        reference = f"{url.group(1)}{url.group(2)}#{url.group(3)}"
        if PR_REF_RE.fullmatch(reference):
            return reference
    raise CliError("usage", "not a PR reference (host/project#N) or an https PR URL")


def host_of(pr: str) -> str:
    """The host part of a canonical reference."""
    return pr.split("/", 1)[0]


def repo_of(pr: str) -> str:
    return pr.rsplit("#", 1)[0]
