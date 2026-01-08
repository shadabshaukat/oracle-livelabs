# Oracle Resource Manager Terraform Stack: OCI PostgreSQL + Optional Compute

This repository contains a Terraform stack to deploy:
- Oracle Cloud Infrastructure (OCI) Virtual Cloud Network (VCN) with a private subnet and related networking (NAT Gateway, optional Service Gateway, Route Table, Security Lists, NSG)
- OCI PostgreSQL Database System with credentials stored in OCI Vault/Key/Secret
- Optional: A small OCI Compute instance (private) in the same subnet (added in this iteration)

The stack is designed to work both in Terraform CLI and Oracle Resource Manager (ORM). This README focuses on deploying via ORM.

Repository layout:
- oci_postgres_tf_stack/: Terraform configuration for the stack
- README.md: This guide for ORM deployment

## Prerequisites

- OCI tenancy with permissions to create networking, KMS Vault/Key/Secret, OCI PostgreSQL, and Compute Instance
- A target Compartment OCID where resources will be created
- Oracle Resource Manager policies in place (tenant or compartment scope). Example policy (recommended at compartment scope):
  - allow service resource_manager to manage all-resources in compartment <your_compartment_name>
- SSH public key (optional unless creating the compute instance) to access the compute instance (if you enable it and set assign_public_ip=false you will need private access such as Bastion/VCN peering/DRG)

## What gets created

- VCN with CIDR 10.10.0.0/16 (configurable) and a private subnet (no public IPs)
- Optional Service Gateway to access Oracle Services Network
- NAT Gateway for outbound internet from private subnet
- Default Security List with SSH ingress to 22 (security still restricted by private subnet no-public-IP)
- NSG for PostgreSQL (ingress tcp/5432)
- OCI KMS Vault (optional), Key and Secret for PostgreSQL admin credentials
- OCI PostgreSQL DB System with:
  - Flexible CPU/memory based on variables
  - Admin username provided by variable
  - Admin password stored as a Vault secret (generated via random_string)
- Optional Compute instance in the same private subnet (no public IP by default)

## Variables (ORM stack inputs)

The following inputs are consumed by the stack. When creating a Stack in ORM, you will be prompted for these. Defaults are provided where possible.

Required/common:
- compartment_ocid: Compartment OCID for all resources
- region: Region to deploy into (default: us-ashburn-1)

Networking:
- create_vcn_subnet: Whether to create a new VCN and private subnet (default: true)
- create_service_gateway: Whether to create Service Gateway (default: true)
- vcn_cidr: List of VCN CIDR blocks (default: ["10.10.0.0/16"])
- psql_subnet_ocid: If not creating VCN/subnet, the OCID of an existing private subnet to use (default: "")

Vault/KMS:
- create_vault: Whether to create a new Vault (default: true)
- vault_id: Existing vault OCID if not creating a new one (default: "")

PostgreSQL:
- psql_admin: Admin username for the PostgreSQL service (no default; required)
- psql_version: PostgreSQL version (default: 14)
- inst_count: Number of DB instances (default: 1)
- num_ocpu: OCPU count for PostgreSQL (default: 2)
- psql_shape: Internal map used to select the PG shape from OCPU count (do not modify unless you know what you are doing)
- psql_iops: Internal map of IO settings (do not modify unless you know what you are doing)

Compute (optional small instance added with this iteration):
- create_compute: Whether to create a compute instance (default: false)
- compute_shape: Compute shape (default: "VM.Standard.E4.Flex")
- compute_ocpus: OCPU count (default: 1)
- compute_memory_in_gbs: Memory in GB (default: 6)
- compute_assign_public_ip: Assign public IP to VNIC (default: false). The subnet is private and disallows public IPs by default; set this true only if you attach to a public subnet you provide.
- compute_display_name: Display name (default: "app-host-1")
- compute_ssh_public_key: SSH public key for opc user (default: ""). Required for instance SSH access.
- compute_image_ocid: Optional image OCID to use; if blank the latest Oracle Linux image compatible with the shape will be selected automatically.
- compute_nsg_ids: Optional list of NSG OCIDs to attach to the VNIC (default: [])

Notes:
- If create_vcn_subnet=false, provide psql_subnet_ocid of an existing private subnet to place both the PostgreSQL service and the compute instance.
- Access to the compute instance in a private subnet typically requires Bastion service, VPN/DRG, or peering. The default security list allows inbound SSH (22), but the subnet prohibits public IPs, so there is no public internet ingress to the instance unless you change the design.

## Outputs

- psql_admin_pwd: Sensitive output of generated admin password (also stored in Vault secret)
- compute_instance_id: OCID of the compute instance (if created)

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
     - Optional: region (defaults to us-ashburn-1), create_vcn_subnet, create_service_gateway, create_vault, etc.
     - Compute: set create_compute, compute_ssh_public_key (recommended), and others as needed

3. Create the Stack.

4. Plan and Apply:
   - From the Stack view: Actions → Terraform Plan (optional) to review changes
   - Actions → Terraform Apply to provision resources

5. Review Outputs:
   - After Apply completes, navigate to the Job details to see Outputs:
     - psql_admin_pwd (sensitive, also stored in Vault)
     - compute_instance_id (if compute created)

## Updating the Stack to add the small Compute instance

If you created the stack prior to this iteration and want to add the compute instance:
- Update the stack source with the latest content (upload a new zip with the updated `oci_postgres_tf_stack/` or update the Git ref)
- In Stack variables:
  - Set create_compute=true
  - Provide your compute_ssh_public_key (contents of ~/.ssh/id_rsa.pub or similar)
  - Adjust compute_shape / compute_ocpus / compute_memory_in_gbs if needed (defaults: VM.Standard.E4.Flex, 1 OCPU, 6 GB)
- Run Plan and then Apply in Resource Manager

The compute instance is created in the same private subnet as PostgreSQL by default. For public access you would need a public subnet, a different design, or Bastion.

## Destroying the Stack

- In ORM, from the Stack view: Actions → Terraform Destroy
- This will remove all resources created by the stack (PostgreSQL, networking, vault/keys, and compute if created)

## Troubleshooting

- KMS Vault/Key creation can occasionally fail DNS resolution on first try (known OCI transient). Re-run Apply; it often succeeds on retry.
- Ensure the compartment policy allows Resource Manager to manage resources in the compartment.
- If not creating VCN/subnet, verify your provided subnet OCID is in the compartment and region specified and is private.

## Notes on Security

- PostgreSQL admin credentials are generated and stored as an OCI Vault Secret.
- Default Security List allows SSH from 0.0.0.0/0, but the private subnet prohibits public IPs, which prevents exposure by default. Lock down further as per your security requirements and consider NSGs tailored for the compute instance.

## CLI usage (optional)

You can also run the stack with Terraform CLI:
- cd oci_postgres_tf_stack
- terraform init
- terraform apply -var='compartment_ocid=<ocid>' -var='psql_admin=<name>' [-var-file=<file>]