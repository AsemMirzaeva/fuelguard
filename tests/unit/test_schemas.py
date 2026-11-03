from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.enums import ReviewStatus, TransactionStatus
from app.schemas.cards import CardCreate, CardLimitsUpdate
from app.schemas.fleet import DriverUpdate, TruckCreate, TruckUpdate
from app.schemas.transactions import PurchaseRequest, ReviewRequest, TransactionRead

BASE = dict(card_number="7001-0001", station_id=1, gallons="10", total_price="30")


class TestPurchaseRequest:
    def test_valid(self):
        p = PurchaseRequest(**BASE)
        assert p.gallons == Decimal("10") and p.truck_id is None

    def test_strips_whitespace_in_card_number(self):
        assert PurchaseRequest(**{**BASE, "card_number": "  7001-0001 "}).card_number == "7001-0001"

    @pytest.mark.parametrize("gallons", ["0", "-1", "1.2345", "12345678901"])
    def test_bad_gallons(self, gallons):
        with pytest.raises(ValidationError):
            PurchaseRequest(**{**BASE, "gallons": gallons})

    @pytest.mark.parametrize("price", ["-0.01", "1.001"])
    def test_bad_price(self, price):
        with pytest.raises(ValidationError):
            PurchaseRequest(**{**BASE, "total_price": price})

    def test_zero_price_is_allowed(self):
        assert PurchaseRequest(**{**BASE, "total_price": "0"}).total_price == 0

    def test_short_card_number_rejected(self):
        with pytest.raises(ValidationError):
            PurchaseRequest(**{**BASE, "card_number": "123"})

    def test_client_cannot_set_transaction_time(self):
        p = PurchaseRequest(**BASE, transaction_time="2020-01-01T00:00:00Z")
        assert not hasattr(p, "transaction_time")


class TestPartialUpdates:
    def test_limits_update_remembers_which_fields_were_sent(self):
        assert CardLimitsUpdate(daily_limit="10").model_fields_set == {"daily_limit"}

    def test_explicit_null_limit_counts_as_sent(self):
        u = CardLimitsUpdate(weekly_limit=None)
        assert u.model_fields_set == {"weekly_limit"} and u.weekly_limit is None

    def test_empty_update_changes_nothing(self):
        assert CardLimitsUpdate().model_fields_set == set()

    def test_limit_must_be_positive(self):
        with pytest.raises(ValidationError):
            CardLimitsUpdate(daily_limit="0")

    def test_driver_name_cannot_be_nulled_but_phone_can(self):
        assert DriverUpdate(phone=None).model_fields_set == {"phone"}
        with pytest.raises(ValidationError, match="full_name cannot be null"):
            DriverUpdate(full_name=None)

    def test_truck_capacity_cannot_be_nulled(self):
        with pytest.raises(ValidationError):
            TruckUpdate(tank_capacity_gal=None)


class TestOtherSchemas:
    def test_truck_capacity_must_be_positive(self):
        with pytest.raises(ValidationError):
            TruckCreate(plate_number="AB-1", tank_capacity_gal="0")

    def test_card_create_limits_optional(self):
        c = CardCreate(card_number="1234", driver_id=1)
        assert c.daily_limit is None

    def test_review_accepts_only_reviewed_or_dismissed(self):
        assert ReviewRequest(review_status="reviewed").review_status == ReviewStatus.REVIEWED
        with pytest.raises(ValidationError):
            ReviewRequest(review_status="none")

    @pytest.mark.parametrize(
        "status, authorized",
        [("pending", True), ("approved", True), ("declined", False)],
    )
    def test_authorized_is_false_only_for_declined(self, status, authorized):
        now = datetime.now(timezone.utc)
        tx = TransactionRead(
            id=1, idempotency_key="k" * 8, card_id=1, driver_id=1, truck_id=None, station_id=1,
            gallons="1", total_price="1", transaction_time=now, status=TransactionStatus(status),
            decline_reason=None, review_status="none", created_at=now,
        )  # fmt: skip
        assert tx.authorized is authorized
        assert tx.model_dump()["authorized"] is authorized
