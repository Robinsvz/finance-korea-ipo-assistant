from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from app.models import Ipo


@dataclass
class IpoScheduleRecord:
    corp_name: str
    market: Optional[str] = None
    kind_biz_no: Optional[str] = None
    report_filed_date: Optional[date] = None
    demand_start_date: Optional[date] = None
    demand_end_date: Optional[date] = None
    subscription_start_date: Optional[date] = None
    subscription_end_date: Optional[date] = None
    refund_date: Optional[date] = None
    payment_date: Optional[date] = None
    listing_date: Optional[date] = None
    offer_price_final: Optional[int] = None
    offer_amount_million: Optional[int] = None
    underwriters: List[str] = field(default_factory=list)
    source: str = "kind"
    source_confidence: float = 0.8
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AllocationResult:
    ipo_id: int
    broker_name: str
    user_cash: int
    offer_price: int
    apply_shares: int
    expected_equal_shares_low: float
    expected_equal_shares_mid: float
    expected_equal_shares_high: float
    expected_proportional_shares_conservative: float
    expected_proportional_shares_base: float
    expected_proportional_shares_optimistic: float
    expected_total_shares_base: float
    estimated_fee: int
    estimated_required_deposit: int
    assumption_source: str = "brokers.yaml"
    assumption_updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class MonthlyUniverseEntry:
    """One canonical offering and the events that place it in a monthly cohort."""

    ipo: "Ipo"
    ipo_id: int
    offering_id: str
    event_badges: tuple[str, ...]
    inclusion_dates: Dict[str, tuple[date, ...]]
    reconciliation_state: str
    planned_listing_date: Optional[date]
    actual_listing_date: Optional[date]
    display_listing_date: Optional[date]


@dataclass(frozen=True)
class MonthlyUniverseResult:
    """Core monthly activity plus filing-only rows excluded from the core count."""

    month_start: date
    month_end: date
    core: tuple[MonthlyUniverseEntry, ...]
    filing_update_appendix: tuple[MonthlyUniverseEntry, ...]

    @property
    def core_count(self) -> int:
        return len(self.core)


@dataclass
class HealthStatus:
    database_ok: bool
    schema_current: bool
    schema_revision: Optional[str]
    expected_schema_revision: Optional[str]
    telegram_configured: bool
    email_configured: bool
    dart_configured: bool
    naver_configured: bool
    database_url: str
