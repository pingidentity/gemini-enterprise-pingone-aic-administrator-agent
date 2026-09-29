/**
 * Copyright 2026 Ping Identity
 *
 * Variables for the PingOne AIC Administrator Agent Marketplace deployment.
 */

variable "project_id" {
  description = "The ID of the project in which to provision resources. Leave empty to deploy into the authenticated credentials' project (Marketplace injects the customer's project on real deployments)."
  type        = string
  default     = null
}

// Marketplace requires this variable name to be declared
variable "goog_cm_deployment_name" {
  description = "The name of the deployment."
  type        = string
  # Marketplace injects the deployment name chosen on the deployment page.
  default     = "pingaic-deployment"
}

variable "agent_engine_name" {
  description = "The name of the Agent Engine application."
  type        = string
  default     = "pingone-aic-administrator"
}

variable "region" {
  description = "The GCP region to deploy to."
  type        = string
  default     = "us-central1"
}

variable "agent_package_name" {
  description = "The name of the Python package in the source archive."
  type        = string
  default     = "ping_admin_agent"
}

# PingOne Advanced Identity Cloud connection
variable "ping_base_url" {
  description = "Base URL of the customer's PingOne Advanced Identity Cloud tenant, e.g. https://openam-<tenant>.forgeblocks.com."
  type        = string
  # A placeholder keeps `terraform plan` satisfiable when no tenant URL is
  # supplied (Marketplace validation plans in its own project). Empty would
  # break the runtime, but validation never applies the plan; a real
  # deployment requires the customer's value on the deployment page.
  default     = "https://openam-placeholder.forgeblocks.com"
}

variable "ping_realm" {
  description = "The PingOne Advanced Identity Cloud realm to administer, e.g. alpha."
  type        = string
  default     = "alpha"
}

variable "ping_scopes" {
  description = "OAuth2 scopes the agent requests from the tenant, space-separated."
  type        = string
  default     = "fr:idm:* fr:am:*"
}

variable "ping_audit_username_field" {
  description = "Audit event JSON-pointer path holding the username, e.g. /payload/principal. Leave empty to disable username filtering."
  type        = string
  default     = "/payload/principal"
}

# PingOne Advanced Identity Cloud service-account credentials (Secret Manager)
# Secret IDs are names, not payloads, and are resolved only at apply time;
# placeholders keep `terraform plan` satisfiable for Marketplace validation,
# which plans in its own project before any customer secret exists.
variable "ping_service_account_id_secret" {
  description = "Secret Manager secret ID containing the PingAIC service-account ID."
  type        = string
  default     = "pingaic-service-account-id"
}

variable "ping_private_jwk_secret" {
  description = "Secret Manager secret ID containing the matching private RSA JWK as JSON."
  type        = string
  default     = "pingaic-private-jwk"
}

variable "ping_audit_api_key_secret" {
  description = "Secret Manager secret ID containing the monitoring API key."
  type        = string
  default     = "pingaic-audit-api-key"
}

variable "ping_audit_api_secret_secret" {
  description = "Secret Manager secret ID containing the monitoring API secret."
  type        = string
  default     = "pingaic-audit-api-secret"
}

variable "ping_token_url" {
  description = "OAuth2 token endpoint override for the PingAIC tenant. Leave empty to derive it from the base URL."
  type        = string
  default     = ""
}

# Agent model and branding
variable "agent_model" {
  description = "The Gemini model the agent uses."
  type        = string
  default     = "gemini-3.5-flash"
}

variable "agent_model_location" {
  description = "The location for model requests, e.g. global or us."
  type        = string
  default     = "global"
}

variable "ping_logo_url" {
  description = "HTTPS URL of the Ping Identity logo PNG used on the card and panel."
  type        = string
  default     = "https://storage.googleapis.com/tech-partner-ping-public-branding/ping-logo-square-1251.png"
}

variable "ping_canvas_logo" {
  description = "Show the logo at the top of the Gemini Enterprise workspace panel."
  type        = bool
  default     = true
}

variable "min_instances" {
  description = "Minimum Agent Engine instances kept warm. 1 avoids a cold start on the first prompt after idle (small always-on cost); 0 scales to zero."
  type        = number
  default     = 1
}

# Compute instance (Marketplace validation requires a VM in the package)
variable "zone" {
  description = "The GCP zone for the validation compute instance."
  type        = string
  default     = "us-central1-a"
}

variable "machine_type" {
  description = "Machine type for the validation compute instance."
  type        = string
  default     = "e2-small"
}

variable "boot_disk_size" {
  description = "Size of the boot disk in GB."
  type        = number
  default     = 10
}

variable "boot_disk_type" {
  description = "Type of the boot disk."
  type        = string
  default     = "pd-balanced"
}

variable "source_image" {
  description = "Source image for the boot disk."
  type        = string
  default     = "projects/forgerock-public/global/images/pingone-aic-administrator-agent-image"
}

variable "network" {
  description = "The VPC network to attach the validation instance to, by name or self-link. Leave empty (or take the default selection) to create a dedicated network for this deployment."
  type        = string
  default     = ""
}

variable "subnetwork" {
  description = "The subnetwork to attach the validation instance to, by name or self-link. Leave empty to let Google Cloud choose or create one in the instance's region."
  type        = string
  default     = ""
}
