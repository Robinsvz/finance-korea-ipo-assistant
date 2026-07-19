from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db import Base
from app.services.clock import local_today


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Ipo(Base, TimestampMixin):
    __tablename__ = "ipos"

    id = Column(Integer, primary_key=True)
    identity_key = Column(
        String(320),
        default=lambda: f"temp:{uuid.uuid4().hex}",
        nullable=False,
        unique=True,
        index=True,
    )
    offering_id = Column(
        String(320),
        default=lambda: f"offering:temp:{uuid.uuid4().hex}",
        nullable=False,
        unique=True,
        index=True,
    )
    corp_name = Column(String(255), nullable=False)
    normalized_name = Column(String(255), nullable=False, index=True)
    ticker = Column(String(20), index=True)
    market = Column(String(50))
    industry = Column(String(255))
    ipo_type = Column(String(50))
    is_sample = Column(Boolean, default=False, nullable=False, index=True)
    data_origin = Column(String(50), default="unknown", nullable=False, index=True)
    status = Column(String(50), default="scheduled", index=True)
    status_changed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    schedule_version = Column(Integer, default=1, nullable=False)
    needs_review = Column(Boolean, default=False, nullable=False)
    report_filed_date = Column(Date)
    demand_start_date = Column(Date, index=True)
    demand_end_date = Column(Date, index=True)
    offer_amount_million = Column(Integer)
    offer_price_min = Column(Integer)
    offer_price_max = Column(Integer)
    offer_price_final = Column(Integer)
    subscription_start_date = Column(Date, index=True)
    subscription_end_date = Column(Date, index=True)
    refund_date = Column(Date)
    refund_date_source = Column(String(100))
    refund_date_verified_at = Column(DateTime(timezone=True))
    payment_date = Column(Date)
    payment_date_source = Column(String(100))
    payment_date_verified_at = Column(DateTime(timezone=True))
    listing_date = Column(Date, index=True)
    actual_listing_date = Column(Date, index=True)
    dart_corp_code = Column(String(20), index=True)
    dart_receipt_no = Column(String(30))
    kind_biz_no = Column(String(40), index=True)
    source_confidence = Column(Float, default=0.0)

    underwriters = relationship(
        "UnderwriterAllocation", back_populates="ipo", cascade="all, delete-orphan"
    )
    demand_forecasts = relationship(
        "DemandForecast", back_populates="ipo", cascade="all, delete-orphan"
    )
    financial_metrics = relationship(
        "FinancialMetric", back_populates="ipo", cascade="all, delete-orphan"
    )
    news_metrics = relationship("NewsMetric", back_populates="ipo", cascade="all, delete-orphan")
    search_trends = relationship("SearchTrend", back_populates="ipo", cascade="all, delete-orphan")
    scores = relationship("ScoreSnapshot", back_populates="ipo", cascade="all, delete-orphan")
    participations = relationship(
        "UserParticipation", back_populates="ipo", cascade="all, delete-orphan"
    )
    source_documents = relationship(
        "SourceDocument", back_populates="ipo", cascade="all, delete-orphan"
    )
    fact_observations = relationship(
        "IpoFactObservation", back_populates="ipo", cascade="all, delete-orphan"
    )
    schedule_revisions = relationship(
        "IpoScheduleRevision", back_populates="ipo", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "normalized_name", "subscription_start_date", name="uq_ipo_name_sub_start"
        ),
    )


class UnderwriterAllocation(Base):
    __tablename__ = "underwriter_allocations"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    broker_name = Column(String(255), nullable=False, index=True)
    role = Column(String(50))
    public_offer_shares = Column(Integer)
    retail_allocated_shares = Column(Integer)
    equal_allocation_ratio = Column(Float, default=0.5)
    proportional_allocation_ratio = Column(Float, default=0.5)
    min_subscription_shares = Column(Integer, default=10)
    max_subscription_shares = Column(Integer)
    deposit_rate = Column(Float, default=0.5)
    subscription_fee = Column(Integer, default=0)
    account_open_deadline = Column(Date)
    subscription_unit_rule_id = Column(String(100), default="default")
    expected_applicants_count = Column(Integer)
    proportional_competition_conservative = Column(Float)
    proportional_competition_base = Column(Float)
    proportional_competition_optimistic = Column(Float)
    actual_applicants_count = Column(Integer)
    actual_proportional_competition = Column(Float)
    actual_source = Column(String(100))
    actual_recorded_at = Column(DateTime(timezone=True))
    assumption_source = Column(String(100))
    assumption_updated_at = Column(DateTime(timezone=True))
    source = Column(String(100))
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    ipo = relationship("Ipo", back_populates="underwriters")

    __table_args__ = (Index("uq_underwriter_ipo_broker_idx", "ipo_id", "broker_name", unique=True),)


