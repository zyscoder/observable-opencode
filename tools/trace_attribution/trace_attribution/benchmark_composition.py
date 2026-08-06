"""Pure ordered composition for benchmark Trace factual inputs."""

from __future__ import annotations

import copy
import json
from typing import Iterable, Optional

from .evaluation_facts import inject_external_evaluation_facts
from .models import JsonDict, stable_json
from .quality_review import inject_quality_gap_records_graph_free


def compose_effective_trace(
    trace: JsonDict,
    review: Optional[JsonDict] = None,
    evaluations: Iterable[JsonDict] = (),
) -> JsonDict:
    """Compose factual inputs without accessing artifacts or constructing a graph."""

    composed = copy.deepcopy(trace)
    _reject_duplicate_artifact_ids(composed)
    if review is not None:
        composed = inject_quality_gap_records_graph_free(
            composed,
            copy.deepcopy(review),
        )
    composed = inject_external_evaluation_facts(
        composed,
        [copy.deepcopy(evaluation) for evaluation in evaluations],
    )
    _reject_duplicate_artifact_ids(composed)
    return json.loads(stable_json(composed))


def _reject_duplicate_artifact_ids(trace: JsonDict) -> None:
    artifacts = trace.get("artifacts")
    if not isinstance(artifacts, list):
        return
    artifact_ids = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_id = artifact.get("artifact_id")
        if not isinstance(artifact_id, str):
            raise ValueError("artifact_id must be a string")
        artifact_ids.append(artifact_id)
    duplicate_ids = {
        artifact_id
        for artifact_id in artifact_ids
        if artifact_ids.count(artifact_id) > 1
    }
    if duplicate_ids:
        raise ValueError("duplicate artifact_id: {0}".format(sorted(duplicate_ids)[0]))
