output "cluster_name" { value = aws_eks_cluster.this.name }
output "cluster_endpoint" { value = aws_eks_cluster.this.endpoint }
output "cluster_ca_certificate" { value = aws_eks_cluster.this.certificate_authority[0].data }
output "cluster_oidc_issuer" { value = aws_eks_cluster.this.identity[0].oidc[0].issuer }
output "cluster_oidc_provider_arn" { value = aws_iam_openid_connect_provider.eks.arn }
output "vpc_id" { value = aws_vpc.this.id }
output "workload_subnet_ids" { value = values(aws_subnet.workload)[*].id }
output "data_subnet_ids" { value = values(aws_subnet.data)[*].id }
output "cluster_security_group_id" { value = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id }
output "ecr_repository_urls" { value = { for name, repo in aws_ecr_repository.application : name => repo.repository_url } }
output "kubernetes_operator_role_arn" { value = aws_iam_role.kubernetes_operator.arn }
