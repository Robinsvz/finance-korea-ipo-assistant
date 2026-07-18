from app.services.jobs import _run_operations


def test_batch_retries_only_failed_item_and_continues():
    calls = {"A": 0, "B": 0}

    def operation(name):
        calls[name] += 1
        if name == "A" and calls[name] == 1:
            raise RuntimeError("temporary")
        return {"ipo": name, "updated": True}

    result = _run_operations(["A", "B"], operation)
    assert result["processed"] == 2
    assert result["success"] == 2
    assert result["failed"] == 0
    assert result["retried"] == 1
    assert calls == {"A": 2, "B": 1}


def test_batch_records_final_failure_without_stopping_other_items():
    def operation(name):
        if name == "A":
            raise RuntimeError("permanent")
        return {"ipo": name}

    result = _run_operations(["A", "B"], operation)
    assert result["success"] == 1
    assert result["failed"] == 1
    assert result["failures"][0]["attempts"] == 2


def test_batch_does_not_count_skipped_or_partial_as_success():
    responses = {
        "A": {"updated": True},
        "B": {"skipped": True, "reason": "credential missing"},
        "C": {"updated": True, "errors": ["trend failed"]},
        "D": {"updated": False, "error": "source rejected"},
    }

    result = _run_operations(list(responses), responses.__getitem__)

    assert result["success"] == 1
    assert result["skipped"] == 1
    assert result["partial"] == 1
    assert result["failed"] == 1
