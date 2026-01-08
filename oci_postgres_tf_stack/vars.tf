
variable region { default = "us-ashburn-1" }
#variable "tenancy_name" { }
variable  compartment_ocid {   }


## Network 

variable create_service_gateway { default = true }
variable create_vcn_subnet { default = true }
variable psql_subnet_ocid {  default = "" } ## Private Subnet OCID of existing Subnet 
variable "vcn_cidr" { 
    type = list
    default = ["10.10.0.0/16"] 
    }

variable "create_vault" { default = true }
variable "vault_id" { 
  default = ""
}
## PSQL 
variable "psql_admin" {
    type = string
    description = "Name of PSQL Admin Username"
}

variable psql_version {  default = 16 } 
variable inst_count  { default = 1 }

variable "num_ocpu" { default = 2 }
  
variable "psql_shape_name" {
  type    = string
  default = "PostgreSQL.VM.Standard.E4.Flex"
}

variable psql_iops {
   type = map(number)
    default = {75000}
}

#variable psql_passwd_type  {  default = "PLAIN_TEXT" }

## Compute

variable create_compute { default = false }

variable "compute_shape" { default = "VM.Standard.E4.Flex" }

variable "compute_ocpus" { default = 1 }

variable "compute_memory_in_gbs" { default = 8 }

variable "compute_assign_public_ip" { default = false }

variable "compute_display_name" { default = "app-host-1" }

variable "compute_ssh_public_key" {
  type = string
  default = ""
}

variable "compute_image_ocid" {
  type = string
  default = ""
}

variable "compute_nsg_ids" {
  type = list(string)
  default = []
}

variable "compute_boot_volume_size_in_gbs" {
  default = 50
}
