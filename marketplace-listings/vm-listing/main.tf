/**
 * Copyright 2026 Ping Identity
 *
 * Deploy the PingOne AIC Administrator Agent to Vertex AI Agent Engine from
 * Google Cloud Marketplace. Patterned on Google's marketplace-agents-package
 * reference (github.com/VeerMuchandi/marketplace-agents-package, Apache-2.0):
 * an Agent Engine deployment through the vendored Cloud Foundation Fabric
 * agent-engine module, plus the compute instance Marketplace validation
 * currently requires (see the product diagram note: validation requires a VM
 * to pass).
 */

terraform {
  required_version = ">= 1.5.7"
  required_providers {
    google = {
      source = "hashicorp/google"
    }
  }
}

# When var.project_id is injected (Marketplace deployments), the provider
# targets that project explicitly. Resources without their own project
# attribute then resolve there. When it is not set, the provider falls back
# to the deploying credentials' own project. Referencing only the variable
# (not the client-config data source) keeps the dependency acyclic.
provider "google" {
  project = var.project_id
  region  = var.region
}

# Resolves to the project of the credentials running Terraform. Marketplace
# validation and Cloud Config deployments authenticate as the deploying
# project's identity, so when var.project_id is not injected (or set on the
# deployment page), resources land in that project rather than a placeholder.
data "google_client_config" "deployer" {}

locals {
  # var.project_id (injected by Marketplace on real deployments) wins; the
  # client-config data source is a convenience fallback for local plans and
  # is wrapped in try(): its read fails in sandboxes whose credentials carry
  # no project, and that must not fail the plan when project_id is injected.
  project_id = coalesce(
    try(var.project_id, null),
    try(data.google_client_config.deployer.project, null)
  )
  # The runtime identity needs to read exactly these secrets; the customer
  # creates them with the values from their PingOne Advanced Identity Cloud
  # tenant before deploying.
  secret_accessors = [
    var.ping_service_account_id_secret,
    var.ping_private_jwk_secret,
    var.ping_audit_api_key_secret,
    var.ping_audit_api_secret_secret,
  ]
  runtime_env = {
    PING_BASE_URL                    = var.ping_base_url
    PING_REALM                       = var.ping_realm
    PING_SCOPES                      = var.ping_scopes
    PING_ADMIN_MODEL                 = var.agent_model
    PING_ADMIN_MODEL_LOCATION        = var.agent_model_location
    PING_ADMIN_AUDIT_USERNAME_FIELD  = var.ping_audit_username_field
    PING_ADMIN_A2UI_ENABLED          = "true"
    PING_ADMIN_LOGO_URL              = var.ping_logo_url
    PING_ADMIN_CANVAS_LOGO           = var.ping_canvas_logo
    PING_ADMIN_STATE_STORE           = "sqlite"
    GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY = "true"
  }
}

# The Agent Engine API rejects env entries with empty values ("Required
# field is not set"), so the optional token-URL override is emitted only
# when the customer set one.
locals {
  runtime_env_complete = (
    var.ping_token_url == ""
    ? local.runtime_env
    : merge(local.runtime_env, { PING_TOKEN_URL = var.ping_token_url })
  )
}

# 1. Agent Engine deployment through the vendored Cloud Foundation Fabric module.
module "agent_engine" {
  source     = "./modules/agent-engine"
  name       = var.agent_engine_name
  project_id = local.project_id
  region     = var.region

  agent_engine_config = {
    agent_framework = "a2a"
    # The API derives the A2A HTTP surface from the declared class methods;
    # without them the engine boots with no routes ("does not have A2A
    # methods defined"). Mirrors exactly what the SDK packaging path
    # generates from the A2aAgent template (verified against the working
    # deployment); the embedded card is the packaging-time card the runtime
    # rewrites at set_up.
    class_methods = jsondecode(file("assets/class_methods.json"))
    environment_variables = merge(local.runtime_env_complete, {
      PROJECT_ID                = local.project_id
      LOCATION                  = var.region
      GOOGLE_GENAI_USE_VERTEXAI = "1"
    })
    # Agent Engine delivers each secret's PAYLOAD as the environment
    # variable's value. The config reads the plaintext names directly, so
    # inject under those names; the *_SECRET spellings are for resource-name
    # references, which Agent Engine does not provide here.
    secret_environment_variables = {
      PING_ADMIN_SERVICE_ACCOUNT_ID = { secret_id = var.ping_service_account_id_secret }
      PING_ADMIN_PRIVATE_JWK        = { secret_id = var.ping_private_jwk_secret }
      PING_ADMIN_AUDIT_API_KEY      = { secret_id = var.ping_audit_api_key_secret }
      PING_ADMIN_AUDIT_API_SECRET   = { secret_id = var.ping_audit_api_secret_secret }
    }
    min_instances = var.min_instances
    max_instances = 1
  }

