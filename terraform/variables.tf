variable "aws_region" {
  description = "AWS deployment region"
  type        = string
  default     = "eu-north-1"
}

variable "cluster_name" {
  description = "Name of the ChakoraHub Kubernetes cluster"
  type        = string
  default     = "chakorahub-cluster"
}

variable "kubernetes_version" {
  description = "Desired Kubernetes control plane version"
  type        = string
  default     = "1.30"
}

variable "node_instance_type" {
  description = "EC2 instance type for EKS worker nodes"
  type        = string
  default     = "t3.medium"
}

variable "subnet_ids" {
  description = "List of VPC subnet IDs where the EKS cluster and node group reside"
  type        = list(string)
  default     = []
}