class DemandForecast(Base):
    __tablename__ = "demand_forecasts"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    institutional_competition_ratio = Column(Float)
    lockup_commitment_ratio = Column(Float)
    price_band_top_ratio = Column(Float)
    above_band_participation_ratio = Column(Float)
    final_price_position = Column(String(50))
    participants_count = Column(Integer)
    result_published_at = Column(DateTime(timezone=True))
    source = Column(String(100))
    receipt_no = Column(String(30))
    raw_text_hash = Column(String(64))
    evidence_json = Column(JSON)
    parser_version = Column(String(50), default="dart-tables-v1")
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    ipo = relationship("Ipo", back_populates="demand_forecasts")

    __table_args__ = (Index("uq_demand_ipo_receipt_idx", "ipo_id", "receipt_no", unique=True),)


class FinancialStatement(Base):
    __tablename__ = "financial_statements"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    fiscal_year = Column(Integer, index=True)
    statement_type = Column(String(20))
    account_name = Column(String(255), index=True)
    account_code = Column(String(100))
    amount = Column(Numeric(24, 2))
    currency = Column(String(20), default="KRW")
    source = Column(String(100))
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        Index(
            "uq_financial_statement_idx",
            "ipo_id",
            "fiscal_year",
            "statement_type",
            "account_code",
            unique=True,
        ),
    )


class FinancialMetric(Base):
    __tablename__ = "financial_metrics"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    fiscal_year = Column(Integer, index=True)
    source = Column(String(100), default="opendart", nullable=False)
    revenue = Column(Numeric(24, 2))
    revenue_yoy_growth = Column(Float)
    revenue_yoy_status = Column(String(30))
    gross_profit = Column(Numeric(24, 2))
    gross_margin = Column(Float)
    operating_profit = Column(Numeric(24, 2))
    operating_margin = Column(Float)
    net_income = Column(Numeric(24, 2))
    net_margin = Column(Float)
    total_assets = Column(Numeric(24, 2))
    total_liabilities = Column(Numeric(24, 2))
    total_equity = Column(Numeric(24, 2))
    debt_ratio = Column(Float)
    operating_cash_flow = Column(Numeric(24, 2))
    free_cash_flow = Column(Numeric(24, 2))
    roe = Column(Float)
    roa = Column(Float)
    is_profitable = Column(Boolean)
    has_negative_ocf = Column(Boolean)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    ipo = relationship("Ipo", back_populates="financial_metrics")

    __table_args__ = (
        Index("uq_financial_metric_idx", "ipo_id", "fiscal_year", "source", unique=True),
    )


class ValuationMetric(Base):
    __tablename__ = "valuation_metrics"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    as_of_date = Column(Date, nullable=False, default=local_today)
    source = Column(String(100), default="manual", nullable=False)
    market_cap_at_offer = Column(Numeric(24, 2))
    enterprise_value_estimate = Column(Numeric(24, 2))
    per = Column(Float)
    pbr = Column(Float)
    psr = Column(Float)
    ev_to_sales = Column(Float)
    ev_to_ebitda = Column(Float)
    peer_group_json = Column(JSON)
    peer_median_per = Column(Float)
    peer_median_psr = Column(Float)
    valuation_discount_premium_ratio = Column(Float)
    valuation_comment = Column(Text)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        Index("uq_valuation_metric_idx", "ipo_id", "as_of_date", "source", unique=True),
    )


