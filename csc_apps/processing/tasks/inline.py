from csc_apps.processing.tasks.base import TaskEnvelope, TaskRunner


class InlineTaskRunner(TaskRunner):
    """Executes the task synchronously, in-process, at enqueue time.

    Dev/local-only (docs/processing-pipeline.md §3, docs/open-decisions.md OD-003) -
    it has no retry backoff, no isolation from the web process, and blocks the calling
    request for the task's full duration. It exists so Phase 0 is runnable end-to-end
    without standing up a broker/queue before a production TaskRunner is chosen.
    """

    def enqueue(self, task: TaskEnvelope) -> None:
        task.run()
