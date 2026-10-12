"""Regression for the actual HTMX filter payload, including empty All options."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import InboxMessage
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.mark.django_db
@pytest.mark.parametrize("selected_type", ["dm", ""])
def test_feed_all_options_do_not_hide_messages(client, org_owner, organization, selected_type):
    workspace = Workspace.objects.create(name="Inbox filters", organization=organization)
    WorkspaceMembership.objects.create(user=org_owner, workspace=workspace, workspace_role="owner")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="instagram_login", account_platform_id="ig-test", account_name="Test account"
    )
    for kind in ["dm", "comment"]:
        InboxMessage.objects.create(
            workspace=workspace,
            social_account=account,
            platform_message_id=f"test-{kind}",
            message_type=kind,
            body=f"Visible {kind}",
            received_at=timezone.now(),
        )
    client.force_login(org_owner)

    response = client.get(
        reverse("inbox:feed", kwargs={"workspace_id": workspace.id}),
        {"q": "", "platform": "", "account": "", "type": selected_type, "status": "", "sentiment": ""},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert b"Visible dm" in response.content
    assert (b"Visible comment" in response.content) == (not selected_type)
