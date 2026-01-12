# OCI PostgreSQL Terraform Stack (Resource Manager ready)

This module deploys:
- VCN with:
  - Private subnet for PostgreSQL
  - Public subnet for Compute (with Internet Gateway and route table)
  - Optional Service Gateway (for OSN access, e.g., backups)
  - NAT Gateway for private egress
- Network Security:
  - Default Security List permits SSH (tcp/22)
  - NSG for PostgreSQL permitting tcp/5432 only from within the VCN CIDR
- OCI PostgreSQL DB System:
  - Shape family via psql_shape_name and OCPUs via num_ocpu
  - Admin user from psql_admin
  - Admin password provided via variable or auto-generated (no Vault)
  - Optional (default-enabled): attach an OCI PostgreSQL Configuration with overrides
- Optional Compute Instance:
  - In the public subnet by default
  - Public IP attachment controlled by compute_assign_public_ip

Region-agnostic:
- Availability Domain is discovered dynamically (no hard-coded AD-1/2/3)

## Inputs

Core
- compartment_ocid (string): Target compartment OCID
- region (string): Target region (default: us-ashburn-1)
- tenancy_ocid (string, optional): If provided, used for AD discovery; else compartment_ocid is used

Networking
- create_vcn_subnet (bool, default true): Create VCN, subnets, gateways, route tables
- create_service_gateway (bool, default true): Create Service Gateway
- vcn_cidr (list(string), default ["10.10.0.0/16"]): VCN CIDR(s)
- psql_subnet_ocid (string, default ""): Existing private subnet OCID when create_vcn_subnet=false (used for both PG and compute if you choose)
- public_subnet_ocid (string, default ""): Existing public subnet OCID for Compute when create_vcn_subnet=false

Credentials
- psql_admin (string): PostgreSQL admin username (required)
- psql_admin_password (string, sensitive, default ""): Optional plaintext password. If empty, a strong random password is generated and exposed as a sensitive output psql_admin_pwd.

PostgreSQL
- psql_version (number, default 16)
- num_ocpu (number, default 2): OCPU count for PostgreSQL
- inst_count (number, default 1)
- psql_shape_name (string, default "PostgreSQL.VM.Standard.E5.Flex")
- psql_iops (map(number), default as provided): IOPS profile mapping

Compute (optional)
- create_compute (bool, default false): Whether to create compute instance
- compute_shape (string, default "VM.Standard.E5.Flex")
- compute_ocpus (number, default 1)
- compute_memory_in_gbs (number, default 8)
- compute_assign_public_ip (bool, default false): Public IP for VNIC; ensure your subnet permits it
- compute_display_name (string, default "app-host-1")
- compute_ssh_public_key (string): SSH public key for opc user
- compute_image_ocid (string, default ""): If empty, latest Oracle Linux image is selected
- compute_nsg_ids (list(string), default []): Optional NSG OCIDs to attach
- compute_boot_volume_size_in_gbs (number, default 50)

### PostgreSQL Configuration (default-enabled)

This stack can create and attach an OCI PostgreSQL configuration to the DB System. By default, a configuration is created with common extensions and parameters unless you provide an existing configuration OCID.

- create_psql_configuration (bool, default true): Create a new configuration
- psql_configuration_ocid (string, default ""): If non-empty, use this existing configuration and skip creation
- psql_config_display_name (string, default "livelab_flexible_configuration")
- psql_config_is_flexible (bool, default true)
- psql_config_compatible_shapes (list(string), defaults include Flex shapes)
- psql_config_description (string, default "test configuration created by terraform")
- psql_config_overrides (map(string), default): key/value overrides rendered as items under db_configuration_overrides. Defaults include:
  - oci.admin_enabled_extensions = "pg_stat_statements,pglogical"
  - pglogical.conflict_log_level = "debug1"
  - pg_stat_statements.max = "5000"

The DB System config_id is set to the created configuration’s OCID by default, or to psql_configuration_ocid if you provide one.

## Behavior Notes

- PostgreSQL password handling:
  - If psql_admin_password == "", a strong random password is generated via the random provider.
  - The final password is returned as a sensitive output: psql_admin_pwd.
  - No OCI Vault or KMS is used by this module.

- Networking and Security:
  - Private subnet: no public IPs; PSQL port 5432 allowed only from within VCN via NSG.
  - Public subnet: has IGW and route table for internet access; default security list allows SSH (22).

- Availability Domain:
  - Selected dynamically using the first available AD in the region.

## Outputs

- psql_admin_pwd (sensitive): Final PostgreSQL admin password (provided or generated)
- psql_configuration_id: OCID of the configuration (created or provided), if applicable
- compute_instance_id: OCID of the compute instance (if created)
- compute_state: Lifecycle state of the compute instance (if created)
- compute_public_ip: Public IP of the compute instance (if created and assigned)

## Usage in Oracle Resource Manager

- Create a Stack from this directory (oci_postgres_tf_stack) or ZIP it and upload.
- Provide required variables (compartment_ocid, psql_admin). Optional: psql_admin_password; if omitted, a random password is generated.
- PostgreSQL Configuration (optional/default-enabled):
  - To create a configuration: leave psql_configuration_ocid blank (default), keep create_psql_configuration=true (default), and review psql_config_overrides
  - To use an existing configuration: set psql_configuration_ocid to your config OCID
- For Compute, set create_compute=true and provide compute_ssh_public_key.
- Plan and Apply. Retrieve the psql_admin_pwd and psql_configuration_id from the Job outputs.

## Known Considerations

- If you use an existing subnet (create_vcn_subnet=false), ensure its routing and public IP policies match your compute_assign_public_ip choice.
- For private-only access to Compute, set compute_assign_public_ip=false and use Bastion/VPN/DRG as appropriate.
