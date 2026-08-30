# Horse deployment contract

This directory defines the deployable artifact for the Horse environment. It is not a deployment command and does not authorize VPS, DNS, OAuth, database, or secret changes.

## Fixed decisions

- Use an immutable GHCR image tag containing the full Git SHA; record the resolved digest before deployment.
- Build only in GitHub Actions. The VPS pulls the tested image and never builds source.
- Use the existing Traefik network. The application and PostgreSQL expose no host ports.
- Keep PostgreSQL private and persistent.
- The Horse G3 staging profile stores media in the persistent directory `BRIGHTBEAN_MEDIA_DIR` on the existing VPS. A dedicated read-only Nginx container serves `/media/` behind Traefik; no R2 service is required.
- Keep the S3-compatible profile available only for a later, explicit storage decision. Local media and S3 credentials are never enabled at the same time.
- Feed secrets through mounted files outside Git. Social OAuth credentials should be stored through BrightBean's encrypted credential store after staging is healthy.
- Treat `unknown` as a protected publishing state. It is never selected by the retry worker.

## Secret files

Create the directory named by `BRIGHTBEAN_SECRETS_DIR` with mode `0700`. For local media, create two newline-terminated files readable by the non-root application UID (`root:10001`, mode `0640`):

- `django_secret_key`;
- `postgres_password`.

The S3 profile additionally requires these files:

- `s3_access_key_id`;
- `s3_secret_access_key`.

Never place values in `.env.horse`, Compose labels, commands, images, logs, or this repository.

## Preflight for a future staging deployment

1. Copy `.env.horse.example` to `.env.horse` outside Git and replace placeholders.
2. Verify that `BRIGHTBEAN_IMAGE` is a full SHA tag and resolve it to a registry digest.
3. Verify the external Traefik network name without changing it.
4. Create `BRIGHTBEAN_MEDIA_DIR` with owner `10001:10001`, mode `0755`, and confirm the Nginx UID 101 can read created files. Reserve and monitor its disk budget.
5. Create a PostgreSQL backup destination outside the container volume.
6. Render the local-media model with `docker compose --env-file .env.horse -f compose.yml -f compose.local-media.yml config` and inspect it for host ports or leaked values. This override requires a Compose release that supports `!override`.
7. Run `migrate` only after the backup and schema compatibility review.

## Backup and restore design

- Daily PostgreSQL custom-format dump during the client-zero pilot.
- Mandatory dump immediately before every migration-bearing release.
- Encrypt and copy dumps outside `postgres_data`; keep no OAuth values in logs or filenames.
- RPO: 24 hours. Initial RTO: 2 hours.
- Restore rehearsal happens in staging before any social OAuth connection.
- Archive `BRIGHTBEAN_MEDIA_DIR` together with every database backup; database backup alone is not a media backup.
- Copy both artifacts to an independent backup destination. Local media on the same VPS is persistent across container restarts, but not resilient to total VPS loss by itself.
- Rollback returns to the prior image digest. Restore the database only when the migration compatibility note says rollback is unsafe without it.

## Runtime evidence required later

- app health endpoint is healthy;
- worker heartbeat is younger than 300 seconds and its latest cycle succeeded;
- no app or database host port exists;
- app, worker, media, and database remain under their declared memory/CPU ceilings;
- a file written through Django storage is delivered through `/media/` with the same checksum and survives a service restart;
- backup and restore evidence is recorded before OAuth.
