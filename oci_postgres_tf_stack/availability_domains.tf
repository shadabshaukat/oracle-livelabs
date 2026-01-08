# Dynamically discover available Availability Domains for the target region/tenancy.
# This avoids hard-coding AD-1/2/3 which can fail in single-AD regions or regions with different AD counts.
data "oci_identity_availability_domains" "ads" {
  compartment_id = coalesce(var.tenancy_ocid, var.compartment_ocid)
}
