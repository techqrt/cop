"""Async task-execution abstraction (ADR-009, docs/processing-pipeline.md §3). PMS has
no background-processing precedent (docs/pms-reference-analysis.md §1), so this is a
new interface for CSC, deliberately decoupled from any specific queue technology - the
production implementation is docs/open-decisions.md OD-003.
"""

import abc
import dataclasses
from typing import Callable


@dataclasses.dataclass
class TaskEnvelope:
    """What gets queued - identifies the job row and the callable that executes it, so
    a TaskRunner implementation never needs to know about ProcessingJob/pipeline
    internals."""

    job_id: int
    run: Callable[[], None]


class TaskRunner(abc.ABC):
    @abc.abstractmethod
    def enqueue(self, task: TaskEnvelope) -> None:
        raise NotImplementedError
