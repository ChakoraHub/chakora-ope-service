output "cluster_name" {
  description = "Name of the provisioned EKS cluster"
  value       = aws_eks_cluster.chakorahub_cluster.name
}

output "cluster_endpoint" {
  description = "Endpoint for EKS Kubernetes API server"
  value       = aws_eks_cluster.chakorahub_cluster.endpoint
}

output "kubeconfig_command" {
  description = "AWS CLI command to configure kubectl credentials"
  value       = "aws eks update-kubeconfig --region ${var.aws_region} --name ${aws_eks_cluster.chakorahub_cluster.name}"
}
