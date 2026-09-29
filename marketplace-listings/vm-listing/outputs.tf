/**
 * Copyright 2026 Ping Identity
 *
 * Post-deployment outputs shown on the Marketplace deployment page.
 */

output "agent_engine_id" {
  description = "Fully qualified Agent Engine id."
  value       = module.agent_engine.id
}

output "agent_name" {
  description = "The name of the deployed agent."
  value       = var.agent_engine_name
}

output "service_account_email" {
  description = "The runtime identity the agent uses."
  value       = module.agent_engine.service_account.email
}

output "next_steps" {
  description = "What to do after Terraform finishes."
  value       = <<-EOT
    1. Export the A2A card:
       curl -H "Authorization: Bearer $(gcloud auth print-access-token)" \
         "https://${var.region}-aiplatform.googleapis.com/v1beta1/projects/${local.project_id}/locations/${var.region}/reasoningEngines/$(basename ${module.agent_engine.id})/a2a/v1/card"
    2. Register the agent in Gemini Enterprise (Agents > Add agent > Custom
       agent via A2A), attach a cloud-platform OAuth authorization, and grant
       your admin group the Agent User role.
    3. Verify a user search renders the administration panel.
  EOT
}
