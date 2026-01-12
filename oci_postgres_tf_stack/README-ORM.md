# Oracle Resource Manager Terraform Stack: OCI PostgreSQL + Optional Compute

This repository contains a Terraform stack to deploy:
- Oracle Cloud Infrastructure (OCI) Virtual Cloud Network (VCN) with a private subnet and related networking (NAT Gateway, optional Service Gateway, Route Table, Security Lists, NSG)
- OCI PostgreSQL Database System with admin password provided or auto-generated (no Vault)
- Optional: A small OCI Compute instance in a public subnet (added in this iteration)

The stack is designed to work both in Terraform CLI and Oracle Resource Manager (ORM). This README focuses on deploying via ORM.

Repository layout:
- oci_postgres_tf_stack/: Terraform configuration for the stack
- README.md: This guide for ORM deployment

## Prerequisites

- OCI tenancy with permissions to create networking, OCI PostgreSQL, and Compute Instance
- A target Compartment OCID where resources will be created
- Oracle Resource Manager policies in place (tenant or compartment scope). Example policy (recommended at compartment scope):
  - allow service resource_manager to manage all-resources in compartment <your_compartment_name>
- SSH public key (optional unless creating the compute instance) to access the compute instance (if you enable it and set assign_public_ip=false you will need private access such as Bastion/VCN peering/DRG)

## What gets created

- VCN with CIDR 10.10.0.0/16 (configurable) and a private subnet (no public IPs)
- Optional Service Gateway to access Oracle Services Network
- NAT Gateway for outbound internet from private subnet
- Public subnet for Compute with Internet Gateway and route table
- Default Security List with SSH ingress to 22 (security still restricted by private subnet no-public-IP)
- NSG for PostgreSQL (ingress tcp/5432)
- PostgreSQL admin password handled via input or generated (sensitive output; no Vault)
- OCI PostgreSQL DB System with:
  - Flexible CPU/memory based on variables
  - Admin username provided by variable
  - Admin password provided via psql_admin_password or auto-generated (random_password), returned as a sensitive output
- Optional Compute instance in the public subnet

## Subnet defaults and ports

- When vcn_cidr is ["10.10.0.0/16"], the module creates:
  - Private subnet: 10.10.1.0/24 (cidrsubnet(..., 8, 1))
  - Public subnet: 10.10.2.0/24 (cidrsubnet(..., 8, 2))
- Public subnet ingress allowed: tcp/22, tcp/443, tcp/8443, tcp/8000, tcp/9000 from 0.0.0.0/0
- Private subnet ingress allowed: tcp/22 and tcp/5432 from within the VCN CIDR

## Variables (ORM stack inputs)

The following inputs are consumed by the stack. When creating a Stack in ORM, you will be prompted for these. Defaults are provided where possible.

Required/common:
- compartment_ocid: Compartment OCID for all resources
- region: Region to deploy into (default: us-ashburn-1)
- tenancy_ocid: Tenancy OCID used for AD discovery (optional; if omitted, compartment_ocid is used)

Networking:
- create_vcn_subnet: Whether to create a new VCN and private subnet (default: true)
- create_service_gateway: Whether to create Service Gateway (default: true)
- vcn_cidr: List of VCN CIDR blocks (default: ["10.10.0.0/16"])
- psql_subnet_ocid: If not creating VCN/subnet, the OCID of an existing private subnet to use (default: "")
- public_subnet_ocid: If not creating VCN/subnet, the OCID of an existing public subnet to use for Compute (default: ""; if omitted, psql_subnet_ocid is used)

Credentials:
- psql_admin_password: Optional plain-text admin password; leave blank to auto-generate a strong random password. The final value is returned as sensitive output psql_admin_pwd.

PostgreSQL:
- psql_admin: Admin username for the PostgreSQL service (no default; required)
- psql_version: PostgreSQL version (default: 16)
- inst_count: Number of DB instances (default: 1)
- num_ocpu: OCPU count for PostgreSQL (default: 2)
- psql_shape_name: PostgreSQL shape family name (default: "PostgreSQL.VM.Standard.E5.Flex"). Use with num_ocpu to determine capacity; memory is derived as num_ocpu × 16 GB.
- psql_iops: Internal map of IO settings (do not modify unless you know what you are doing)

Compute (optional small instance added with this iteration):
- create_compute: Whether to create a compute instance (default: false)
- compute_shape: Compute shape (default: "VM.Standard.E5.Flex")
- compute_ocpus: OCPU count (default: 1)
- compute_memory_in_gbs: Memory in GB (default: 8)
- compute_assign_public_ip: Assign public IP to VNIC (default: false). When this stack creates the network, the compute instance is placed in the public subnet; set this to true to assign a public IP. If you use an existing subnet (create_vcn_subnet=false), this flag must be compatible with that subnet’s settings.
- compute_display_name: Display name (default: "app-host-1")
- compute_ssh_public_key: SSH public key for opc user (default: ""). Required for instance SSH access.
- compute_image_ocid: Optional image OCID to use; if blank the latest Oracle Linux image compatible with the shape will be selected automatically.
- compute_nsg_ids: Optional list of NSG OCIDs to attach to the VNIC (default: [])

### PostgreSQL Configuration (optional)

