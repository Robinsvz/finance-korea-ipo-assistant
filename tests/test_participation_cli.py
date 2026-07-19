from datetime import date
from types import SimpleNamespace

from app import cli


def test_participation_cli_pins_active_score_lineage(monkeypatch):
    active_score = SimpleNamespace(id=10)
    legacy_score = SimpleNamespace(id=99)
    ipo = SimpleNamespace(
        id=1,
        subscription_start_date=date(2026, 7, 20),
        actual_listing_date=None,
        listing_date=date(2026, 7, 30),
    )
    captured = {}

    class Scope:
        def __enter__(self):
            return "session"

        def __exit__(self, *_args):
            return None

    def select_score(*_args, **kwargs):
        captured["selection"] = kwargs
        if kwargs == {"model_version": "rules-v3-simple", "rules_hash": "active-hash"}:
            return active_score
        return legacy_score

    def record(*_args, **kwargs):
        captured["recorded_score"] = kwargs["score"]
        return SimpleNamespace(id=1)

    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "session_scope", lambda: Scope())
    monkeypatch.setattr(cli, "_find_or_exit", lambda *_args: ipo)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(default_user_cash=10_000_000),
    )
    monkeypatch.setattr(
        cli,
        "resolve_scoring_identity",
        lambda: ("rules-v3-simple", "active-hash"),
    )
    monkeypatch.setattr(cli, "latest_actionable_score", select_score)
    monkeypatch.setattr(cli, "record_participation", record)
    monkeypatch.setattr(
        cli,
        "summarize_participation",
        lambda _row: SimpleNamespace(to_dict=lambda: {"participation_id": 1}),
    )
    monkeypatch.setattr(cli, "print_json", lambda _result: None)
    args = SimpleNamespace(
        name="계보기업",
        broker="한국투자증권",
        cash=None,
        applied_shares=10,
        cash_committed="50000",
        allocated_shares=None,
        fee=0,
        action="RESEARCH_ONLY",
        decision_at=None,
        note="",
    )

    cli.cmd_record_participation(args)

    assert captured["selection"] == {
        "model_version": "rules-v3-simple",
        "rules_hash": "active-hash",
    }
    assert captured["recorded_score"] is active_score
    assert captured["recorded_score"] is not legacy_score
