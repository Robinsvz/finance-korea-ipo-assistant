from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import DemandForecast, DiligenceReview, Ipo, NewsMetric
from app.services.diligence import DiligenceCriteria, build_diligence_report


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, future=True)()


def test_diligence_passes_when_disclosure_and_articles_are_reviewed():
    session = _session()
    ipo = Ipo(
        corp_name="테스트바이오",
        normalized_name="테스트바이오",
        status="scheduled",
        dart_corp_code="12345678",
        dart_receipt_no="20260627000001",
        subscription_start_date=date.today() + timedelta(days=3),
    )
    session.add(ipo)
    session.flush()
    session.add(
        DemandForecast(
            ipo_id=ipo.id,
            institutional_competition_ratio=1200,
            lockup_commitment_ratio=0.18,
            source="opendart",
        )
    )
    session.add(
        NewsMetric(
            ipo_id=ipo.id,
            window_start=date.today() - timedelta(days=30),
            window_end=date.today(),
            total_article_count=5,
            deduped_article_count=3,
            major_media_count=1,
            recent_7d_article_count=2,
            previous_7d_article_count=1,
            article_growth_rate_7d=1.0,
            positive_keyword_count=1,
            negative_keyword_count=0,
            risk_keyword_count=0,
            top_keywords_json=[{"keyword": "성장", "count": 2}],
            top_articles_json=[
                {"title": "테스트바이오 IPO", "link": "https://example.com/article"}
            ],
            summary="최근 기사 검토 완료.",
        )
    )
    session.add_all(
        [
            DiligenceReview(
                ipo_id=ipo.id,
                review_type="disclosure",
                review_status="reviewed",
                reviewed_by="tester",
                reviewed_at=datetime.now(timezone.utc),
            ),
            DiligenceReview(
                ipo_id=ipo.id,
                review_type="articles",
                review_status="reviewed",
                reviewed_by="tester",
                reviewed_at=datetime.now(timezone.utc),
            ),
        ]
    )
    session.flush()

    report = build_diligence_report(session, [ipo], DiligenceCriteria())

    assert report["summary"] == {"total": 1, "completed": 1, "pending": 0}
    item = report["items"][0]
    assert item["completed"] is True
    assert item["criteria"]["disclosure_review"]["score"] == 100
    assert item["criteria"]["article_review"]["score"] == 100


def test_diligence_marks_missing_disclosure_and_articles_pending():
    session = _session()
    ipo = Ipo(
        corp_name="미검토스팩",
        normalized_name="미검토스팩",
        status="scheduled",
        subscription_start_date=date.today() + timedelta(days=3),
    )
    session.add(ipo)
    session.flush()

    report = build_diligence_report(session, [ipo], DiligenceCriteria())

    assert report["summary"] == {"total": 1, "completed": 0, "pending": 1}
    item = report["items"][0]
    assert item["completed"] is False
    assert "dart_corp_code" in item["criteria"]["disclosure_review"]["missing"]
    assert "news_metrics" in item["criteria"]["article_review"]["missing"]


def test_collected_evidence_is_not_manual_review():
    session = _session()
    ipo = Ipo(
        corp_name="수집기업",
        normalized_name="수집기업",
        status="scheduled",
        dart_corp_code="1",
        dart_receipt_no="R1",
    )
    session.add(ipo)
    session.flush()
    session.add(
        DemandForecast(
            ipo_id=ipo.id,
            receipt_no="R1",
            institutional_competition_ratio=1000,
            source="opendart",
        )
    )
    session.flush()
    report = build_diligence_report(session, [ipo])
    disclosure = report["items"][0]["criteria"]["disclosure_review"]
    assert disclosure["passed"] is False
    assert "manual_disclosure_review" in disclosure["missing"]
