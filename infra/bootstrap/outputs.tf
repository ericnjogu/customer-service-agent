output "state_bucket" {
  value = aws_s3_bucket.state.id
}

output "github_plan_role_arn" {
  value = aws_iam_role.github_plan.arn
}

output "github_ecr_role_arn" {
  value = aws_iam_role.github_ecr.arn
}
