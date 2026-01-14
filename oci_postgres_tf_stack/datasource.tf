# Random password generation and local selection logic
# If psql_admin_password is provided, it will be used.
# Otherwise, a strong random password will be generated at apply time.

resource "random_password" "psql_admin_password" {
  length           = 16
  upper            = true
  lower            = true
  numeric          = true
  special          = true
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
  min_special      = 1
  override_special = "!@#$%^&*()-_=+[]{}<>?"
}

locals {
  psql_admin_password = var.psql_admin_password != "" ? var.psql_admin_password : random_password.psql_admin_password.result
}

# Object Storage bucket for uploads
resource "oci_objectstorage_bucket" "uploads_bucket" {
  compartment_id = var.compartment_ocid
  name           = var.object_storage_bucket_name
  namespace      = data.oci_objectstorage_namespace.ns.namespace
  access_type    = "NoPublicAccess"
}

data "oci_objectstorage_namespace" "ns" {
  compartment_id = var.compartment_ocid
}
