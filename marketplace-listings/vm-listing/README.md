# PingOne AIC Administrator Agent for Google Cloud Marketplace deployment

Deploys the PingOne AIC Administrator Agent to Vertex AI Agent Engine in your
Google Cloud project, ready to register in Gemini Enterprise.

The agent administers a PingOne Advanced Identity Cloud tenant from Gemini
Enterprise: user search, password reset, session/MFA management, group/role
membership, direct assignments, and audit lookup. Every change is gated behind
explicit operator confirmation.

## Before you deploy

Create four Secret Manager secrets in the target project with values from
your PingOne Advanced Identity Cloud tenant:

| Secret | Contains |
| --- | --- |
| PingAIC service-account ID | The service account's `id` from the tenant |
| Private JWK | The matching private RSA JWK as JSON |
| Audit API key | Monitoring API key |
| Audit API secret | Monitoring API secret |

Each secret ID is entered on the deployment page. The deployment also needs
APIs enabled (Vertex AI, Secret Manager, Compute Engine); the Marketplace
deployment flow enables them for you.

## What the deployment creates

- A Vertex AI Agent Engine instance running the agent (scale-to-zero, max one
  instance; UI state shared by the workers in SQLite)
- A dedicated runtime service account with the roles the agent needs, plus
  read access on exactly the four secrets you reference
- A small validation compute instance (required by Marketplace validation
  today; not part of the agent's serving path)

## After deployment

1. Export the A2A card from the deployed runtime (the `next_steps` output
   prints the exact command).
2. Register the agent in Gemini Enterprise: Agents → Add agent → Custom
   agent via A2A, paste the card, attach a `cloud-platform` OAuth
   authorization, and grant your administrator group the Agent User role.
3. Run a user search in Gemini Enterprise and confirm the administration
   panel renders.

## Support

Ping Identity Support: https://support.pingidentity.com/s/ ·
https://marketplace.pingone.com/item/google-gemini-enterprise-pingone-aic-administrator-agent
