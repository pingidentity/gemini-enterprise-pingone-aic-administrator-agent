# PingOne AIC Administrator Agent: deployment guide

Deploys the PingOne AIC Administrator Agent to Vertex AI Agent Engine in your
Google Cloud project and registers it in your Gemini Enterprise app.

The agent administers a PingOne Advanced Identity Cloud tenant from Gemini
Enterprise: user search, password reset, session/MFA management, group/role
membership, direct assignments, and audit lookup. Every change is gated behind
explicit operator confirmation.

This guide deploys with Terraform. Deploying from the Google Cloud Marketplace
listing instead replaces steps 4 and 5 with the listing's deployment page;
every other step is the same (see [Deploying from Marketplace](#deploying-from-marketplace)).

## How access works

| Connection | How it is authenticated |
| --- | --- |
| Operator → Gemini Enterprise | The operator's Gemini Enterprise sign-in and the agent's **Agent User** permission |
| Gemini Enterprise → agent | The operator's own Google token (`cloud-platform` scope), obtained through an **OAuth authorization** you create in step 8. Each operator consents once. |
| Agent → Secret Manager | The deployment's runtime service account, which can read exactly the four secrets you create in step 3 |
| Agent → PingOne Advanced Identity Cloud | The tenant service account's credentials from those secrets; no user sign-in |

The OAuth authorization is required: the agent runs at a Vertex AI API
address, and Google rejects any request without a Google token before the
agent sees it.

## What the deployment creates

- A Vertex AI Agent Engine instance running the agent (at most one instance;
  `min_instances = 1` keeps it warm at a small always-on cost, `0` scales to
  zero)
- A dedicated runtime service account with the roles the agent needs, plus
  read access on exactly the four secrets you reference
- Marketplace deployments only: a small idle validation compute instance
  (required by Marketplace validation; not part of the agent's serving path).
  Terraform deployments skip it with `create_validation_instance = false`.

## Step 1: Check the prerequisites

- **PingOne Advanced Identity Cloud:** a tenant service account (its ID and
  private RSA JWK) with the administrative scopes the agent uses
  (`fr:idm:* fr:am:*` by default), and a monitoring API key and secret for
  audit lookup.
- **Google Cloud project** for the agent, with billing enabled.
- **Tools:** Terraform 1.5.7 or later, the Google Cloud CLI (`gcloud`), and
  `jq`.
- **Your access on the agent project:** Owner, or all of Vertex AI
  Administrator, Service Account Admin, Service Account User, Project IAM
  Admin, and Service Usage Admin.
- **Gemini Enterprise:** an app, and on its project the Discovery Engine
  Admin role (to create the authorization and register the agent). Note the
  app's project ID, location (`global`, `us`, or `eu`) and app ID.
- **An operator group** (for example *IdP Admins*) whose members have Gemini
  Enterprise licenses and belong to your Google Workspace or Cloud Identity
  organization.

Set these once in your shell; later steps use them:

```bash
PROJECT_ID="your-agent-project"          # where the agent runs
REGION="us-central1"
GE_PROJECT_ID="your-gemini-enterprise-project"
GE_LOCATION="us"                         # the app's location: global, us, or eu
GE_APP_ID="your-app-id"
GE_PROJECT_NUMBER="$(gcloud projects describe "$GE_PROJECT_ID" --format='value(projectNumber)')"
case "$GE_LOCATION" in
  global) GE_API_HOST="discoveryengine.googleapis.com" ;;
  *)      GE_API_HOST="${GE_LOCATION}-discoveryengine.googleapis.com" ;;
esac
```

To look up the app ID:

```bash
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "X-Goog-User-Project: $GE_PROJECT_ID" \
  "https://$GE_API_HOST/v1alpha/projects/$GE_PROJECT_ID/locations/$GE_LOCATION/collections/default_collection/engines" \
  | jq -r '.engines[] | "\(.name | split("/") | last)  \(.displayName)"'
```

## Step 2: Enable the APIs

```bash
gcloud services enable aiplatform.googleapis.com secretmanager.googleapis.com \
  iam.googleapis.com cloudresourcemanager.googleapis.com \
  cloudtrace.googleapis.com logging.googleapis.com --project="$PROJECT_ID"
```

## Step 3: Create the four secrets

Create them in the agent project with values from your tenant. The names are
yours to choose; the examples match `terraform.tfvars.example`.

```bash
# The private JWK file from your tenant; delete the file afterwards.
gcloud secrets create pingaic-private-jwk --project="$PROJECT_ID" --data-file=private-jwk.json
printf '%s' "<service-account-id>" | gcloud secrets create pingaic-service-account-id --project="$PROJECT_ID" --data-file=-
# Paste each monitoring credential at the prompt; it is not echoed or kept in shell history.
read -rs VALUE && printf '%s' "$VALUE" | gcloud secrets create pingaic-audit-api-key --project="$PROJECT_ID" --data-file=-; unset VALUE
read -rs VALUE && printf '%s' "$VALUE" | gcloud secrets create pingaic-audit-api-secret --project="$PROJECT_ID" --data-file=-; unset VALUE
```

| Secret | Contains |
| --- | --- |
| PingAIC service-account ID | The service account's `id` from the tenant |
| Private JWK | The matching private RSA JWK as JSON |
| Audit API key | Monitoring API key |
| Audit API secret | Monitoring API secret |

The deployment receives only the secret IDs, never their values. The agent
reads the values at runtime with its own identity.

## Step 4: Set your values

Unzip the package into an empty folder, then:

```bash
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:

| Setting | Value |
| --- | --- |
| `project_id`, `region` | The agent project and region |
| `goog_cm_deployment_name` | A short prefix for the runtime service account's name (letters, digits, hyphens) |
| `agent_engine_name` | The Agent Engine instance's display name |
| `ping_base_url`, `ping_realm` | Your tenant, e.g. `https://openam-<tenant>.forgeblocks.com`, realm `alpha` |
| `ping_*_secret` | The four secret IDs from step 3 |
| `create_validation_instance` | Keep `false`: the validation instance exists only for Marketplace, and its image is not available outside it |

The other settings in `variables.tf` have working defaults (model, scopes,
`min_instances`, logo). Ignore `marketplace_test.tfvars`; Marketplace
validation uses it.

## Step 5: Deploy

```bash
gcloud auth login
gcloud auth application-default login
terraform init
terraform plan
terraform apply
```

The plan adds 9 resources: the runtime service account, its five role
grants, the Agent Engine instance, and two waits for the grants to take
effect. The apply takes about 10–15 minutes, most of it waiting.

Terraform keeps its state in `terraform.tfstate` in this folder. Keep it to
update or remove the deployment later, or configure a remote backend (such
as a GCS bucket) before `terraform init`.

## Step 6: Export the agent card

The agent card tells Gemini Enterprise where the agent is and how to call it.

```bash
ENGINE="$(terraform output -raw agent_engine_id)"
curl -s --fail-with-body -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://${REGION}-aiplatform.googleapis.com/v1beta1/${ENGINE}/a2a/v1/card" \
  | jq . > agent-card.json
jq -r '.name, .url' agent-card.json
```

The last command prints the agent's name and an address ending in `/a2a`. A
403 means your account lacks Vertex AI User on the agent project.

## Step 7: Create an OAuth client for Gemini Enterprise

Create a new client for this agent in the **Gemini Enterprise project**, in
the Google Cloud console:

1. **APIs & Services → OAuth consent screen** (Google Auth Platform). If the
   project has none yet, create one with user type **Internal**, an app name
   (for example *Gemini Enterprise PingOne AIC Admin Agent*) and a support
   email. Under **Data Access**, add the scope
   `https://www.googleapis.com/auth/cloud-platform`.
2. **APIs & Services → Credentials → Create credentials → OAuth client ID.**
3. Application type: **Web application**. Name: for example
   *Gemini Enterprise PingOne AIC Admin Agent*.
4. **Authorized redirect URIs:** add both
   - `https://vertexaisearch.cloud.google.com/oauth-redirect`
   - `https://vertexaisearch.cloud.google.com/static/oauth/oauth.html`
5. Click **Create** and copy the **client ID** and **client secret**. The
   secret is shown only once; if you lose it, add a new secret on the client.

An **Internal** consent screen lets only accounts in your organization
consent, so operators must sign in with an organization account.

## Step 8: Create the OAuth authorization

The authorization stores the OAuth client in Gemini Enterprise. **Each agent
registration needs its own authorization**; one that is attached to another
agent is rejected with "is used by another agent".

```bash
AUTH_ID="pingaic-admin-agent-invoke"     # unique per agent registration
OAUTH_CLIENT_ID="<client ID from step 7>"
read -rs "OAUTH_CLIENT_SECRET?Client secret: "; echo   # zsh; in bash: read -rsp 'Client secret: ' OAUTH_CLIENT_SECRET; echo
export OAUTH_CLIENT_SECRET

jq -n --arg name "projects/$GE_PROJECT_NUMBER/locations/$GE_LOCATION/authorizations/$AUTH_ID" \
  --arg id "$OAUTH_CLIENT_ID" \
  --arg uri "https://accounts.google.com/o/oauth2/v2/auth?client_id=${OAUTH_CLIENT_ID}&redirect_uri=https%3A%2F%2Fvertexaisearch.cloud.google.com%2Foauth-redirect&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform&include_granted_scopes=true&response_type=code&access_type=offline&prompt=consent" \
  '{name: $name, serverSideOauth2: {clientId: $id, clientSecret: $ENV.OAUTH_CLIENT_SECRET,
    authorizationUri: $uri, tokenUri: "https://oauth2.googleapis.com/token"}}' \
| curl -s --fail-with-body --request POST \
    --header "Authorization: Bearer $(gcloud auth print-access-token)" \
    --header "Content-Type: application/json" \
    --header "X-Goog-User-Project: $GE_PROJECT_ID" \
    --data-binary @- \
    "https://$GE_API_HOST/v1alpha/projects/$GE_PROJECT_NUMBER/locations/$GE_LOCATION/authorizations?authorizationId=$AUTH_ID" \
| jq -r .name
unset OAUTH_CLIENT_SECRET
```

It prints the authorization's name. Keep every query parameter of the
authorization URL: without `access_type=offline` and `prompt=consent`,
operators are asked to consent again on every request.

## Step 9: Register the agent

```bash
jq -n --arg card "$(jq -c . agent-card.json)" \
  --arg auth "projects/$GE_PROJECT_NUMBER/locations/$GE_LOCATION/authorizations/$AUTH_ID" \
  '{displayName: "PingOne AIC Administrator Agent",
    description: "Provides user search, password reset, session/MFA management, group/role membership, direct assignments, and audit lookup.",
    icon: {uri: ($card | fromjson | .iconUrl)},
    a2aAgentDefinition: {jsonAgentCard: $card},
    authorizationConfig: {agentAuthorization: $auth}}' \
| curl -s --fail-with-body --request POST \
    --header "Authorization: Bearer $(gcloud auth print-access-token)" \
    --header "Content-Type: application/json" \
    --header "X-Goog-User-Project: $GE_PROJECT_ID" \
    --data-binary @- \
    "https://$GE_API_HOST/v1alpha/projects/$GE_PROJECT_ID/locations/$GE_LOCATION/collections/default_collection/engines/$GE_APP_ID/assistants/default_assistant/agents" \
| jq -r '.name, .state'
```

It prints the registration's name and `ENABLED`.

In the console instead (skip the command in step 8; the console creates the
authorization): open the app, then **Agents → Add agents → Custom agent via
A2A**, paste the contents of `agent-card.json`, and click **Preview agent
details → Next**. On the authorization step, enter the client ID and secret
from step 7, the authorization URL from step 8 (with your client ID in it),
and the token URL `https://oauth2.googleapis.com/token`, instead of clicking
**Skip & Finish**.

Register it as **Custom agent via A2A**, not as an ADK agent: the agent
serves only A2A routes and an ADK registration fails.

## Step 10: Grant operator access

1. Grant the operator group Vertex AI User on the agent project. Gemini
   Enterprise calls the agent with each operator's own token, so this is
   checked on every request:

   ```bash
   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="group:idp-admins@example.com" --role="roles/aiplatform.user"
   ```

2. In Gemini Enterprise, open the agent's user permissions and grant the
   same group **Agent User**.

## Step 11: Authorize and verify

Each operator does this once:

1. Open the Gemini Enterprise app, choose **PingOne AIC Administrator
   Agent**, and ask it to find a test user, e.g. `find jsmith`.
2. The first reply is *The agent requires additional authorization for:
   pingaic-admin-agent-invoke*. This is expected. Click **Authorize**,
   choose your organization account, and click **Allow**.
3. Ask again. The administration panel shows the search results.

Then check a change end to end: open the test user's profile, request a
change, **reject** it in the review, and confirm the tenant is unchanged.
Approvals expire after 10 minutes and can be used once.

## Updating the deployment

Change `terraform.tfvars` and run `terraform apply` again. A change that
replaces the Agent Engine instance gives it a new ID: export the card again
(step 6) and update the registration's card in Gemini Enterprise.

## Removing the deployment

1. Delete the registration: in Gemini Enterprise, **Agents**, then delete
   **PingOne AIC Administrator Agent**.
2. Delete the authorization:

   ```bash
   curl -s --fail-with-body --request DELETE \
     --header "Authorization: Bearer $(gcloud auth print-access-token)" \
     --header "X-Goog-User-Project: $GE_PROJECT_ID" \
     "https://$GE_API_HOST/v1alpha/projects/$GE_PROJECT_NUMBER/locations/$GE_LOCATION/authorizations/$AUTH_ID"
   ```

3. In the agent folder, run `terraform destroy`. It deletes the Agent Engine
   instance together with its chat sessions, the runtime service account and
   its grants.
4. Optionally delete the OAuth client (step 7) and the four secrets (step 3).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| *The agent requires additional authorization for: …* | Expected on each operator's first use. Click **Authorize**. |
| *Error during authorization* after you click **Allow** | Gemini Enterprise could not exchange the consent for a token. Check that the OAuth client lists the redirect URIs from step 7 and that the authorization holds the client's current secret. If it persists, create a new OAuth client (step 7) and a new authorization (step 8), then attach it to the registration: `PATCH …/agents/AGENT_ID?updateMask=authorizationConfig` with `{"authorizationConfig":{"agentAuthorization":"…/authorizations/NEW_ID"}}`. |
| *Access blocked: … can only be used within its organization* | You chose an account outside the organization that owns the Internal consent screen. Choose your organization account. |
| *… is used by another agent* when registering | The authorization belongs to another registration. Create a new one with a different `AUTH_ID` (step 8). |
| 401 from the agent | The registration has no authorization attached, or consent is incomplete. |
| 403 from the agent or the card export | The caller lacks Vertex AI User on the agent project (step 10). |
| `terraform apply` fails reading the image `pingone-aic-administrator-agent-image…` | Set `create_validation_instance = false` (step 4). |
| The agent cannot reach the tenant | Check the four secret IDs in `terraform.tfvars` and that the secrets hold the tenant service account's real credentials. |

## Deploying from Marketplace

From the Google Cloud Marketplace listing, do steps 1–3, then open the
listing (Ping provides the direct link), click **Deploy**, and enter your
tenant URL, realm and the four secret IDs on the deployment page. The
deployment enables the APIs and creates the validation instance for you. Its
`next_steps` output prints the card export command for step 6; continue with
steps 6–11.

## Support

Ping Identity Support: https://support.pingidentity.com/s/ ·
https://marketplace.pingone.com/item/google-gemini-enterprise-pingone-aic-administrator-agent
