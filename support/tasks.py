from celery import shared_task

from .models import SupportFinancialAction
from .services import reconcile_refund


@shared_task(bind=True, max_retries=60, default_retry_delay=300)
def reconcile_support_refund(self, refund_id):
    action = SupportFinancialAction.objects.filter(
        pk=refund_id
    ).first()

    if action is None:
        return {"status": "not_found"}

    if action.status == SupportFinancialAction.Status.SUCCEEDED:
        return {"status": "succeeded"}

    if action.status == SupportFinancialAction.Status.FAILED:
        return {"status": "failed"}

    try:
        action = reconcile_refund(action=action)
    except Exception as exc:
        raise self.retry(exc=exc) from exc

    if action.status in {
        SupportFinancialAction.Status.PROCESSING,
        SupportFinancialAction.Status.PROCESSING_UNKNOWN,
    }:
        raise self.retry(
            exc=RuntimeError(
                "Refund remains pending with the provider."
            )
        )

    return {"status": action.status}