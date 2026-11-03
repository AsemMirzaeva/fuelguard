from datetime import datetime

from sqlalchemy import and_, or_

from app.models import FuelTransaction
from app.models.enums import TransactionStatus


def stuck_condition(cutoff: datetime):
    """Transactions the background analysis has not finished, saved before cutoff."""
    return and_(
        FuelTransaction.created_at < cutoff,
        or_(
            FuelTransaction.status == TransactionStatus.PENDING,
            and_(
                FuelTransaction.status == TransactionStatus.DECLINED,
                FuelTransaction.analysis_done_at.is_(None),
            ),
        ),
    )