This stack can optionally create and attach an OCI PostgreSQL configuration. The configuration is created only if:
- create_psql_configuration = true
- psql_configuration_ocid = "" (blank)
- psql_config_overrides contains at least one entry

Inputs:
- create_psql_configuration (bool, default false): Create a new configuration
- psql_configuration_ocid (string, default ""): Use an existing configuration OCID (skips creation)
- psql_config_display_name (string, default "psql_flex_config")
- psql_config_is_flexible (bool, default true)
- psql_config_compatible_shapes (list(string), defaults include Flex shapes)
- psql_config_description (string)
- psql_config_overrides (map(string)) key/value overrides rendered as items under db_configuration_overrides

Example overrides:
```
psql_config_overrides = {
  "oci.admin_enabled_extensions" = "pg_stat_statements,pglogical"
  "pglogical.conflict_log_level" = "debug1"
  "pg_stat_statements.max"       = "5000"
}
```

Notes:
- If you provide psql_configuration_ocid, the DB System will use that config_id and the resource will not be created.
- If overrides are empty, no configuration resource will be created (to satisfy provider requirements that at least one items block is present when the block exists).

## Outputs

- psql_admin_pwd: Sensitive output of admin password (provided or generated)
- compute_instance_id: OCID of the compute instance (if created)
- compute_state: Lifecycle state of the compute instance (if created)
- compute_public_ip: Public IP of the compute instance (if created and assigned)
- psql_configuration_id: OCID of the configuration (created or provided), if applicable

## Deploying via Oracle Resource Manager

1. Prepare the stack archive:
   - Option A: From this repository root, zip the folder oci_postgres_tf_stack/ only:
     - macOS/Linux: `zip -r oci_postgres_tf_stack.zip oci_postgres_tf_stack`
   - Option B: Use the Git repo directly in ORM (Create Stack from Git/Version Control). If you do this, set the working directory to `oci_postgres_tf_stack`.

2. In OCI Console:
   - Open: Developer Services → Resource Manager → Stacks → Create Stack
   - Source:
     - If uploading ZIP: select your `oci_postgres_tf_stack.zip`
     - If using Git/VCS: provide repository URL and branch, and set Working Directory to `oci_postgres_tf_stack`
   - Terraform version: Any supported by the `oracle/oci` provider and your environment (the stack uses provider constraints minimally)
   - Configure variables as described above:
     - Required: compartment_ocid, psql_admin
     - Optional: region (defaults to us-ashburn-1), create_vcn_subnet, create_service_gateway, etc.
     - Compute: set create_compute, compute_ssh_public_key (recommended), and others as needed
     - PostgreSQL Configuration (optional):
       - To create a configuration: set create_psql_configuration=true, leave psql_configuration_ocid blank, and provide psql_config_overrides (map)
       - To use an existing configuration: set psql_configuration_ocid to your config OCID

3. Create the Stack.

4. Plan and Apply:
   - From the Stack view: Actions → Terraform Plan (optional) to review changes
   - Actions → Terraform Apply to provision resources

5. Review Outputs:
   - After Apply completes, navigate to the Job details to see Outputs:
     - psql_admin_pwd (sensitive)
     - compute_instance_id (if compute created)
     - psql_configuration_id (if a configuration was created or provided)

## Updating the Stack to add the small Compute instance

If you created the stack prior to this iteration and want to add the compute instance:
- Update the stack source with the latest content (upload a new zip with the updated `oci_postgres_tf_stack/` or update the Git ref)
- In Stack variables:
  - Set create_compute=true
  - Provide your compute_ssh_public_key (contents of ~/.ssh/id_rsa.pub or similar)
  - Adjust compute_shape / compute_ocpus / compute_memory_in_gbs if needed (defaults: VM.Standard.E4.Flex, 1 OCPU, 8 GB)
- Run Plan and then Apply in Resource Manager

The compute instance is created in the public subnet by default. For private-only access, set compute_assign_public_ip=false and/or attach to your own private subnet (set create_vcn_subnet=false and supply psql_subnet_ocid).

## Destroying the Stack

- In ORM, from the Stack view: Actions → Terraform Destroy
- This will remove all resources created by the stack (PostgreSQL, networking, and compute if created)

## Troubleshooting

- If plan/apply fails due to permissions, ensure the compartment policy allows Resource Manager to manage the required resources.
- Ensure the compartment policy allows Resource Manager to manage resources in the compartment.
- If you intend to create a PostgreSQL configuration but none appears in the plan: confirm that `create_psql_configuration=true`, `psql_configuration_ocid` is blank, and `psql_config_overrides` has at least one entry.
- If not creating VCN/subnet, verify your provided subnet OCID is in the compartment and region specified and is private.

## Notes on Security

- PostgreSQL admin password is provided or auto-generated and returned only as a sensitive output (no Vault).
- Default Security List allows SSH from 0.0.0.0/0, but the private subnet prohibits public IPs, which prevents exposure by default. Lock down further as per your security requirements and consider NSGs tailored for the compute instance.

## CLI usage (optional)

You can also run the stack with Terraform CLI:
- cd oci_postgres_tf_stack
- terraform init
- terraform plan -var='compartment_ocid=<ocid>' -var='psql_admin=<name>' -var-file=example.tfvars
- terraform apply -var='compartment_ocid=<ocid>' -var='psql_admin=<name>' -var-file=example.tfvars
