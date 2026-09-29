# PingOne AIC Administrator Agent for Google Cloud Marketplace

![Ping Identity](ping_admin_agent/assets/ping-identity-logo.png)

Deploy the **PingOne AIC Administrator Agent** to your Google Cloud project
from Google Cloud Marketplace, then register it in your Gemini Enterprise app.
The agent gives your administrators a conversational, A2UI-rendered
administration console for your **PingOne Advanced Identity Cloud** tenant:
user search, password reset, session termination, MFA and group/role
membership, direct assignments, and audit lookup. Every change is gated behind
explicit operator confirmation.

- **Runtime:** Vertex AI Agent Engine (Agent Runtime) in **your** project:
  Google holds no tenant data; the agent runs inside your tenancy.
- **Access model:** Gemini Enterprise calls the runtime with each operator's
  own Google token (`cloud-platform` scope). Your admin group needs the Gemini
  Enterprise **Agent User** permission and Vertex AI User on the runtime
  project.
- **Tenant authentication:** one shared PingOne Advanced Identity Cloud
  service account; its credentials live in **your** Secret Manager and are
  never embedded in the agent.
- **UI:** A2UI v0.9: the administration workspace renders inside Gemini
  Enterprise's panel.

---

## Deployment steps

There are two phases: **deploy from Marketplace** (provisions the agent in
your Google Cloud project) and **register in Gemini Enterprise** (connects
your Gemini Enterprise app to it). Each takes minutes; do them in order.

### Step 0: Before you deploy

1. **A PingOne Advanced Identity Cloud tenant** with administrative access,
   and a tenant **service account** whose credentials the agent will use.
2. **A Google Cloud project** with billing enabled. The Marketplace
   deployment flow enables the required APIs (Vertex AI, Secret Manager,
   Compute Engine, Cloud Storage) for you.
3. **Your Google account needs** `resourcemanager.projects.setIamPolicy` on
   the project; the deployment creates a service account and grants it roles.
4. **A Gemini Enterprise app** where you can add agents, and a group (e.g.
   *IdP Admins*) that will operate the agent.

### Step 1: Create four secrets in Secret Manager

Create four secrets **in the target project** with values from your PingOne
Advanced Identity Cloud tenant. Names are yours to choose (the examples below
work); you will enter each name on the deployment page.

```bash
gcloud services enable secretmanager.googleapis.com

printf '%s' "<service-account-id>" | gcloud secrets create pingaic-service-account-id --data-file=-
printf '%s' "<private-jwk-json>"   | gcloud secrets create pingaic-private-jwk --data-file=-
printf '%s' "<audit-api-key>"      | gcloud secrets create pingaic-audit-api-key --data-file=-
printf '%s' "<audit-api-secret>"   | gcloud secrets create pingaic-audit-api-secret --data-file=-
```

| Secret | Contains |
| --- | --- |
| PingAIC service-account ID | The service account's `id` from the tenant |
| Private JWK | The matching private RSA JWK, as JSON |
| Audit API key | Monitoring API key |
| Audit API secret | Monitoring API secret |

### Step 2: Deploy from Google Cloud Marketplace

1. Open the product's Marketplace listing URL (Ping provides the direct link;
   the listing is private) and click **Deploy**.
2. Fill the deployment form:
   - **Deployment name**: a short name for the deployment.
   - **Service account**: accept the new-account default; it provisions the
     runtime identity with exactly the roles the agent needs.
   - **Your tenant settings**: PingOne Advanced Identity Cloud URL (e.g.
     `https://openam-<tenant>.forgeblocks.com`), realm (`alpha` by default),
     OAuth2 scopes, and the audit username field (`/payload/principal` works
     for most tenants).
   - **The four secret IDs** from step 1.
   - Optional: agent name, region, Gemini model, logo URL, always-on
     instances (`1` keeps one instance warm, with no cold start, at a small
     always-on cost; `0` scales to zero), and the VPC network/subnetwork for
     the validation instance (leave empty to create a dedicated network).
3. Accept the API enablements and IAM grants, then click **Deploy**.

The deployment takes 5–10 minutes and creates:

