output "bucket_name" {
  value = aws_s3_bucket.recordings.bucket
}

output "bucket_arn" {
  value = aws_s3_bucket.recordings.arn
}

output "aws_access_key_id" {
  value = aws_iam_access_key.camera_save.id
}

output "aws_secret_access_key" {
  value     = aws_iam_access_key.camera_save.secret
  sensitive = true
}