class IpoStructureMetric(Base):
    __tablename__ = "ipo_structure_metrics"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    total_offer_shares = Column(Integer)
    new_share_ratio = Column(Float)
    secondary_share_ratio = Column(Float)
    free_float_shares_on_listing = Column(Integer)
    as_of_date = Column(Date, nullable=False, default=local_today)
    source = Column(String(100), default="manual", nullable=False)
    free_float_market_cap = Column(Numeric(24, 2))
    free_float_ratio_on_listing = Column(Float)
    vc_overhang_ratio = Column(Float)
    lockup_expiry_schedule_json = Column(JSON)
    use_of_proceeds_summary = Column(Text)
    risk_flags_json = Column(JSON)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        Index("uq_structure_metric_idx", "ipo_id", "as_of_date", "source", unique=True),
    )


class NewsMetric(Base):
    __tablename__ = "news_metrics"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    window_start = Column(Date)
    window_end = Column(Date)
    source = Column(String(100), default="naver_news", nullable=False)
    api_total_count = Column(Integer)
    total_article_count = Column(Integer)
    deduped_article_count = Column(Integer)
    major_media_count = Column(Integer)
    recent_7d_article_count = Column(Integer)
    previous_7d_article_count = Column(Integer)
    article_growth_rate_7d = Column(Float)
    positive_keyword_count = Column(Integer)
    negative_keyword_count = Column(Integer)
    risk_keyword_count = Column(Integer)
    top_keywords_json = Column(JSON)
    top_articles_json = Column(JSON)
    summary = Column(Text)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    ipo = relationship("Ipo", back_populates="news_metrics")

    __table_args__ = (
        Index("uq_news_metric_idx", "ipo_id", "window_start", "window_end", "source", unique=True),
    )


class SearchTrend(Base):
    __tablename__ = "search_trends"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    source = Column(String(100))
    keyword_group = Column(String(255))
    window_start = Column(Date)
    window_end = Column(Date)
    baseline_avg_index = Column(Float)
    recent_avg_index = Column(Float)
    peak_index = Column(Float)
    trend_growth_rate = Column(Float)
    trend_acceleration = Column(Float)
    zero_data_ratio = Column(Float)
    raw_series_json = Column(JSON)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    ipo = relationship("Ipo", back_populates="search_trends")

    __table_args__ = (
        Index(
            "uq_search_trend_idx",
            "ipo_id",
            "keyword_group",
            "window_start",
            "window_end",
            "source",
            unique=True,
        ),
    )


class MarketConditionSnapshot(Base):
    __tablename__ = "market_condition_snapshots"

    id = Column(Integer, primary_key=True)
    as_of_date = Column(Date, nullable=False, index=True)
    kosdaq_momentum_20d = Column(Float)
    recent_ipo_success_rate = Column(Float)
    volatility_20d = Column(Float)
    source = Column(String(100), default="pykrx+outcomes", nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("as_of_date", name="uq_market_condition_date"),)


