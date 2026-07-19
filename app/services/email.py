from __future__ import annotations

import hashlib
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Mapping, Optional

from app.config import get_settings, validate_email_mailbox
from app.services.observability import (
    finish_notification,
    mask_secrets,
    record_notification_content,
    reserve_notification,
)

SMTP_SECRET_PATTERNS = (
    re.compile(r"(?i)(smtp[_-]?password|password|passwd)=([^&\s]+)"),
    re.compile(r"(?i)(smtps?://[^:/\s]+:)([^@/\s]+)(@)"),
)


@dataclass(frozen=True)
class EmailContent:
    subject: str
    text: str
    html: str


@dataclass(frozen=True)
class EmailAuditContext:
    universe_hash: Optional[str] = None
    evidence_hash: Optional[str] = None
    metadata: Optional[Mapping[str, Any]] = None


def send_email_message(
    content: EmailContent,
    *,
    event_key: str,
    recipients: Optional[tuple[str, ...]] = None,
    audit_context: Optional[EmailAuditContext] = None,
) -> dict[str, Any]:
    """Send one independently tracked MIME message per recipient.

    SMTP cannot guarantee exactly-once delivery after an ambiguous network timeout. A
    deterministic Message-ID and the notification lease make retries observable and
    suppress normal duplicate job executions, without claiming stronger semantics.
    """

    settings = get_settings()
    selected_recipients = _normalize_recipients(
        settings.email_recipients if recipients is None else recipients
    )
    state = _configuration_state(settings, selected_recipients)
    if state == "absent":
        return {
            "processed": 0,
            "sent": 0,
            "duplicate": 0,
            "failed": 0,
            "delivery_state_unknown": 0,
            "skipped": True,
            "reason": "Email is not configured",
            "failures": [],
        }
    if state == "partial":
        raise ValueError("Email configuration is incomplete")

    _validate_delivery_settings(settings)
    _validate_content(content, event_key)
    _validate_audit_context(audit_context)
    sender_address = _mailbox(settings.email_from, "EMAIL_FROM")
    if settings.email_reply_to:
        _mailbox(settings.email_reply_to, "EMAIL_REPLY_TO")
    for recipient in selected_recipients:
        _mailbox(recipient, "email recipient")

    result: dict[str, Any] = {
        "processed": len(selected_recipients),
        "sent": 0,
        "duplicate": 0,
        "failed": 0,
        "delivery_state_unknown": 0,
        "skipped": False,
        "failures": [],
    }
    for recipient in selected_recipients:
        try:
            event, acquired = reserve_notification(
                "email",
                event_key,
                recipient,
                lease_seconds=900,
            )
        except Exception as exc:
            _record_failure(result, recipient, exc)
            continue
        if not acquired:
            result["duplicate"] += 1
            continue

        try:
            record_notification_content(
                event.id,
                subject=content.subject,
                text_body=content.text,
                html_body=content.html,
                universe_hash=audit_context.universe_hash if audit_context else None,
                evidence_hash=audit_context.evidence_hash if audit_context else None,
                metadata=dict(audit_context.metadata or {}) if audit_context else None,
            )
        except Exception as exc:
            audit_error = RuntimeError(
                f"notification content audit persistence failed: {_safe_error(exc)}"
            )
            try:
                finish_notification(event.id, status="failed", error=str(audit_error))
            except Exception as finish_exc:
                audit_error = RuntimeError(
                    f"{audit_error}; notification finalization failed: {_safe_error(finish_exc)}"
                )
            _record_failure(result, recipient, audit_error)
            continue

        message = _build_message(
            content,
            event_key=event_key,
            recipient=recipient,
            email_from=settings.email_from,
            reply_to=settings.email_reply_to,
        )
        try:
            _deliver_message(settings, message, sender_address, recipient)
        except Exception as exc:
            try:
                finish_notification(
                    event.id,
                    status="failed",
                    error=_safe_error(exc),
                )
            except Exception as finish_exc:
                _record_unknown_delivery_state(result, recipient, exc, finish_exc)
            else:
                _record_failure(result, recipient, exc)
            continue

        try:
            finish_notification(
                event.id,
                status="sent",
                response={"message_id": message["Message-ID"]},
            )
        except Exception as exc:
            _record_unknown_delivery_state(result, recipient, None, exc)
            continue
        result["sent"] += 1
    return result


def _configuration_state(settings: Any, recipients: tuple[str, ...]) -> str:
    transport = (settings.smtp_host, settings.email_from)
    optional = (
        settings.email_reply_to,
        settings.smtp_username,
        settings.smtp_password,
    )
    # A repository default recipient alone is safe and does not mean SMTP was
    # partially configured. Scheduled jobs remain a no-op until transport
    # settings are deliberately supplied.
    if not any(transport) and not any(optional):
        return "absent"
    if not recipients or not all(transport):
        return "partial"
    return "configured"


