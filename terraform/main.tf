terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket = "amcgavin-tfstates"
    key    = "camera-sync/terraform.tfstate"
    region = "ap-southeast-2"
  }
}

provider "aws" {
  region = "ap-southeast-2"
}

resource "aws_s3_bucket" "recordings" {
  bucket_prefix = "cctv"
}

resource "aws_s3_bucket_lifecycle_configuration" "recordings" {
  bucket = aws_s3_bucket.recordings.id

  rule {
    id     = "expire-recordings"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 14
    }
  }
}

resource "aws_iam_user" "camera_save" {
  name = "camera-save"
}

resource "aws_iam_user_policy" "camera_save" {
  user = aws_iam_user.camera_save.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.recordings.arn}/*"
      }
    ]
  })
}

resource "aws_iam_access_key" "camera_save" {
  user = aws_iam_user.camera_save.name
}
