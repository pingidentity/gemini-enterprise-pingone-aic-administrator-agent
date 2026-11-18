# PingOne AIC Administrator Agent deployment

Deploys the PingOne AIC Administrator Agent to Vertex AI Agent Engine in your
Google Cloud project, ready to register in Gemini Enterprise. Deploy it from
the Google Cloud Marketplace listing, or run this Terraform package yourself
(see [Deploying with Terraform](#deploying-with-terraform)).

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

You give the deployment each secret's ID, never its value: on the
Marketplace deployment page, or in `terraform.tfvars`. The agent reads the
values at runtime with its own identity, which can read only these four
secrets.

```bash
printf %s "<service-account-id>" | gcloud secrets create pingaic-service-account-id --project=<PROJECT_ID> --data-file=-
gcloud secrets create pingaic-private-jwk --project=<PROJECT_ID> --data-file=private-jwk.json
printf %s "<audit-api-key>" | gcloud secrets create pingaic-audit-api-key --project=<PROJECT_ID> --data-file=-
printf %s "<audit-api-secret>" | gcloud secrets create pingaic-audit-api-secret --project=<PROJECT_ID> --data-file=-
```

## What the deployment creates

- A Vertex AI Agent Engine instance running the agent (scale-to-zero, max one
  instance; UI state shared by the workers in SQLite)
- A dedicated runtime service account with the roles the agent needs, plus
  read access on exactly the four secrets you reference
- Marketplace deployments only: a small validation compute instance (required
  by Marketplace validation today; not part of the agent's serving path).
  Terraform deployments skip it with `create_validation_instance = false`.

## Deploying with Terraform

Use this route to deploy without the Marketplace listing.

**You need:**

- Terraform 1.5.7 or later, and the Google Cloud CLI.
- A Google Cloud project with billing enabled.
- On that project, either Owner, or all of: Vertex AI Administrator,
  Service Account Admin, Service Account User, Project IAM Admin, and
  Service Usage Admin (to enable the APIs).

**1. Enable the APIs:**

```bash
gcloud services enable aiplatform.googleapis.com secretmanager.googleapis.com \
  iam.googleapis.com cloudresourcemanager.googleapis.com \
  cloudtrace.googleapis.com logging.googleapis.com --project=<PROJECT_ID>
```

**2. Create the four secrets** (see [Before you deploy](#before-you-deploy)).

**3. Set your values.** Unzip the package, copy `terraform.tfvars.example`
to `terraform.tfvars`, and fill it in. Keep
`create_validation_instance = false`: the validation instance exists only
for Marketplace and its image is not available outside it. The remaining
settings in `variables.tf` have working defaults.
(`marketplace_test.tfvars` is used by Marketplace validation; ignore it.)

**4. Deploy:**

```bash
gcloud auth application-default login
terraform init
terraform plan
terraform apply
```

The apply takes about 10–15 minutes, most of it waiting for the new IAM
grants to take effect before Agent Engine starts. Terraform keeps its state
in `terraform.tfstate` in this folder: keep it to update or remove the
deployment later (`terraform destroy`), or configure a remote backend such
as a GCS bucket before `terraform init`.

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
