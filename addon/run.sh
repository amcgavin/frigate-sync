#!/usr/bin/with-contenv bashio

export AWS_ACCESS_KEY_ID="$(bashio::config 'aws_access_key_id')"
export AWS_SECRET_ACCESS_KEY="$(bashio::config 'aws_secret_access_key')"
export AWS_REGION="$(bashio::config 'aws_region')"
export S3_BUCKET="$(bashio::config 's3_bucket')"
export UPLOAD_DELAY_MINUTES="$(bashio::config 'upload_delay_minutes')"
export CAMERAS_JSON="$(bashio::config 'cameras')"

bashio::log.info "Starting camera-save sync..."
exec python3 /app/sync.py
