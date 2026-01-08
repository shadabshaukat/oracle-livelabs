variable "region" {
  type    = string
  default = "us-ashburn-1"
}

variable "compartment_ocid" {
  type = string
}

## Network

variable "create_service_gateway" {
  type    = bool
  default = true
}

variable "create_vcn_subnet" {
  type    = bool
  default = true
}

variable "psql_subnet_ocid" {
  type        = string
  description = "Private Subnet OCID of existing subnet (used when create_vcn_subnet = false)"
  default     = ""
}

variable "vcn_cidr" {
  type    = list(string)
  default = ["10.10.0.0/16"]
}

## Vault/KMS

variable "create_vault" {
  type    = bool
  default = true
}

variable "vault_id" {
  type    = string
  default = ""
}

## PostgreSQL

variable "psql_admin" {
  type        = string
  description = "Name of PostgreSQL admin user"
}

variable "psql_version" {
  type    = number
  default = 14
}

variable "inst_count" {
  type    = number
  default = 1
}

variable "num_ocpu" {
  type    = number
  default = 2
}

variable "psql_shape_name" {
  type        = string
  description = "PostgreSQL shape family name"
  default     = "PostgreSQL.VM.Standard.E4.Flex"
}

variable "psql_iops" {
  type = map(number)
  default = {
    75  = 75000
    150 = 150000
    225 = 225000
    300 = 300000
  }
}

# variable "psql_passwd_type" { default = "PLAIN_TEXT" }

## Compute (optional)

variable "create_compute" {
  type    = bool
  default = false
}

variable "compute_shape" {
  type    = string
  default = "VM.Standard.E4.Flex"
}

variable "compute_ocpus" {
  type    = number
  default = 1
}

variable "compute_memory_in_gbs" {
  type    = number
  default = 6
}

variable "compute_assign_public_ip" {
  type    = bool
  default = false
}

variable "compute_display_name" {
  type    = string
  default = "app-host-1"
}

variable "compute_ssh_public_key" {
  type    = string
  default = ""
}

variable "compute_image_ocid" {
  type    = string
  default = ""
}

variable "compute_nsg_ids" {
  type    = list(string)
  default = []
}

variable "compute_boot_volume_size_in_gbs" {
  type    = number
  default = 50
}