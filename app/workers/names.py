"""Task names shared by the API (which sends them) and the worker (which runs them)."""

ANALYZE_TRANSACTION = "fuelguard.analyze_transaction"
CHECK_DECLINED_ATTEMPT = "fuelguard.check_declined_attempt"
NOTIFY_SUSPICIOUS = "fuelguard.notify_suspicious_transaction"
DAILY_REPORT = "fuelguard.daily_report"
RECOVER_STUCK = "fuelguard.recover_stuck_transactions"
