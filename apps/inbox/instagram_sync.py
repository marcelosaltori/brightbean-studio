"""Persist Instagram batch progress using the existing workspace KV store."""

import copy
import uuid

from django.db import transaction
from django.utils import timezone

from apps.settings_manager.models import WorkspaceSetting


def checkpoint_key(account_id):
    return f"inbox.instagram_dm_checkpoint.{account_id}"


def sync_instagram_batch(engine, account, provider, seen_types, dm_since, comment_since):
    from .tasks import _is_recent, _related_post_key, resolve_related_posts

    now = timezone.now()
    token = str(uuid.uuid4())
    key = checkpoint_key(account.id)
    with transaction.atomic():
        checkpoint, _ = WorkspaceSetting.objects.get_or_create(
            workspace_id=account.workspace_id,
            key=key,
            defaults={"value": {}},
        )
        checkpoint = WorkspaceSetting.objects.select_for_update().get(pk=checkpoint.pk)
        state = copy.deepcopy(checkpoint.value or {})
        if state.get("lease_until", 0) > now.timestamp():
            return
        state.update(lease_token=token, lease_until=now.timestamp() + 900)
        checkpoint.value = state
        checkpoint.save(update_fields=["value", "updated_at"])

    try:
        batch = provider.get_inbox_batch(
            access_token=account.oauth_access_token,
            dm_state=state,
            dm_since=dm_since,
            comment_since=comment_since,
        )
        related_posts = resolve_related_posts(account, batch.messages)
        with transaction.atomic():
            checkpoint = WorkspaceSetting.objects.select_for_update().get(
                workspace_id=account.workspace_id,
                key=key,
            )
            if (checkpoint.value or {}).get("lease_token") != token:
                return
            for msg in batch.messages:
                # Resumed historical batches must stay silent too, even though
                # earlier batches have already persisted messages of this type.
                historical_dm = msg.message_type == "dm" and msg.extra.get("initial_backfill")
                notify_new = (msg.message_type in seen_types and not historical_dm) or _is_recent(msg.timestamp)
                engine._upsert_message(
                    account,
                    msg,
                    notify=notify_new,
                    related_post_id=related_posts.get(_related_post_key(msg.extra)),
                )
            batch.state.pop("lease_token", None)
            batch.state.pop("lease_until", None)
            batch.state["committed_at"] = timezone.now().isoformat()
            checkpoint.value = batch.state
            checkpoint.save(update_fields=["value", "updated_at"])
    finally:
        # On a failed DB/message write the transaction rolled back. Release our
        # lease only; preserve the prior queue so the entire batch can retry.
        with transaction.atomic():
            checkpoint = WorkspaceSetting.objects.select_for_update().get(
                workspace_id=account.workspace_id,
                key=key,
            )
            remaining = checkpoint.value or {}
            if remaining.get("lease_token") == token:
                remaining.pop("lease_token", None)
                remaining.pop("lease_until", None)
                checkpoint.value = remaining
                checkpoint.save(update_fields=["value", "updated_at"])