def _validate_delivery_settings(settings: Any) -> None:
    security = str(settings.smtp_security).strip().lower()
    if security not in {"starttls", "ssl"}:
        raise ValueError("SMTP_SECURITY must be starttls or ssl")
    if settings.smtp_port <= 0:
        raise ValueError("SMTP_PORT must be positive")
    if settings.smtp_timeout_sec <= 0:
        raise ValueError("SMTP_TIMEOUT_SEC must be positive")
    if bool(settings.smtp_username) != bool(settings.smtp_password):
        raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be configured together")


def _validate_content(content: EmailContent, event_key: str) -> None:
    if not event_key.strip():
        raise ValueError("event_key is required")
    _reject_newlines(content.subject, "email subject")
    if not content.subject.strip():
        raise ValueError("email subject is required")
    if not content.text.strip() or not content.html.strip():
        raise ValueError("both text and HTML email bodies are required")


def _validate_audit_context(context: Optional[EmailAuditContext]) -> None:
    if context is None:
        return
    for name, value in (
        ("universe_hash", context.universe_hash),
        ("evidence_hash", context.evidence_hash),
    ):
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    if context.metadata is not None and not isinstance(context.metadata, Mapping):
        raise ValueError("email audit metadata must be a mapping")


def _normalize_recipients(recipients: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in recipients:
        recipient = str(raw).strip()
        if not recipient:
            continue
        canonical = recipient.casefold()
        if canonical not in seen:
            seen.add(canonical)
            normalized.append(recipient)
    return tuple(normalized)


def _mailbox(value: str, field_name: str) -> str:
    return validate_email_mailbox(value, field_name)


def _reject_newlines(value: str, field_name: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field_name} must not contain newlines")


def _build_message(
    content: EmailContent,
    *,
    event_key: str,
    recipient: str,
    email_from: str,
    reply_to: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = content.subject
    message["From"] = email_from
    message["To"] = recipient
    if reply_to:
        message["Reply-To"] = reply_to
    message["Message-ID"] = _stable_message_id(event_key, recipient, email_from)
    message.set_content(content.text)
    message.add_alternative(content.html, subtype="html")
    return message


def _stable_message_id(event_key: str, recipient: str, email_from: str) -> str:
    address = _mailbox(email_from, "EMAIL_FROM")
    domain = address.rsplit("@", 1)[1].lower()
    digest = hashlib.sha256(f"{event_key}\0{recipient.casefold()}".encode()).hexdigest()[:32]
    return f"<korea-ipo-{digest}@{domain}>"


def _deliver_message(
    settings: Any,
    message: EmailMessage,
    sender_address: str,
    recipient: str,
) -> None:
    context = ssl.create_default_context()
    security = str(settings.smtp_security).strip().lower()
    if security == "ssl":
        with smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout_sec,
            context=context,
        ) as client:
            _authenticate_and_send(client, settings, message, sender_address, recipient)
        return

    with smtplib.SMTP(
        settings.smtp_host,
        settings.smtp_port,
        timeout=settings.smtp_timeout_sec,
    ) as client:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        _authenticate_and_send(client, settings, message, sender_address, recipient)


def _authenticate_and_send(
    client: Any,
    settings: Any,
    message: EmailMessage,
    sender_address: str,
    recipient: str,
) -> None:
    if settings.smtp_username:
        client.login(settings.smtp_username, settings.smtp_password)
    refused = client.send_message(
        message,
        from_addr=sender_address,
        to_addrs=[recipient],
    )
    if refused:
        raise smtplib.SMTPRecipientsRefused(refused)


def _record_failure(result: dict[str, Any], recipient: str, exc: Exception) -> None:
    result["failed"] += 1
    result["failures"].append(
        {
            "recipient": _mask_recipient(recipient),
            "error": _safe_error(exc),
        }
    )


def _record_unknown_delivery_state(
    result: dict[str, Any],
    recipient: str,
    delivery_exc: Optional[Exception],
    finish_exc: Exception,
) -> None:
    result["failed"] += 1
    result["delivery_state_unknown"] += 1
    delivery_detail = (
        f"delivery error {_safe_error(delivery_exc)}; "
        if delivery_exc is not None
        else "SMTP accepted the message; "
    )
    result["failures"].append(
        {
            "recipient": _mask_recipient(recipient),
            "state": "delivery-state-unknown",
            "error": (
                f"{delivery_detail}notification finalization failed: {_safe_error(finish_exc)}"
            ),
        }
    )


def _mask_recipient(recipient: str) -> str:
    _name, address = parseaddr(recipient)
    if "@" not in address:
        return "***"
    local, domain = address.rsplit("@", 1)
    return f"{local[:1]}***@{domain}"


def _safe_error(exc: Exception) -> str:
    masked = mask_secrets(f"{type(exc).__name__}: {exc}")
    for pattern in SMTP_SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)(smtps?"):
            masked = pattern.sub(r"\1***REDACTED***\3", masked)
        else:
            masked = pattern.sub(r"\1=***REDACTED***", masked)
    return masked
