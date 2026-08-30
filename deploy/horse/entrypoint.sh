#!/bin/sh
set -eu

read_secret() {
    secret_path="$1"
    if [ ! -r "$secret_path" ]; then
        echo "Required secret file is missing: $secret_path" >&2
        exit 1
    fi
    tr -d '\r\n' < "$secret_path"
}

export SECRET_KEY="$(read_secret /run/secrets/django_secret_key)"
postgres_password="$(read_secret /run/secrets/postgres_password)"
encoded_password="$(python -c 'import sys; from urllib.parse import quote; print(quote(sys.argv[1], safe=""))' "$postgres_password")"
postgres_host="${POSTGRES_HOST:-brightbean-db}"
export DATABASE_URL="postgres://${POSTGRES_USER}:${encoded_password}@${postgres_host}:5432/${POSTGRES_DB}"

if [ "${STORAGE_BACKEND:-s3}" = "s3" ]; then
    export S3_ACCESS_KEY_ID="$(read_secret /run/secrets/s3_access_key_id)"
    export S3_SECRET_ACCESS_KEY="$(read_secret /run/secrets/s3_secret_access_key)"
fi

unset postgres_password encoded_password postgres_host
exec "$@"