  service_account_config = {
    create = true
    name   = substr("${var.goog_cm_deployment_name}-agent", 0, 30)
    roles = [
      "roles/aiplatform.user",
      "roles/storage.objectViewer",
      "roles/serviceusage.serviceUsageConsumer",
      "roles/cloudtrace.agent",
      "roles/secretmanager.secretAccessor",
    ]
  }

  deployment_files = {
    source_config = {
      source_path       = "assets/source.tar.gz"
      # The module-level instance in runtime.py: Agent Engine serves the
      # constructed A2A application object, not the class (matching the
      # working SDK deployment, whose framework label is "a2a").
      entrypoint_module = "${var.agent_package_name}.runtime"
      entrypoint_object = "agent"
      requirements_path = "${var.agent_package_name}/requirements.txt"
    }
  }
}

# 2. Secret-level access for the runtime identity. Google's
# secret_environment_variables wiring grants the accessor on the referenced
# secrets itself, so this extra grant is opt-in: enable it only for
# deployments that keep their secrets otherwise restricted (the secret must
# already exist, and the deployer's identity needs secretmanager.admin to
# set its IAM policy).
variable "manage_secret_iam" {
  description = "Grant the runtime identity secretAccessor on the referenced secrets. Enable only when the secrets exist and are otherwise restricted; Google's secret_environment_variables wiring already grants access."
  type        = bool
  default     = false
}

resource "google_secret_manager_secret_iam_member" "runtime_accessor" {
  for_each = var.manage_secret_iam ? toset([
    var.ping_service_account_id_secret,
    var.ping_private_jwk_secret,
    var.ping_audit_api_key_secret,
    var.ping_audit_api_secret_secret,
  ]) : toset([])
  # The secret must exist before the binding; customers create the secrets
  # before deploying (see README).
  secret_id = "projects/${local.project_id}/secrets/${each.key}"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${module.agent_engine.service_account.email}"
}

# 3. Compute instance: Marketplace validation currently requires a VM in the
# package. The instance is a small idle Debian box and is not part of the
# agent's serving path. When var.network is empty (the default), a dedicated
# VPC and subnet are created for it. The deployment then works in projects
# that have no pre-existing network (e.g. fresh validation projects).
# Marketplace's network picker submits "default" when the customer takes the
# default selection, and validation projects may have no default VPC at all,
# so the bare name "default" is treated like "unspecified": the deployment
# creates its dedicated network instead of referencing a possibly missing
# one. Customers with a real custom VPC pass its name or self-link.
locals {
  # null and "" both mean "unspecified" (a harness may inject either form);
  # the bare name "default" is the network picker's default submission.
  use_created_network = (
    var.network == null || var.network == "" || var.network == "default"
  )
}

resource "google_compute_network" "validation" {
  count   = local.use_created_network ? 1 : 0
  name    = "${var.goog_cm_deployment_name}-network"
  project = local.project_id

  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "validation" {
  count         = local.use_created_network ? 1 : 0
  name          = "${var.goog_cm_deployment_name}-subnet"
  project       = local.project_id
  region        = var.region
  network       = google_compute_network.validation[0].id
  ip_cidr_range = "10.10.0.0/28"
}

resource "google_compute_instance" "instance" {
  name         = "${var.goog_cm_deployment_name}-vm"
  machine_type = var.machine_type
  zone         = var.zone

  tags = ["${var.goog_cm_deployment_name}-deployment"]

  boot_disk {
    device_name = "aic-admin-agent-boot-disk"
    initialize_params {
      size  = var.boot_disk_size
      type  = var.boot_disk_type
      image = var.source_image
    }
  }

  metadata = {
    google-logging-enable    = "0"
    google-monitoring-enable = "0"
  }

  network_interface {
    network = local.use_created_network ? google_compute_network.validation[0].id : var.network
    # Enterprise customers often manage custom VPCs; with no subnetwork given,
    # a created deployment uses its dedicated subnet and a referenced network
    # uses the network's regional subnet in the instance's zone.
    subnetwork = coalesce(
      try(var.subnetwork == "" ? null : var.subnetwork, null),
      local.use_created_network ? google_compute_subnetwork.validation[0].id : null
    )
    # No external IP: the validation instance is not part of the serving
    # path and must not widen the deployment's attack surface. Omitting the
    # access_config block means no external address.
  }

  # A dedicated, minimal identity: the validation instance shares no
  # permissions with the agent runtime.
  service_account {
    email  = google_service_account.validation_vm.email
    scopes = ["https://www.googleapis.com/auth/devstorage.read_only"]
  }
}

resource "google_service_account" "validation_vm" {
  account_id   = substr("${var.goog_cm_deployment_name}-vm", 0, 30)
  display_name = "Marketplace validation instance"
}
