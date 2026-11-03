import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.config import get_settings
from app.db.sync_db import session_scope
from app.models import DailyReport, Notification
from app.models.enums import FlagReason as F
from app.models.enums import TransactionStatus as S
from app.workers import names, risk_analysis, tasks
from tests import seed
from tests.seed import add_declined, add_tx, snapshot

settings = get_settings()


def db_error():
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


def run(task, *args, **kw):
    """Run a task the way a worker would, in-process, including its retry logic."""
    return task.apply(args=args, kwargs=kw)


class TestPing:
    def test_ping(self):
        assert run(tasks.ping).get() == "pong"


class TestRegistration:
    def test_tasks_are_registered_under_the_shared_names(self):
        from app.workers.celery_app import celery_app

        registered = set(celery_app.tasks)
        assert {names.ANALYZE_TRANSACTION, names.CHECK_DECLINED_ATTEMPT, names.NOTIFY_SUSPICIOUS,
                names.DAILY_REPORT, names.RECOVER_STUCK} <= registered  # fmt: skip

    @pytest.mark.parametrize(
        "task",
        [tasks.analyze_transaction, tasks.check_declined_attempt, tasks.notify_suspicious_transaction,
         tasks.daily_report, tasks.recover_stuck_transactions],
    )  # fmt: skip
    def test_retry_policy(self, task):
        assert task.max_retries == settings.analysis_max_retries
        assert set(task.autoretry_for) == {OperationalError, InterfaceError}
        assert task.retry_backoff is True and task.retry_jitter is True
        assert task.acks_late is True and task.reject_on_worker_lost is True


class TestAnalyzeTask:
    def test_flagged_purchase_queues_exactly_one_notification(self, db, sent):
        w = seed.create_world(db, per_transaction_limit=10)
        tx = add_tx(db, w, price="50")
        assert run(tasks.analyze_transaction, tx).successful()
        assert sent == [(names.NOTIFY_SUSPICIOUS, tx)]
        assert snapshot(tx)["status"] == S.APPROVED

    def test_clean_purchase_queues_nothing(self, db, world, sent):
        tx = add_tx(db, world, gallons="10", price="30")
        assert run(tasks.analyze_transaction, tx).successful() and sent == []

    def test_running_it_twice_does_not_queue_a_second_notification(self, db, sent):
        w = seed.create_world(db, per_transaction_limit=10)
        tx = add_tx(db, w, price="50")
        run(tasks.analyze_transaction, tx)
        run(tasks.analyze_transaction, tx)
        assert len(sent) == 1

    def test_a_broker_outage_after_saving_does_not_fail_the_task(self, db, monkeypatch, caplog):
        w = seed.create_world(db, per_transaction_limit=10)
        tx = add_tx(db, w, price="50")
        monkeypatch.setattr(tasks.notify_suspicious_transaction, "delay", Mock(side_effect=ConnectionError("redis down")))
        with caplog.at_level(logging.ERROR):
            assert run(tasks.analyze_transaction, tx).successful()
        assert snapshot(tx)["status"] == S.APPROVED and snapshot(tx)["flag_rows"] == 1  # work is saved
        assert "Could not queue notification" in caplog.text

    def test_unknown_transaction_is_not_an_error(self, sent):
        assert run(tasks.analyze_transaction, 999999).successful() and sent == []


class TestDeclinedTask:
    def test_burst_queues_a_notification(self, db, world, sent):
        add_declined(db, world, minutes_ago=20)
        add_declined(db, world, minutes_ago=10)
        tx = add_declined(db, world)
        assert run(tasks.check_declined_attempt, tx).successful()
        assert sent == [(names.NOTIFY_SUSPICIOUS, tx)]

    def test_single_attempt_queues_nothing(self, db, world, sent):
        tx = add_declined(db, world)
        assert run(tasks.check_declined_attempt, tx).successful() and sent == []
        assert snapshot(tx)["done"] is True


class TestNotifyTask:
    def notifications(self):
        with session_scope() as s:
            return s.query(Notification).count()

    def test_creates_one_notification_even_when_run_twice(self, db, world):
        tx = add_tx(db, world, status=S.APPROVED)
        seed.add_flag(db, tx)
        run(tasks.notify_suspicious_transaction, tx)
        run(tasks.notify_suspicious_transaction, tx)
        assert self.notifications() == 1

    def test_logs_an_alert(self, db, world, caplog):
        tx = add_tx(db, world, status=S.APPROVED)
        seed.add_flag(db, tx)
        with caplog.at_level(logging.WARNING):
            run(tasks.notify_suspicious_transaction, tx)
        assert "ALERT Suspicious transaction" in caplog.text


