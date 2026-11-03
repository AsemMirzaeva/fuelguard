import logging

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.workers import dispatch, names
from app.workers.celery_app import celery_app


class TestEnqueue:
    async def test_returns_true_and_sends_the_task_by_name(self, monkeypatch):
        sent = []
        monkeypatch.setattr(
            celery_app, "send_task", lambda name, **kw: sent.append((name, kw["args"], kw["retry"]))
        )
        assert await dispatch.enqueue(names.ANALYZE_TRANSACTION, 7) is True
        assert sent == [(names.ANALYZE_TRANSACTION, (7,), False)]

    async def test_broker_failure_is_logged_not_raised(self, monkeypatch, caplog):
        def boom(*a, **kw):
            raise ConnectionError("redis is down")

        monkeypatch.setattr(celery_app, "send_task", boom)
        with caplog.at_level(logging.ERROR):
            assert await dispatch.enqueue(names.ANALYZE_TRANSACTION, 7) is False
        assert "recovery job will retry" in caplog.text


class TestSettings:
    ENV = dict(
        database_url_async="postgresql+asyncpg://x",
        database_url_sync="postgresql+psycopg2://x",
        redis_url="redis://x",
        oidc_issuer="http://localhost:8180/realms/fuelguard",
    )

    def test_defaults_match_the_agreed_rules(self):
        s = Settings(_env_file=None, **self.ENV)
        assert (s.frequency_max_purchases, s.frequency_window_minutes) == (3, 30)
        assert (s.large_purchase_multiplier, s.large_purchase_min_history) == (2.0, 5)
        assert s.large_purchase_fallback_tank_ratio == 0.8
        assert (s.stuck_after_minutes, s.recovery_interval_seconds) == (5, 120)

    def test_a_too_short_issuer_is_rejected(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{**self.ENV, "oidc_issuer": "x"})

    @pytest.mark.parametrize("missing", ["redis_url", "oidc_issuer"])
    def test_missing_required_setting_is_rejected(self, monkeypatch, missing):
        monkeypatch.delenv(missing.upper())  # the container provides it, so remove it for this test
        env = dict(self.ENV)
        del env[missing]
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **env)


def test_beat_schedule_has_both_periodic_jobs():
    schedule = celery_app.conf.beat_schedule
    assert schedule["recover-stuck-transactions"]["task"] == names.RECOVER_STUCK
    assert schedule["daily-report"]["task"] == names.DAILY_REPORT
    assert schedule["daily-report"]["schedule"].hour == {0} and schedule["daily-report"]["schedule"].minute == {10}