class ScoreSnapshot(Base):
    __tablename__ = "score_snapshots"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    score_date = Column(Date, nullable=False, index=True)
    calculated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    confidence_score = Column(Float, default=0, nullable=False)
    total_score = Column(Float, nullable=False)
    demand_score = Column(Float, default=0)
    financial_score = Column(Float, default=0)
    valuation_score = Column(Float, default=0)
    structure_score = Column(Float, default=0)
    news_trend_score = Column(Float, default=0)
    allocation_score = Column(Float, default=0)
    market_condition_score = Column(Float, default=0)
    risk_penalty_score = Column(Float, default=0)
    recommendation_band = Column(String(50), nullable=False)
    missing_features_json = Column(JSON)
    explanation_json = Column(JSON)
    model_version = Column(String(50), default="rules-v1")
    rules_hash = Column(String(64))
    input_fingerprint = Column(String(64), default=lambda: uuid.uuid4().hex, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    ipo = relationship("Ipo", back_populates="scores")

    __table_args__ = (
        Index(
            "uq_score_snapshot_idx",
            "ipo_id",
            "score_date",
            "model_version",
            "input_fingerprint",
            unique=True,
        ),
    )


class AllocationScenario(Base):
    __tablename__ = "allocation_scenarios"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    broker_name = Column(String(255))
    user_cash = Column(Integer)
    offer_price = Column(Integer)
    apply_shares = Column(Integer)
    expected_equal_shares_low = Column(Float)
    expected_equal_shares_mid = Column(Float)
    expected_equal_shares_high = Column(Float)
    expected_proportional_shares_conservative = Column(Float)
    expected_proportional_shares_base = Column(Float)
    expected_proportional_shares_optimistic = Column(Float)
    expected_total_shares_base = Column(Float)
    estimated_fee = Column(Integer)
    estimated_required_deposit = Column(Integer)
    assumption_source = Column(String(100))
    assumption_updated_at = Column(DateTime(timezone=True))
    calculation_version = Column(String(50), default="allocation-v1")
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class UserParticipation(Base, TimestampMixin):
    __tablename__ = "user_participations"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    score_snapshot_id = Column(Integer, ForeignKey("score_snapshots.id"), index=True)
    broker_name = Column(String(255), nullable=False, index=True)
    decision_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    decision_action = Column(String(50), nullable=False)
    user_cash = Column(Integer, nullable=False)
    applied_shares = Column(Integer, nullable=False)
    cash_committed = Column(Integer, nullable=False)
    allocated_shares = Column(Integer)
    offer_price_snapshot = Column(Numeric(18, 2))
    subscription_fee = Column(Integer, default=0, nullable=False)
    note = Column(Text)

    ipo = relationship("Ipo", back_populates="participations")
    exits = relationship(
        "ParticipationExit", back_populates="participation", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("ipo_id", "broker_name", name="uq_user_ipo_broker"),)


class ParticipationExit(Base):
    __tablename__ = "participation_exits"

    id = Column(Integer, primary_key=True)
    participation_id = Column(
        Integer, ForeignKey("user_participations.id"), nullable=False, index=True
    )
    sold_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    strategy = Column(String(50), default="MANUAL", nullable=False)
    sold_shares = Column(Integer, nullable=False)
    sell_price = Column(Numeric(18, 2), nullable=False)
    selling_cost = Column(Integer, default=0, nullable=False)
    note = Column(Text)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    participation = relationship("UserParticipation", back_populates="exits")


class ListingPrice(Base):
    __tablename__ = "listing_prices"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    ticker = Column(String(20), index=True)
    listing_date = Column(Date, index=True)
    d0_trade_date = Column(Date)
    d1_trade_date = Column(Date)
    d3_trade_date = Column(Date)
    d0_open_price = Column(Numeric(18, 2))
    d0_high_price = Column(Numeric(18, 2))
    d0_low_price = Column(Numeric(18, 2))
    d0_close_price = Column(Numeric(18, 2))
    d0_volume = Column(Float)
    d0_turnover_value = Column(Numeric(24, 2))
    d1_close_price = Column(Numeric(18, 2))
    d1_volume = Column(Float)
    d3_close_price = Column(Numeric(18, 2))
    d3_volume = Column(Float)
    source = Column(String(100))
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        Index("uq_listing_price_idx", "ipo_id", "listing_date", "source", unique=True),
    )


class OutcomeEvaluation(Base):
    __tablename__ = "outcome_evaluations"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    score_snapshot_id = Column(Integer, ForeignKey("score_snapshots.id"))
    evaluation_version = Column(String(50), default="outcome-v2", nullable=False)
    offer_price = Column(Numeric(18, 2))
    d0_open_return_pct = Column(Float)
    d0_close_return_pct = Column(Float)
    d1_close_return_pct = Column(Float)
    d3_close_return_pct = Column(Float)
    open_to_d0_close_return_pct = Column(Float)
    max_drawdown_d0_pct = Column(Float)
    estimated_profit_per_share_open = Column(Numeric(18, 2))
    estimated_profit_per_share_d1 = Column(Numeric(18, 2))
    estimated_profit_per_share_d3 = Column(Numeric(18, 2))
    label_open_success = Column(Boolean)
    label_d1_success = Column(Boolean)
    label_d3_success = Column(Boolean)
    prediction_was_correct = Column(Boolean)
    error_reason = Column(Text)
    evaluated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index(
            "uq_outcome_evaluation_idx",
            "ipo_id",
            "score_snapshot_id",
            "evaluation_version",
            unique=True,
        ),
    )


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"

    id = Column(Integer, primary_key=True)
    source = Column(String(100), nullable=False, index=True)
    source_key = Column(String(255), nullable=False, index=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), index=True)
    fetched_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    raw_text_hash = Column(String(64), index=True)
    payload_json = Column(JSON)
    raw_text = Column(Text)

    __table_args__ = (
        Index("uq_source_snapshot_idx", "source", "source_key", "raw_text_hash", unique=True),
    )