class TestRetries:
    def test_temporary_database_errors_are_retried_until_it_works(self, monkeypatch):
        flaky = Mock(side_effect=[db_error(), InterfaceError("x", {}, Exception("gone")), False])
        monkeypatch.setattr(risk_analysis, "analyze_transaction", flaky)
        result = run(tasks.analyze_transaction, 1)
        assert result.successful() and flaky.call_count == 3

    def test_gives_up_after_max_retries_and_says_so(self, monkeypatch, caplog):
        always = Mock(side_effect=db_error())
        monkeypatch.setattr(risk_analysis, "analyze_transaction", always)
        with caplog.at_level(logging.ERROR):
            result = run(tasks.analyze_transaction, 7)
        assert result.failed()
        assert always.call_count == settings.analysis_max_retries + 1
        assert "gave up" in caplog.text and "recovery job will re-queue" in caplog.text

    def test_other_errors_are_not_retried(self, monkeypatch):
        broken = Mock(side_effect=ValueError("bug"))
        monkeypatch.setattr(risk_analysis, "analyze_transaction", broken)
        result = run(tasks.analyze_transaction, 1)
        assert result.failed() and broken.call_count == 1

    def test_a_failed_attempt_leaves_no_partial_data(self, db, monkeypatch):
        """If the work fails halfway, the transaction rolls back and the purchase stays pending."""
        w = seed.create_world(db, per_transaction_limit=10)
        tx = add_tx(db, w, price="50")
        original = risk_analysis._insert_flags

        def insert_then_crash(session, tx_id, results):
            original(session, tx_id, results)
            raise db_error()

        monkeypatch.setattr(risk_analysis, "_insert_flags", insert_then_crash)
        assert run(tasks.analyze_transaction, tx).failed()
        snap = snapshot(tx)
        assert snap["status"] == S.PENDING and snap["flag_rows"] == 0 and snap["done"] is False

        monkeypatch.setattr(risk_analysis, "_insert_flags", original)
        assert run(tasks.analyze_transaction, tx).successful()  # a later retry completes it
        assert snapshot(tx)["status"] == S.APPROVED and snapshot(tx)["flag_rows"] == 1


class TestRecovery:
    def test_requeues_each_kind_of_unfinished_work(self, db, world, sent):
        pending = add_tx(db, world, created_minutes_ago=10)
        declined = add_declined(db, world, created_minutes_ago=10)
        unnotified = add_tx(db, world, status=S.APPROVED)
        seed.add_flag(db, unnotified, minutes_ago=10)

        result = run(tasks.recover_stuck_transactions).get()

        assert result == {"unfinished": 2, "unnotified": 1}
        assert sorted(sent) == sorted([
            (names.ANALYZE_TRANSACTION, pending),
            (names.CHECK_DECLINED_ATTEMPT, declined),
            (names.NOTIFY_SUSPICIOUS, unnotified),
        ])  # fmt: skip

    def test_nothing_to_do(self, db, world, sent, caplog):
        add_tx(db, world, created_minutes_ago=1)
        assert run(tasks.recover_stuck_transactions).get() == {"unfinished": 0, "unnotified": 0}
        assert sent == []

    def test_end_to_end_a_lost_message_is_recovered(self, db, sent):
        """A purchase whose queue message was lost still ends up analyzed, flagged and notified."""
        w = seed.create_world(db, per_transaction_limit=10)
        tx = add_tx(db, w, price="50", created_minutes_ago=10)  # saved, but no message ever sent

        run(tasks.recover_stuck_transactions)
        assert sent == [(names.ANALYZE_TRANSACTION, tx)]
        run(tasks.analyze_transaction, tx)           # the worker picks the re-queued message up
        run(tasks.notify_suspicious_transaction, tx)  # ...and the notification message it queued
        with session_scope() as s:
            assert s.query(Notification).filter_by(transaction_id=tx).count() == 1
        assert snapshot(tx)["status"] == S.APPROVED

        sent.clear()
        assert run(tasks.recover_stuck_transactions).get() == {"unfinished": 0, "unnotified": 0}
        assert sent == []  # nothing left to recover

    def test_database_trouble_is_retried(self, monkeypatch):
        flaky = Mock(side_effect=[db_error(), ([], [])])
        monkeypatch.setattr(risk_analysis, "find_stuck", flaky)
        assert run(tasks.recover_stuck_transactions).successful() and flaky.call_count == 2


class TestDailyReportTask:
    def reports(self):
        with session_scope() as s:
            return {(r.report_date.isoformat(), r.card_id): float(r.total_spend) for r in s.query(DailyReport)}

    def test_explicit_date(self, db, world):
        at = datetime(2026, 2, 10, 12, tzinfo=timezone.utc)
        add_tx(db, world, at=at, price="42", status=S.APPROVED)
        assert run(tasks.daily_report, "2026-02-10").get() == 1
        assert self.reports() == {("2026-02-10", world.card_id): 42.0}

    def test_defaults_to_yesterday_utc(self, db, world):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        add_tx(db, world, at=yesterday, price="10", status=S.APPROVED)
        add_tx(db, world, price="99", status=S.APPROVED)  # today: not in yesterday's report
        run(tasks.daily_report)
        assert self.reports() == {(yesterday.date().isoformat(), world.card_id): 10.0}

    def test_bad_date_fails_without_retrying(self):
        result = run(tasks.daily_report, "not-a-date")
        assert result.failed() and isinstance(result.result, ValueError)

    def test_rerun_is_safe(self, db, world):
        add_tx(db, world, at=datetime(2026, 2, 10, 8, tzinfo=timezone.utc), price="5", status=S.APPROVED)
        run(tasks.daily_report, "2026-02-10")
        run(tasks.daily_report, "2026-02-10")
        assert len(self.reports()) == 1
