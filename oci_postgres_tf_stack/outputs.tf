output  "psql_admin_pwd" { 
  #value = oci_psql_db_system.psql_inst_1.credentials
  value = random_string.psql_admin_password.result
  sensitive   = true
 }

output "compute_instance_id" {
  value = length(oci_core_instance.app_host) > 0 ? oci_core_instance.app_host[0].id : null
}
