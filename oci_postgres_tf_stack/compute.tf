#############################################
# Optional Small Compute Instance (Private) #
#############################################

# Select latest Oracle Linux image compatible with the chosen shape when compute_image_ocid is not provided
data "oci_core_images" "ol_compatible" {
  compartment_id         = var.compartment_ocid
  operating_system       = "Oracle Linux"
  shape                  = var.compute_shape
  sort_by                = "TIMECREATED"
  sort_order             = "DESC"
}

# Compute instance in the same private subnet as PostgreSQL (no public IP by default)
resource "oci_core_instance" "app_host" {
  count                = var.create_compute == true ? 1 : 0
  availability_domain  = data.oci_identity_availability_domains.ads.availability_domains[0].name
  compartment_id       = var.compartment_ocid
  display_name         = var.compute_display_name
  shape                = var.compute_shape

  # For Flex shapes (default is VM.Standard.E4.Flex). If a non-Flex shape is used this block may cause a validation error.
  shape_config {
    ocpus         = var.compute_ocpus
    memory_in_gbs = var.compute_memory_in_gbs
  }

  create_vnic_details {
    # For stack-created network, compute is placed in the public subnet.
    assign_public_ip = var.compute_assign_public_ip
    subnet_id        = var.create_vcn_subnet == true ? oci_core_subnet.vcn1_pub_subnet[0].id : (length(var.public_subnet_ocid) > 0 ? var.public_subnet_ocid : var.psql_subnet_ocid)
    nsg_ids          = var.compute_nsg_ids
  }

  source_details {
    source_type               = "image"
    source_id                 = var.compute_image_ocid != "" ? var.compute_image_ocid : data.oci_core_images.ol_compatible.images[0].id
    boot_volume_size_in_gbs   = var.compute_boot_volume_size_in_gbs
  }

  metadata = {
    ssh_authorized_keys = var.compute_ssh_public_key
  }

  timeouts {
    create = "60m"
    update = "60m"
    delete = "60m"
  }
}

# Primary VNIC attachments (for IP outputs and optional bootstrap)
data "oci_core_vnic_attachments" "app_host_vnics" {
  count          = var.create_compute == true ? 1 : 0
  compartment_id = var.compartment_ocid
  instance_id    = oci_core_instance.app_host[0].id
}

data "oci_core_vnic" "app_host_primary_vnic" {
  count   = var.create_compute == true ? 1 : 0
  vnic_id = data.oci_core_vnic_attachments.app_host_vnics[0].vnic_attachments[0].vnic_id
}
