output "psql_configuration_id" {
  value = var.psql_configuration_ocid != "" ? var.psql_configuration_ocid : (length(oci_psql_configuration.psql_flex_config) > 0 ? oci_psql_configuration.psql_flex_config[0].id : null)
}
