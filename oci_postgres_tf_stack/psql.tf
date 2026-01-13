

## Creating OCI Postgres DB 


resource oci_psql_db_system psql_inst_1 {
  compartment_id = var.compartment_ocid
  config_id      = var.psql_configuration_ocid != "" ? var.psql_configuration_ocid : (length(oci_psql_configuration.psql_flex_config) > 0 ? oci_psql_configuration.psql_flex_config[0].id : null)
  db_version = var.psql_version
  #admin_username = var.psql_admin
  #credentials =  random_string.psql_admin_password.result 
  credentials {
    password_details {
      password_type = "PLAIN_TEXT"
      password      = local.psql_admin_password
    }
    username = var.psql_admin
  }

  description = "Postgres SQL Instance"
  display_name = "psql_inst_1"
  freeform_tags = {
  }
  instance_count              = var.inst_count
  instance_memory_size_in_gbs =  var.num_ocpu  * 16 
  instance_ocpu_count         = var.num_ocpu
  #instances_details = <<Optional value>>
  management_policy {
    #backup_policy = <<Optional value >>
    maintenance_window_start = "FRI 04:00"
  }
  network_details {
    nsg_ids = var.create_vcn_subnet == true ? [oci_core_network_security_group.vcn1_nsg[0].id] : []
    #primary_db_endpoint_private_ip = 
    subnet_id      = var.create_vcn_subnet == true ?  oci_core_subnet.vcn1_psql_priv_subnet[0].id : var.psql_subnet_ocid
  }
   shape = var.psql_shape_name
  
  storage_details {
    availability_domain   = data.oci_identity_availability_domains.ads.availability_domains[0].name
    iops                  = var.psql_iops[75]
    is_regionally_durable = "false"
    system_type           = "OCI_OPTIMIZED_STORAGE"
  }
  system_type = "OCI_OPTIMIZED_STORAGE"
}
