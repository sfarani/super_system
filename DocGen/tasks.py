# pyright: reportAttributeAccessIssue=false

try:
    from celery import shared_task
except Exception:  # pragma: no cover
    def shared_task(*_args, **_kwargs):
        def _decorator(func):
            return func
        return _decorator

from .services import process_sla_events


@shared_task(name="docgen.process_sla_events")
def process_sla_events_task():
    return process_sla_events()
