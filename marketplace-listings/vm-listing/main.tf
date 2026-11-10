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
    time = {
      source = "hashicorp/time"
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
  # The secrets the customer creates with the values from their PingOne
  # Advanced Identity Cloud tenant. The runtime receives only their version
  # resource names and reads the payloads itself, with its own identity, when
  # it first calls the tenant: no payload is part of the engine's
  # configuration, and the runtime identity may read exactly these secrets.
  secret_ids = {
    PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET = var.ping_service_account_id_secret
    PING_ADMIN_PRIVATE_JWK_SECRET        = var.ping_private_jwk_secret
    PING_ADMIN_AUDIT_API_KEY_SECRET      = var.ping_audit_api_key_secret
    PING_ADMIN_AUDIT_API_SECRET_SECRET   = var.ping_audit_api_secret_secret
  }
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
    # Secret references, not payloads: the same contract as the SDK
    # deployer (deployment/deploy.py). Agent Engine's secret_env would
    # instead read the secrets with Google's Reasoning Engine service agent,
    # which this deployment does not grant.
    environment_variables = merge(local.runtime_env_complete, {
      PROJECT_ID                = local.project_id
      LOCATION                  = var.region
      GOOGLE_GENAI_USE_VERTEXAI = "1"
      }, {
      for name, secret in local.secret_ids :
      name => "projects/${local.project_id}/secrets/${secret}/versions/latest"
    })
    min_instances = var.min_instances
    max_instances = 1
  }

  # The runtime identity and its grants are created below, outside the
  # module, so that the engine is created only after they are in effect.
  service_account_config = {
    create = false
    email  = google_service_account.runtime.email
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

  # Agent Engine does not retry a deployment whose identity's bindings are
  # not yet in effect, so the engine waits for them.
  depends_on = [time_sleep.runtime_iam]
}

# 2. Runtime identity and its access.
# Service account IDs hold at most 30 characters. A deployment name of up to
# 28 keeps the IDs earlier versions created. A longer one would truncate to
# the same ID for both accounts, so it is shortened with a hash of the full
# name instead.
locals {
  sa_name_base  = replace(var.goog_cm_deployment_name, "_", "-")
  sa_name_short = length(local.sa_name_base) <= 28
  sa_name_long  = "${substr(local.sa_name_base, 0, 16)}-${substr(sha256(var.goog_cm_deployment_name), 0, 6)}"
  runtime_sa_id = (
    local.sa_name_short
    ? substr("${local.sa_name_base}-agent", 0, 30)
    : "${local.sa_name_long}-agent"
  )
  validation_sa_id = (
    local.sa_name_short
    ? substr("${local.sa_name_base}-vm", 0, 30)
    : "${local.sa_name_long}-vm"
  )
}

resource "google_service_account" "runtime" {
  project      = local.project_id
  account_id   = local.runtime_sa_id
  display_name = substr("${var.goog_cm_deployment_name}-agent", 0, 30)
}

locals {
  runtime_roles = [
    "roles/aiplatform.user",
    "roles/storage.objectViewer",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/cloudtrace.agent",
  ]
}

resource "google_project_iam_member" "runtime" {
  for_each = toset(local.runtime_roles)
  project  = local.project_id
  role     = each.key
  member   = google_service_account.runtime.member
}

data "google_project" "runtime" {
  project_id = local.project_id
}

# Secret access is limited to the four secrets named on the deployment page.
# A conditional project binding, rather than one binding per secret, works
# whether or not the secrets exist yet and needs no Secret Manager admin
# rights. Secret Manager resource names in conditions carry the project
# number; the ID never matches.
resource "google_project_iam_member" "runtime_secret_access" {
  project = local.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = google_service_account.runtime.member

  condition {
    title       = "pingaic-agent-secrets"
    description = "Only the PingOne AIC secrets named on the deployment."
    expression = join(" || ", [
      for secret in sort(distinct(values(local.secret_ids))) :
      "resource.name.startsWith(\"projects/${data.google_project.runtime.number}/secrets/${secret}/\")"
    ])
  }
}

resource "time_sleep" "runtime_iam" {
  create_duration = "2m"

  depends_on = [
    google_project_iam_member.runtime,
    google_project_iam_member.runtime_secret_access,
  ]
}

# Earlier versions of this package created the runtime identity inside the
# module. Carry an existing deployment's identity and grants over instead of
# replacing them; its unconditional, project-wide secret access is removed.
moved {
  from = module.agent_engine.google_service_account.service_account[0]
  to   = google_service_account.runtime
}

moved {
  from = module.agent_engine.google_project_iam_member.default["roles/aiplatform.user"]
  to   = google_project_iam_member.runtime["roles/aiplatform.user"]
}

moved {
  from = module.agent_engine.google_project_iam_member.default["roles/storage.objectViewer"]
  to   = google_project_iam_member.runtime["roles/storage.objectViewer"]
}

moved {
  from = module.agent_engine.google_project_iam_member.default["roles/serviceusage.serviceUsageConsumer"]
  to   = google_project_iam_member.runtime["roles/serviceusage.serviceUsageConsumer"]
}

moved {
  from = module.agent_engine.google_project_iam_member.default["roles/cloudtrace.agent"]
  to   = google_project_iam_member.runtime["roles/cloudtrace.agent"]
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
  # The instance must sit in the deployment region, where its subnet is
  # created: a zone outside that region is replaced by one inside it.
  zone_in_region = startswith(coalesce(var.zone, "-"), "${var.region}-")
  zone = (
    local.zone_in_region
    ? var.zone
    : sort(data.google_compute_zones.region[0].names)[0]
  )
}

data "google_compute_zones" "region" {
  count   = local.zone_in_region ? 0 : 1
  project = local.project_id
  region  = var.region
  status  = "UP"
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
  zone         = local.zone

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
  account_id   = local.validation_sa_id
  display_name = "Marketplace validation instance"
}
