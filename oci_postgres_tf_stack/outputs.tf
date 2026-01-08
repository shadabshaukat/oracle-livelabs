output  "psql_admin_pwd" { 
  value      = local.psql_admin_password
  sensitive  = true
}

output "compute_instance_id" {
  value = length(oci_core_instance.app_host) > 0 ? oci_core_instance.app_host[0].id : null
}
