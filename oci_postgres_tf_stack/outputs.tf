output  "psql_admin_pwd" { 
  value      = local.psql_admin_password
  sensitive  = true
}

output "compute_instance_id" {
  value = length(oci_core_instance.app_host) > 0 ? oci_core_instance.app_host[0].id : null
}

output "compute_state" {
  value = length(oci_core_instance.app_host) > 0 ? oci_core_instance.app_host[0].state : null
}

output "compute_public_ip" {
  value = length(data.oci_core_vnic.app_host_primary_vnic) > 0 ? data.oci_core_vnic.app_host_primary_vnic[0].public_ip_address : null
}

output "compute_private_ip" {
  value = length(data.oci_core_vnic.app_host_primary_vnic) > 0 ? data.oci_core_vnic.app_host_primary_vnic[0].private_ip_address : null
}

output "uploads_bucket_name" {
  value       = oci_objectstorage_bucket.uploads_bucket.name
  description = "Object Storage bucket name for search-app uploads"
}
