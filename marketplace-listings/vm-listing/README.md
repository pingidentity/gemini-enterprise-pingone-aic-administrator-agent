# PingOne AIC Administrator Agent: Terraform deployment guide

This guide deploys the PingOne AIC Administrator Agent into your own Google
Cloud project with Terraform, without Google Cloud Marketplace, and registers
it in your Gemini Enterprise app. Allow about an hour; the Terraform apply
itself takes about 10 minutes.

The agent administers a PingOne Advanced Identity Cloud tenant from Gemini
Enterprise: user search, password reset, session/MFA management, group/role
membership, direct assignments, and audit lookup. Every change waits for the
operator's explicit approval.

Deploying from the Google Cloud Marketplace listing instead replaces steps 4
and 5 with the listing's deployment page; every other step is the same (see
[Deploying from Marketplace](#deploying-from-marketplace)).

## How it works

Everything the agent runs stays in your Google Cloud projects: the agent, its
secrets, the Gemini model it calls, and its logs. Gemini Enterprise calls the
agent with each operator's own Google token, which it gets through the OAuth
authorization. The agent then calls your tenant as the tenant service
account, so operators never sign in to the tenant.

| Connection | How it is authenticated |
| --- | --- |
| Operator → Gemini Enterprise | The operator's Gemini Enterprise sign-in and the agent's **Agent User** permission |
| Gemini Enterprise → agent | The operator's own Google token (`cloud-platform` scope), obtained through the OAuth authorization from step 8 and checked against IAM on the agent project on every request. Each operator consents once. |
| Agent → Secret Manager | The deployment's runtime service account, which can read exactly the four secrets from step 3 |
| Agent → PingOne Advanced Identity Cloud | The tenant service account's credentials from those secrets; no user sign-in |

## Access controls

> **Anyone who can call the agent can administer your tenant.** The agent
> makes every change as the tenant service account, so the tenant can't tell
> operators apart or limit what each one does. Google Cloud IAM decides who
> can call the agent, and Gemini Enterprise decides who sees it. You
> configure both.

Through the agent, a caller can read any user's profile and activity in the
realm, reset passwords, end sessions, remove MFA devices, change group and
internal role memberships, manage assignments, and create OIDC and SAML
applications. Each change waits for approval, but the person who asks for it
also approves it: the review catches mistakes, not misuse.

### Who can call the agent

| Check | Where it's set | What it decides |
| --- | --- | --- |
| The permission to call the agent, `aiplatform.reasoningEngines.query` | IAM on the agent project, including roles granted on its folder and organization | Whether Google accepts the request. It's checked on every request, against the operator's own Google account |
| Agent User | The agent's user permissions in your Gemini Enterprise app | Whether the agent appears for the operator in Gemini Enterprise |

The IAM permission is what protects your tenant. Anyone who holds it can call
the agent directly with their own Google token, without Gemini Enterprise.
[Many roles include it](https://docs.cloud.google.com/iam/docs/roles-permissions/aiplatform):
even the read-only Vertex AI Viewer (`roles/aiplatform.viewer`), as well as
Vertex AI User, Vertex AI Administrator and Owner.

### Set up access

1. **Deploy the agent in its own project.** Use an agent project that holds
   nothing else, so roles on it grant access to this agent only. Avoid shared
   projects where many people already hold Vertex AI or basic roles.
2. **Give operators a role that can only call the agent.** Vertex AI User
   (`roles/aiplatform.user`), the usual role for calling agents, also lets its
   holders change, replace and delete the agent.
   [Step 10](#step-10-give-operators-access) creates a narrower role and
   grants it to the operator group.
3. **Remove everyone else's access.** List the roles granted on the agent
   project and inherited from its folder and organization. Besides the
   operator group, only the people who deploy the agent should hold a role
   that includes the permission:

   ```bash
   gcloud projects get-ancestors-iam-policy "$PROJECT_ID" --format=json \
     | jq -r '.[] | .type as $t | .id as $id | .policy.bindings[]? | "\($t) \($id)  \(.role)  \(.members | join(", "))"'
   ```

   To check a role's permissions, open it under **IAM & Admin → Roles**, or
   run `gcloud iam roles describe` with its name. Roles granted on the folder
   or organization are managed there, so ask their owners to remove any that
   reach the agent project.
4. **Share the agent only with the operator group** in Gemini Enterprise,
   never with *All users*. After you register the agent, check its user
   permissions and remove anyone else.
5. **Control who joins the operator group.** Joining it grants administration
   of your tenant. Don't let people join on their own, limit who can add
   members, and review the members regularly.
6. **Keep these roles to the people who deploy the agent.** They reach the
   tenant credentials or the agent itself, outside the checks above:
   - Reading the four secrets from step 3, whose private JWK alone signs in to
     the tenant as the service account: Secret Manager Secret Accessor,
     Secret Manager Admin and Owner.
   - Acting as the runtime service account `<prefix>-agent`, which can read
     those secrets: Service Account Token Creator or Service Account User on
     it.
   - Changing or deleting the agent: Vertex AI User, Vertex AI Administrator
     and Owner on the agent project.
   - Administering the Gemini Enterprise app, which can share the agent with
     anyone: Discovery Engine Admin on its project.
7. **Grant the tenant service account only the scopes the agent uses,**
   `fr:idm:*` and `fr:am:*`. If its private JWK may have leaked, replace the
   key in the tenant and store the new one as a new version of the secret.
8. **Keep a record of who made each change.** The tenant's audit log names
   the tenant service account, not the operator. Turn on Data Access audit
   logs for the Vertex AI API in the agent project and for the Discovery
   Engine API in the Gemini Enterprise project. After step 11, check in Logs
   Explorer that your search appears under your account.
9. **Remove access in one place.** Taking someone out of the operator group
   ends their access on their next request, once Google applies the change. A
   consent they gave earlier doesn't keep it.

## What the deployment creates

- A Vertex AI Agent Engine instance running the agent (at most one instance;
  `min_instances = 1` keeps it warm at a small always-on cost, `0` scales to
  zero)
- A dedicated runtime service account with the roles the agent needs, plus
  read access on exactly the four secrets you reference
- Marketplace deployments only: a small idle validation compute instance
  (required by Marketplace validation; not part of the agent's serving path).
  Terraform deployments skip it with `create_validation_instance = false`.

## Before you start

Gather these before step 1. The tenant credentials and the Gemini Enterprise
details often belong to different teams.

| You need | Details |
| --- | --- |
| PingOne AIC service account | A tenant service account: its ID and its private RSA JWK (JSON), with the administrative scopes the agent uses (`fr:idm:* fr:am:*` by default) |
| PingOne AIC monitoring credentials | A monitoring API key and secret, used for audit lookup |
| Agent project | A Google Cloud project for the agent, with billing enabled |
| Your roles on the agent project | Owner, or all of: Vertex AI Administrator, Service Account Admin, Service Account User, Project IAM Admin, Role Administrator, Service Usage Admin |
| Gemini Enterprise app | Its project ID, location (`global`, `us` or `eu`) and app ID, plus Discovery Engine Admin on its project |
| Operator group | A group (for example *IdP Admins*) whose members have Gemini Enterprise licenses and belong to your Google Workspace or Cloud Identity organization. Everyone in it can administer your tenant |
| Tools | Terraform 1.5.7 or later, the Google Cloud CLI (`gcloud`) and `jq` |
| Deployment package | `pingone-aic-administrator-agent.zip`, version 1.0.4-6 or later, from Ping Identity |

The agent project and the Gemini Enterprise project can be the same project,
but a separate agent project keeps access to the agent narrower (see
[Access controls](#access-controls)).

## Step 1: Get the package and set your shell variables

1. Unzip `pingone-aic-administrator-agent.zip` into a new, empty folder that
   you will keep. Terraform stores the deployment's state there, and you need
   it to update or remove the deployment later.
2. Open a terminal in that folder and sign in to Google Cloud, both for
   `gcloud` and for Terraform:

   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```

3. Set these variables once in that terminal. Later steps use them, so run
   every step in the same terminal session:

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

4. If you don't know the app ID, list the apps in the Gemini Enterprise
   project. Each line shows an app ID and its name:

   ```bash
   curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
     -H "X-Goog-User-Project: $GE_PROJECT_ID" \
     "https://$GE_API_HOST/v1alpha/projects/$GE_PROJECT_ID/locations/$GE_LOCATION/collections/default_collection/engines" \
     | jq -r '.engines[] | "\(.name | split("/") | last)  \(.displayName)"'
   ```

## Step 2: Enable the Google Cloud APIs

Enable the APIs the agent uses in the agent project:

```bash
gcloud services enable aiplatform.googleapis.com secretmanager.googleapis.com \
  iam.googleapis.com cloudresourcemanager.googleapis.com \
  cloudtrace.googleapis.com logging.googleapis.com --project="$PROJECT_ID"
```

The Gemini Enterprise project already has the Discovery Engine API enabled,
because your app runs there.

## Step 3: Store the tenant credentials in Secret Manager

Create four secrets in the agent project with values from your tenant. The
deployment gets only the secret IDs, never their values: the agent reads the
values at runtime, with its own identity, and can read only these four
secrets.

| Secret (example ID) | Contains |
| --- | --- |
| `pingaic-service-account-id` | The tenant service account's ID |
| `pingaic-private-jwk` | The service account's private RSA JWK, as JSON |
| `pingaic-audit-api-key` | The monitoring API key |
| `pingaic-audit-api-secret` | The monitoring API secret |

The IDs are yours to choose; these match the example settings file. Run:

```bash
# The private JWK file from your tenant. Delete the file afterwards.
gcloud secrets create pingaic-private-jwk --project="$PROJECT_ID" --data-file=private-jwk.json
printf '%s' "<service-account-id>" | gcloud secrets create pingaic-service-account-id --project="$PROJECT_ID" --data-file=-
# Paste each monitoring credential at the prompt. It is not shown or kept in shell history.
read -rs VALUE && printf '%s' "$VALUE" | gcloud secrets create pingaic-audit-api-key --project="$PROJECT_ID" --data-file=-; unset VALUE
read -rs VALUE && printf '%s' "$VALUE" | gcloud secrets create pingaic-audit-api-secret --project="$PROJECT_ID" --data-file=-; unset VALUE
```

## Step 4: Set your Terraform values

Copy the example settings file in the package folder, then edit it:

```bash
cp terraform.tfvars.example terraform.tfvars
```

A finished `terraform.tfvars` looks like this:

```hcl
project_id = "your-agent-project"
region     = "us-central1"
goog_cm_deployment_name = "pingaic"
agent_engine_name       = "pingone-aic-administrator"
ping_base_url = "https://openam-<tenant>.forgeblocks.com"
ping_realm    = "alpha"
ping_service_account_id_secret = "pingaic-service-account-id"
ping_private_jwk_secret        = "pingaic-private-jwk"
ping_audit_api_key_secret      = "pingaic-audit-api-key"
ping_audit_api_secret_secret   = "pingaic-audit-api-secret"
create_validation_instance = false
```

| Setting | Value |
| --- | --- |
| `project_id`, `region` | The agent project and region |
| `goog_cm_deployment_name` | A short prefix (letters, digits, hyphens). The runtime service account is named `<prefix>-agent`, so choose a prefix no existing service account in the project already uses |
| `agent_engine_name` | The Agent Engine instance's display name |
| `ping_base_url`, `ping_realm` | Your tenant's URL and realm |
| `ping_*_secret` | The four secret IDs from step 3 |
| `create_validation_instance` | Keep `false`. That instance exists only for Google Cloud Marketplace, and its image isn't available outside it |

The other settings in `variables.tf` have working defaults. One worth
knowing: `min_instances = 1` keeps one instance running, at a small always-on
cost, so the first request after a quiet period doesn't wait for a cold
start. Set `0` to let it scale to zero. Ignore `marketplace_test.tfvars`; only
Marketplace validation uses it.

## Step 5: Deploy with Terraform

```bash
terraform init
terraform plan
terraform apply
```

1. `terraform plan` should report **9 to add**: the runtime service account,
   its five role grants, the Agent Engine instance, and two waits for the
   grants to take effect.
2. `terraform apply` takes about 10–15 minutes, most of it waiting for Agent
   Engine. It ends with `Apply complete! Resources: 9 added` and prints the
   agent's ID and service account.
3. Run `terraform plan` once more. It should report **No changes**.

Keep the folder: Terraform keeps its state in `terraform.tfstate` there, and
you need it to update or remove the deployment. To keep the state elsewhere,
configure a remote backend (such as a Cloud Storage bucket) before
`terraform init`.

If Terraform fails with `invalid_grant` or
`reauth related error (invalid_rapt)`, your organization's session policy has
expired the sign-in. Run `gcloud auth application-default login` again and
repeat the command.

## Step 6: Export the agent card

The agent card tells Gemini Enterprise where the agent is and how to call it.
Save it to `agent-card.json` in the package folder:

```bash
ENGINE="$(terraform output -raw agent_engine_id)"
curl -s --fail-with-body -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://${REGION}-aiplatform.googleapis.com/v1beta1/${ENGINE}/a2a/v1/card" \
  | jq . > agent-card.json
jq -r '.name, .url' agent-card.json
```

The last command prints `PingOne AIC Administrator Agent` and an address
ending in `/a2a`. A 403 means your account lacks Vertex AI User on the agent
project.

## Step 7: Create an OAuth client for Gemini Enterprise

The agent runs at a Vertex AI API address, and Google rejects any request to
it that carries no Google access token, before the agent sees it. Gemini
Enterprise gets each operator's token through an OAuth client you create, and
each operator consents once.

In the Google Cloud console, in the **Gemini Enterprise project**:

1. Open **APIs & Services → OAuth consent screen** (Google Auth Platform). If
   the project has none yet, create one with user type **Internal**, an app
   name (for example *Gemini Enterprise PingOne AIC Admin Agent*) and a
   support email. Under **Data Access**, add the scope
   `https://www.googleapis.com/auth/cloud-platform`.
2. Open **APIs & Services → Credentials → Create credentials → OAuth client
   ID**.
3. Application type: **Web application**. Name: for example *Gemini
   Enterprise PingOne AIC Admin Agent*.
4. Under **Authorized redirect URIs**, add both:
   - `https://vertexaisearch.cloud.google.com/oauth-redirect`
   - `https://vertexaisearch.cloud.google.com/static/oauth/oauth.html`
5. Click **Create**, then copy the **client ID** and **client secret**. The
   secret is shown only once; if you lose it, add a new secret to the client.

Use a new client for each agent registration. A newly created client can take
a few minutes to start working. Because the consent screen is **Internal**,
operators must sign in with an account in your organization.

## Step 8: Create the OAuth authorization

The authorization stores the OAuth client in Gemini Enterprise. Each agent
registration needs its own authorization: one already attached to another
agent is rejected with *is used by another agent*.

1. Set the client ID, and enter the secret at a prompt so it isn't shown or
   kept in shell history:

   ```bash
   AUTH_ID="pingaic-admin-agent-invoke"     # unique per agent registration
   OAUTH_CLIENT_ID="<client ID from step 7>"
   read -rs "OAUTH_CLIENT_SECRET?Client secret: "; echo   # zsh; in bash: read -rsp 'Client secret: ' OAUTH_CLIENT_SECRET; echo
   export OAUTH_CLIENT_SECRET
   ```

2. Optional: check the client ID and secret with Google before you use them.
   This exchanges a dummy code, so it grants nothing:

   ```bash
   printf '%s' "$OAUTH_CLIENT_SECRET" | curl -s https://oauth2.googleapis.com/token \
     --data-urlencode "client_id=$OAUTH_CLIENT_ID" \
     --data-urlencode "client_secret@-" \
     --data-urlencode "code=check" \
     --data-urlencode "grant_type=authorization_code" \
     --data-urlencode "redirect_uri=https://vertexaisearch.cloud.google.com/oauth-redirect" | jq -r .error
   ```

   `invalid_grant` means the ID and secret are right (only the dummy code was
   refused). `invalid_client` means a wrong secret, or a new client that isn't
   active yet: wait a few minutes and try again.

3. Create the authorization:

   ```bash
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

## Step 9: Register the agent in Gemini Enterprise

Register the agent from its card, with the authorization from step 8
attached:

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

To register in the console instead, skip the command in step 8, because the
console creates the authorization:

1. Open the app, then **Agents → Add agents → Custom agent via A2A**.
2. Paste the contents of `agent-card.json`, then click **Preview agent
   details → Next**.
3. On the authorization step, enter the client ID and secret from step 7, the
   authorization URL from step 8 (with your client ID in it), and the token
   URL `https://oauth2.googleapis.com/token`. Don't click **Skip & Finish**.

Register it as **Custom agent via A2A**, not as an ADK agent: the agent serves
only A2A, so an ADK registration fails.

## Step 10: Give operators access

Grant access to the operator group only, with a role that can call the agent
but not change it. [Access controls](#access-controls) explains why.

1. **Create the operator role** in the agent project. It holds only the two
   permissions needed to call the agent:

   ```bash
   gcloud iam roles create pingaicAgentOperator --project="$PROJECT_ID" \
     --title="PingOne AIC Agent Operator" \
     --description="Call the PingOne AIC Administrator Agent without changing it" \
     --permissions="aiplatform.reasoningEngines.get,aiplatform.reasoningEngines.query" \
     --stage=GA
   ```

2. **Grant the role to the operator group.** Gemini Enterprise calls the agent
   with each operator's own token, so this is checked on every request:

   ```bash
   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="group:idp-admins@example.com" \
     --role="projects/$PROJECT_ID/roles/pingaicAgentOperator" --condition=None
   ```

3. **Grant the group Agent User in Gemini Enterprise.** In the Google Cloud
   console, open **Gemini Enterprise → Apps →** your app **→ Agents →
   PingOne AIC Administrator Agent → User permissions**. Add the group with
   **Agent User**, remove anyone else, and don't choose **All users**.

Replace `idp-admins@example.com` with your operator group. A new grant takes a
few minutes to take effect. Don't grant operators Vertex AI User: it also lets
them change and delete the agent.

## Step 11: Authorize and run a first search

Test first with an operator who holds only the step 10 grants. A project
administrator's wider roles would hide a missing grant.

Each operator does this once:

1. Open the Gemini Enterprise app, choose **PingOne AIC Administrator
   Agent**, and ask it to find a test user, for example `find jsmith`.
2. The first reply is *The agent requires additional authorization for:
   pingaic-admin-agent-invoke* (your `AUTH_ID`). This is expected. Click
   **Authorize**, choose your organization account, and click **Allow**.
3. Ask again. The administration panel opens beside the chat with the search
   results.

Then check a change end to end: open the test user's profile, request a
change, **reject** it in the review, and confirm the tenant is unchanged.
Every change needs the operator's explicit approval; an approval expires after
10 minutes and works once.

## Updating and removing the deployment

**To change a setting,** edit `terraform.tfvars` and run `terraform apply`
again in the package folder. If a change replaces the Agent Engine instance,
it gets a new ID: export the card again (step 6) and update the
registration's card in Gemini Enterprise.

**To upgrade to a new package version,** unzip it over the same folder, so
that `terraform.tfvars` and `terraform.tfstate` stay. Then run
`terraform init -upgrade` and `terraform apply`. The apply updates the agent
in place, in about 3 minutes, and keeps its ID and registration.

**To remove everything:**

1. Delete the registration: in the Gemini Enterprise app, open **Agents** and
   delete **PingOne AIC Administrator Agent**.
2. Delete the authorization:

   ```bash
   curl -s --fail-with-body --request DELETE \
     --header "Authorization: Bearer $(gcloud auth print-access-token)" \
     --header "X-Goog-User-Project: $GE_PROJECT_ID" \
     "https://$GE_API_HOST/v1alpha/projects/$GE_PROJECT_NUMBER/locations/$GE_LOCATION/authorizations/$AUTH_ID"
   ```

3. In the package folder, run `terraform destroy`. It deletes the Agent
   Engine instance with its chat sessions, and the runtime service account
   with its grants.
4. Optionally, delete the OAuth client (step 7), the four secrets (step 3) and
   the operator role (step 10).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| *The agent requires additional authorization for: …* | Expected on each operator's first use. Click **Authorize** (step 11). |
| *Error during authorization* after you click **Allow** | Gemini Enterprise couldn't exchange the consent for a token. Check that the OAuth client lists both redirect URIs (step 7), and run the check in step 8. A new client can take a few minutes to start working. If it persists, create a new OAuth client and a new authorization, then attach it to the registration with `PATCH …/agents/AGENT_ID?updateMask=authorizationConfig` and the body `{"authorizationConfig":{"agentAuthorization":"…/authorizations/NEW_ID"}}`. |
| *Access blocked: … can only be used within its organization* | You chose an account outside the organization that owns the Internal consent screen. Choose your organization account. |
| *… is used by another agent* when registering | The authorization belongs to another registration. Create a new one with a different `AUTH_ID` (step 8). |
| *This view expired or belongs to another conversation. Open a fresh view.* | The panel's controls are older than 30 minutes, or the agent's instance restarted since the panel opened (for example after an upgrade). Send a new text request or start a new conversation. Package versions before 1.0.4-6 showed this as *Agent returned an empty stream*. |
| 401 from the agent | The registration has no authorization attached, or the operator hasn't completed consent. |
| 403 from the agent or the card export | An operator lacks the operator role from step 10, or a new grant hasn't taken effect yet. For the card export (step 6), your own account lacks Vertex AI User on the agent project. |
| Terraform fails with `invalid_grant` or `invalid_rapt` | The Google sign-in expired. Run `gcloud auth application-default login` and repeat the command. |
| `terraform apply` fails reading the image `pingone-aic-administrator-agent-image…` | Set `create_validation_instance = false` (step 4). |
| `terraform apply` reports that the service account already exists | Another service account uses the name `<prefix>-agent`. Choose another `goog_cm_deployment_name` (step 4). |
| The agent can't reach the tenant | Check the four secret IDs in `terraform.tfvars`, that the secrets hold the tenant service account's real credentials, and that the service account has the scopes the agent uses. |

## Deploying from Marketplace

From the Google Cloud Marketplace listing, do the shell variables in step 1
and steps 2–3, then open the listing (Ping provides the direct link), click
**Deploy**, and enter your tenant URL, realm and the four secret IDs on the
deployment page. The deployment enables the APIs and creates the validation
instance for you. Its `next_steps` output prints the card export command for
step 6; continue with steps 6–11. [Access controls](#access-controls) applies
to Marketplace deployments too.

## Support

Contact [Ping Identity Support](https://support.pingidentity.com/s/). The
agent's listing is on the
[Ping Identity Marketplace](https://marketplace.pingone.com/item/google-gemini-enterprise-pingone-aic-administrator-agent).
When you open a case, include the package version, the step you were on, and
the exact error message.
