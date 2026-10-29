# SPDX-FileCopyrightText: 2026 European Centre for Medium-Range Weather Forecasts (ECMWF)
#
# SPDX-License-Identifier: Apache-2.0

"""Which release a workflow run is, from its event.

- A push of a release tag: ``release``, or ``prerelease`` for ``-rcN``.
- A same-repo pull request labelled ``approved-for-cd``: ``rehearsal``. It builds ``github.sha``
  (the merge commit) and records the PR head, which is what PR CI is matched on.
- Anything else is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from . import ReleaseError
from .version import ReleaseVersion, parse_tag, rehearsal_version

REHEARSAL_LABEL: Final = "approved-for-cd"
_TAG_REF_PREFIX: Final = "refs/tags/"


@dataclass(frozen=True)
class ReleaseEvent:
    version: ReleaseVersion
    #: The commit to build: the tagged commit, or the PR merge commit.
    sha: str
    #: The PR head for a rehearsal; the tagged commit otherwise.
    head_sha: str
    pr_number: int | None = None
    head_ref: str | None = None

    @property
    def mode(self) -> str:
        return self.version.mode


def classify(
    *,
    event_name: str,
    ref: str,
    sha: str,
    repository: str,
    payload: Mapping[str, Any],
    project_version: str,
    tag_prefix: str = "",
    version_parts: int = 3,
) -> ReleaseEvent:
    if event_name == "push":
        if not ref.startswith(_TAG_REF_PREFIX):
            raise ReleaseError(f"a push of {ref!r} is not a release: releases run from tag pushes")
        tag = ref.removeprefix(_TAG_REF_PREFIX)
        return ReleaseEvent(version=parse_tag(tag, prefix=tag_prefix, parts=version_parts), sha=sha, head_sha=sha)
    if event_name == "pull_request":
        if payload.get("action") != "labeled":
            raise ReleaseError(f"pull_request action {payload.get('action')!r}: a rehearsal runs on 'labeled' only")
        label = (payload.get("label") or {}).get("name")
        if label != REHEARSAL_LABEL:
            raise ReleaseError(f"label {label!r} does not start a rehearsal; {REHEARSAL_LABEL!r} does")
        pr = payload.get("pull_request") or {}
        head = pr.get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name")
        if head_repo != repository:
            raise ReleaseError(f"pull request head is in {head_repo!r}, not {repository!r}: no rehearsal from a fork")
        head_sha = head.get("sha")
        if not isinstance(head_sha, str) or not head_sha:
            raise ReleaseError("pull_request payload has no head.sha")
        number = pr.get("number")
        return ReleaseEvent(
            version=rehearsal_version(project_version, parts=version_parts),
            sha=sha,
            head_sha=head_sha,
            pr_number=number if isinstance(number, int) else None,
            head_ref=head.get("ref") if isinstance(head.get("ref"), str) else None,
        )
    raise ReleaseError(f"event {event_name!r} does not start a release: push a release tag or label a pull request")