class SourceDocument(Base):
    """One immutable source revision belonging to a stable IPO offering."""

    __tablename__ = "source_documents"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    source_snapshot_id = Column(Integer, ForeignKey("source_snapshots.id"), index=True)
    source = Column(String(100), nullable=False, index=True)
    document_key = Column(String(255), nullable=False)
    document_type = Column(String(100), nullable=False, index=True)
    lineage_root_key = Column(String(255), nullable=False, index=True)
    supersedes_document_id = Column(Integer, ForeignKey("source_documents.id"), index=True)
    official_url = Column(Text)
    published_at = Column(DateTime(timezone=True), index=True)
    fetched_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    raw_text_hash = Column(String(64), index=True)
    parser_name = Column(String(100))
    parser_version = Column(String(50))
    metadata_json = Column(JSON)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    ipo = relationship("Ipo", back_populates="source_documents")

    __table_args__ = (UniqueConstraint("source", "document_key", name="uq_source_document_key"),)


class IpoFactObservation(Base):
    """Append-only field value with point-in-time provenance."""

    __tablename__ = "ipo_fact_observations"

    id = Column(Integer, primary_key=True)
    observation_key = Column(String(64), nullable=False, unique=True, index=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    source_document_id = Column(Integer, ForeignKey("source_documents.id"), index=True)
    field_name = Column(String(120), nullable=False, index=True)
    value_json = Column(JSON)
    unit = Column(String(50))
    quality_state = Column(String(40), nullable=False, index=True)
    knowledge_phase = Column(String(40), nullable=False, index=True)
    published_at = Column(DateTime(timezone=True), index=True)
    observed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    fetched_at = Column(DateTime(timezone=True), index=True)
    effective_from = Column(DateTime(timezone=True), index=True)
    effective_to = Column(DateTime(timezone=True), index=True)
    parser_name = Column(String(100))
    parser_version = Column(String(50))
    raw_text_hash = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    ipo = relationship("Ipo", back_populates="fact_observations")


class IpoScheduleRevision(Base):
    """Append-only schedule state emitted by a particular source revision."""

    __tablename__ = "ipo_schedule_revisions"

    id = Column(Integer, primary_key=True)
    revision_key = Column(String(64), nullable=False, unique=True, index=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    source_document_id = Column(Integer, ForeignKey("source_documents.id"), index=True)
    demand_start_date = Column(Date)
    demand_end_date = Column(Date)
    subscription_start_date = Column(Date)
    subscription_end_date = Column(Date)
    refund_date = Column(Date)
    payment_date = Column(Date)
    planned_listing_date = Column(Date)
    actual_listing_date = Column(Date)
    event_status = Column(String(50), nullable=False, index=True)
    withdrawal_reason = Column(Text)
    published_at = Column(DateTime(timezone=True), index=True)
    observed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    effective_from = Column(DateTime(timezone=True), index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    ipo = relationship("Ipo", back_populates="schedule_revisions")


class MonthlyUniverseSnapshot(Base):
    """An append-only reconciliation result for one IPO in one calendar month."""

    __tablename__ = "monthly_universe_snapshots"

    id = Column(Integer, primary_key=True)
    month_start = Column(Date, nullable=False, index=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    offering_id_snapshot = Column(String(320), nullable=False, index=True)
    cohort = Column(String(40), nullable=False, index=True)
    event_badges_json = Column(JSON, nullable=False)
    inclusion_dates_json = Column(JSON, nullable=False)
    reconciliation_state = Column(String(40), nullable=False, index=True)
    reconciliation_sources_json = Column(JSON)
    snapshot_hash = Column(String(64), nullable=False)
    captured_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "month_start",
            "ipo_id",
            "snapshot_hash",
            name="uq_monthly_universe_snapshot",
        ),
    )


class BrokerTermVersion(Base):
    """Versioned official broker terms; never mixed with allocation assumptions."""

    __tablename__ = "broker_term_versions"

    id = Column(Integer, primary_key=True)
    version_key = Column(String(64), nullable=False, unique=True, index=True)
    broker_name = Column(String(255), nullable=False, index=True)
    source_document_id = Column(Integer, ForeignKey("source_documents.id"), index=True)
    effective_from = Column(Date, nullable=False, index=True)
    effective_to = Column(Date, index=True)
    subscription_fee = Column(Integer)
    account_open_deadline_rule = Column(String(255))
    minimum_subscription_shares = Column(Integer)
    equal_allocation_ratio = Column(Float)
    proportional_allocation_ratio = Column(Float)
    terms_json = Column(JSON)
    official_url = Column(Text)
    verified_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class DataQualityIssue(Base):
    """Review queue for missing, conflicting, stale, ambiguous, or leaking facts."""

    __tablename__ = "data_quality_issues"

    id = Column(Integer, primary_key=True)
    issue_key = Column(String(64), nullable=False, unique=True, index=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), index=True)
    source_document_id = Column(Integer, ForeignKey("source_documents.id"), index=True)
    issue_type = Column(String(40), nullable=False, index=True)
    field_name = Column(String(120), index=True)
    severity = Column(String(20), nullable=False, index=True)
    status = Column(String(30), nullable=False, index=True)
    details_json = Column(JSON)
    detected_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at = Column(DateTime(timezone=True))
    resolution_note = Column(Text)


class DiligenceReview(Base, TimestampMixin):
    __tablename__ = "diligence_reviews"

    id = Column(Integer, primary_key=True)
    ipo_id = Column(Integer, ForeignKey("ipos.id"), nullable=False, index=True)
    review_type = Column(String(50), nullable=False)
    review_status = Column(String(30), default="collected", nullable=False, index=True)
    reviewed_by = Column(String(255))
    reviewed_at = Column(DateTime(timezone=True))
    evidence_refs_json = Column(JSON)
    review_note = Column(Text)

    __table_args__ = (UniqueConstraint("ipo_id", "review_type", name="uq_diligence_review"),)


class JobRun(Base):
    __tablename__ = "job_runs"

    id = Column(Integer, primary_key=True)
    run_id = Column(String(64), nullable=False, unique=True, index=True)
    job_name = Column(String(100), nullable=False, index=True)
    status = Column(String(30), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at = Column(DateTime(timezone=True))
    processed_count = Column(Integer, default=0, nullable=False)
    success_count = Column(Integer, default=0, nullable=False)
    partial_count = Column(Integer, default=0, nullable=False)
    skipped_count = Column(Integer, default=0, nullable=False)
    failure_count = Column(Integer, default=0, nullable=False)
    result_json = Column(JSON)
    error_summary = Column(Text)


class NotificationEvent(Base):
    __tablename__ = "notification_events"

    id = Column(Integer, primary_key=True)
    channel = Column(String(50), nullable=False)
    event_key = Column(String(255), nullable=False)
    recipient = Column(String(255), nullable=False)
    status = Column(String(30), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    lease_until = Column(DateTime(timezone=True), index=True)
    attempt_count = Column(Integer, default=0, nullable=False)
    sent_at = Column(DateTime(timezone=True))
    response_json = Column(JSON)
    error_summary = Column(Text)

    content_artifact = relationship(
        "NotificationContentArtifact",
        back_populates="notification_event",
        cascade="all, delete-orphan",
        uselist=False,
    )

    __table_args__ = (
        UniqueConstraint("channel", "event_key", "recipient", name="uq_notification_event"),
    )


class NotificationContentArtifact(Base):
    """Immutable, credential-free copy of content attached to one delivery attempt."""

    __tablename__ = "notification_content_artifacts"

    id = Column(Integer, primary_key=True)
    notification_event_id = Column(
        Integer,
        ForeignKey("notification_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject = Column(Text, nullable=False)
    text_body = Column(Text, nullable=False)
    html_body = Column(Text, nullable=False)
    subject_sha256 = Column(String(64), nullable=False)
    text_sha256 = Column(String(64), nullable=False)
    html_sha256 = Column(String(64), nullable=False)
    content_sha256 = Column(String(64), nullable=False, index=True)
    content_redacted = Column(Boolean, default=False, nullable=False)
    universe_hash = Column(String(64))
    evidence_hash = Column(String(64))
    metadata_json = Column(JSON)
    recorded_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    notification_event = relationship("NotificationEvent", back_populates="content_artifact")

    __table_args__ = (
        UniqueConstraint(
            "notification_event_id",
            name="uq_notification_content_artifact_event",
        ),
    )