- a **Vertex AI Agent Engine** instance running the agent,
- a dedicated **runtime service account** with the roles the agent needs,
- a small idle **validation VM** (required by Marketplace today; not part of
  the agent's serving path), in a dedicated VPC unless you chose one.

When it finishes, the **`next_steps` output** on the deployment page prints
the exact commands for the rest of this guide.

### Step 3: Export the agent card

The agent card describes your deployed runtime to Gemini Enterprise. With
your own credentials (you deployed it, so you have access):

```bash
curl -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://<REGION>-aiplatform.googleapis.com/v1beta1/projects/<PROJECT>/locations/<REGION>/reasoningEngines/<ENGINE_ID>/a2a/v1/card" \
  | jq . > agent-card.json
```

Every placeholder is filled in for you in the deployment page's `next_steps`
output. If the request returns 403, grant yourself
`roles/aiplatform.user` (Vertex AI User) on the runtime project.

### Step 4: Create the Gemini Enterprise OAuth authorization

Gemini Enterprise calls the runtime with each operator's Google token. One
time per Gemini Enterprise project:

1. Create a Google OAuth client: an **Internal** consent screen with the
   `https://www.googleapis.com/auth/cloud-platform` scope, and a **Web
   application** client whose authorized redirect URI is exactly
   `https://vertexaisearch.cloud.google.com/oauth-redirect`.
2. Create the authorization resource in the Gemini Enterprise project (full
   copy-paste commands are in the deployment bundle's `REGISTRATION.md`).
3. Export the client secret as `OAUTH_CLIENT_SECRET` in your shell, never
   in a file or log.

### Step 5: Register the agent in Gemini Enterprise

In your Gemini Enterprise app: **Agents → Add agent → Custom agent via A2A**.

- Paste `agent-card.json` from step 3.
- Attach the **cloud-platform OAuth authorization** from step 4;
  registration uses `agentAuthorization`, which passes each operator's own
  token in the request header.
- Do **not** register as an ADK agent (the runtime serves A2A routes only)
  and do not skip the authorization (requests then arrive without a token
  and are rejected with 401).

Console-free equivalent (the deployment bundle's `REGISTRATION.md` carries
the full command with placeholders filled):

```bash
CARD="$(jq -c . agent-card.json)"
jq -n --arg card "$CARD" \
  --arg auth "projects/<GE_PROJECT_NUMBER>/locations/<GE_LOCATION>/authorizations/pingaic-runtime-invoke" \
  '{displayName:"PingOne AIC Administrator Agent",
    a2aAgentDefinition:{jsonAgentCard:$card},
    authorizationConfig:{agentAuthorization:$auth}}' > agent.json
curl --fail-with-body --request POST \
  --header "Authorization: Bearer $(gcloud auth print-access-token)" \
  --header "Content-Type: application/json" \
  --header "X-Goog-User-Project: <GE_PROJECT_ID>" \
  --data-binary @agent.json \
  "https://<GE_API_HOST>/v1alpha/projects/<GE_PROJECT_ID>/locations/<GE_LOCATION>/collections/default_collection/engines/<GE_APP_ID>/assistants/default_assistant/agents"
```

### Step 6: Set the registration icon

Without this step the registration shows a default initial-letter avatar.
Apply the included patch (in the deployment bundle):

```bash
curl --fail-with-body --request PATCH \
  --header "Authorization: Bearer $(gcloud auth print-access-token)" \
  --header "Content-Type: application/json" \
  --data-binary @gemini-enterprise-icon.json \
  "https://<GE_API_HOST>/v1alpha/<YOUR_AGENT_RESOURCE>?updateMask=icon"
```

This sets the registration icon to the official Ping Identity square mark,
the same avatar the PingID Device Management agent shows.

### Step 7: Grant operator access

1. Grant your administrator group **Vertex AI User** (`roles/aiplatform.user`)
   on the runtime project. Gemini Enterprise calls the runtime with each
   operator's own token, so their IAM is checked on every request.
2. In Gemini Enterprise, grant the same group **Agent User** on the
   registered agent.

### Step 8: Verify

- Each operator accepts **one Google consent prompt** on first use.
- In Gemini Enterprise, search for a test user; the administration panel
  renders with Ping Identity branding.
- Open the test user's profile, inspect relationships, and walk one change
  through review: **reject** it and confirm your PingOne AIC tenant is
  unchanged.
- Approvals inside the agent expire after 10 minutes and are single-use.
  After a redeploy or scale-from-zero, open a fresh workspace rather than
  retrying an old control.

---

## What the agent can do

- **Read:** user search and lookup, group/role membership, direct
  assignments, user activity, audit event lookup.
- **Write (every change behind an explicit review):** password reset, session
  termination, MFA management, group/role membership changes, application
  management (OIDC/SAML).
- **Review flow:** every write shows the exact change before execution;
  approve or reject in the panel or with `approve <id>` / `reject <id>`
  commands. Rejections execute nothing.

## Security model

| Connection | Access control |
| --- | --- |
| Administrator → Gemini Enterprise | Your Gemini Enterprise sign-in + **Agent User** permission |
| Gemini Enterprise → Agent Runtime | The operator's own Google OAuth2 token (`cloud-platform` scope), checked against runtime IAM on every request |
| Runtime → Secret Manager | The dedicated runtime service account reads exactly the four secrets you reference |
| Runtime → PingOne AIC | The tenant service account's JWT bearer grant; no interactive OAuth flow |

The runtime endpoint cannot be made unauthenticated: it is a Vertex AI API
URL, and Google rejects token-less requests before the agent runs.

## Troubleshooting

- **401 on the agent card export or from Gemini Enterprise:** no valid Google
  token reached the runtime. Run `gcloud auth login` / `gcloud auth
  application-default login`, and confirm the registration has the
  cloud-platform authorization attached with consent completed.
- **403 on the card export or agent calls:** the caller lacks
  `roles/aiplatform.user` on the runtime project.
- **"This approval is unknown, expired, or already used":** approvals are
  single-use and expire after 10 minutes; request the change again.
- **The agent cannot reach the tenant:** check the four secret IDs on the
  deployment, and that the values are your tenant service account's real
  credentials.
- **The deployment page shows no agent variables:** the deployment form is
  built from the package's metadata after validation; re-validate the
  deployment package in Producer Portal.

## Local development and manual deployment

This repository also supports deploying the agent directly (without
Marketplace) with `deployment/deploy.py`, plus offline tests, an A2UI
preview, and a live smoke script. See `PLAN.md` for the build history and
`deployment/registration-instructions.md` for the complete manual
registration bundle documentation. Run the offline test suite with:

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/
```

## Support

Ping Identity Support: https://support.pingidentity.com/s/
