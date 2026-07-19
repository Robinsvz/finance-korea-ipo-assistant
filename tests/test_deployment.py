from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_compose_separates_migration_scheduler_and_bot():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {"migrate", "scheduler", "telegram-bot"}.issubset(services)
    assert services["scheduler"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["telegram-bot"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["scheduler"]["stop_grace_period"] == "30s"
    assert services["telegram-bot"]["stop_grace_period"] == "30s"
    for service in services.values():
        assert "ipo-data:/app/data" in service["volumes"]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
    assert "ipo-data" in compose["volumes"]


def test_dockerfile_is_non_root_locked_and_health_checked():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    lock = (ROOT / "requirements.runtime.lock").read_text(encoding="utf-8")
    assert "pip install -r requirements.runtime.lock" in dockerfile
    assert "requirements.lock" not in dockerfile
    assert "COPY tests" not in dockerfile
    assert "USER ipo" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "finance-datareader==" in lock
    assert "greenlet==" in lock
    assert "pykrx==" in lock
    for package in ("pytest==", "pytest-cov==", "coverage==", "ruff=="):
        assert package not in lock


def test_docker_context_excludes_local_and_test_artifacts():
    ignored = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {
        ".git",
        ".env",
        ".venv",
        "data",
        "tests",
        "analysis",
        "scripts",
        "pyproject.toml",
        "requirements.lock",
    }.issubset(ignored)
