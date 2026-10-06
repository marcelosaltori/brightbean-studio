"""Metadata-only observability for Instagram Login DM webhooks."""

import hashlib
import hmac
import json
import logging

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.social_accounts.models import SocialAccount


@pytest.mark.django_db
@override_settings(PLATFORM_CREDENTIALS_FROM_ENV={"instagram_login": {"app_secret": "test-secret"}})
def test_accepted_dm_webhook_logs_counts_without_payload_content(client, organization, caplog):
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(name="Webhook diagnostics", organization=organization)
    SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram_login",
        account_platform_id="ig-diagnostics-123",
        account_name="Test Instagram",
    )
    payload = {
        "entry": [
            {
                "id": "ig-diagnostics-123",
                "messaging": [
                    {
                        "sender": {"id": "private-sender-id"},
                        "message": {"mid": "private-message-id", "text": "private message body"},
                    }
                ],
            }
        ]
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    caplog.set_level(logging.INFO, logger="apps.inbox.webhooks")

    response = client.post(
        reverse("inbox_webhooks:webhook_instagram_login"),
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature,
    )

    assert response.status_code == 200
    logs = [record.getMessage() for record in caplog.records if record.name == "apps.inbox.webhooks"]
    assert logs == [
        "Instagram Login webhook accepted: entries=1 matched_accounts=1 "
        "messaging_events=1 dm_events_with_id=1 dm_records_created=1"
    ]
    assert "private message body" not in logs[0]
    assert "private-sender-id" not in logs[0]
    assert "private-message-id" not in logs[0]
