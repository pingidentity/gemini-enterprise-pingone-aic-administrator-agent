# Manual Gemini Enterprise registration

This bundle describes an already deployed customer Agent Runtime. The runtime
resource and A2A endpoint are in `runtime.json`; `agent-card.json` was fetched
from its authenticated discovery endpoint. Registration does not deploy code.
The card's `securitySchemes` entry declares that every request needs a Google
OAuth2 access token with the `cloud-platform` scope. Agent Runtime rejects any
other request with 401 before the agent runs, even from the same project. On
the registration path below, that token is each operator's own, obtained through
a Google OAuth authorization, so the operator group must hold runtime IAM.

## Register and grant access

Traffic on this path does not go through Agent Gateway; no Agent Registry or
gateway setup is needed. Set the shell variables once for every command below:

```bash
GE_PROJECT_ID="your-ge-project"
GE_PROJECT_NUMBER="123456789012"
GE_LOCATION="global"          # the app's location: global, us, or eu
GE_APP_ID="your-app"
case "$GE_LOCATION" in
  global) GE_API_HOST="discoveryengine.googleapis.com" ;;
  us|eu) GE_API_HOST="${GE_LOCATION}-discoveryengine.googleapis.com" ;;
esac
```

1. Create a Google OAuth client in the Gemini Enterprise project: an *Internal*
   consent screen with the scope `https://www.googleapis.com/auth/cloud-platform`,
   and a *Web application* client whose authorized redirect URI is exactly
   `https://vertexaisearch.cloud.google.com/oauth-redirect`. Export the secret as
   `OAUTH_CLIENT_SECRET` in the shell; never write it to a file or a log.
2. Create the authorization resource. Every query parameter matters; a missing
   `access_type=offline` or `prompt=consent` makes the consent prompt repeat.

```bash
AUTH_URI="https://accounts.google.com/o/oauth2/v2/auth?client_id=OAUTH_CLIENT_ID&redirect_uri=https://vertexaisearch.cloud.google.com/oauth-redirect&scope=https://www.googleapis.com/auth/cloud-platform&include_granted_scopes=true&response_type=code&access_type=offline&prompt=consent"
curl --fail-with-body --request POST \
  --header "Authorization: Bearer $(gcloud auth print-access-token)" \
  --header "Content-Type: application/json" \
  --header "X-Goog-User-Project: ${GE_PROJECT_ID}" \
  "https://${GE_API_HOST}/v1alpha/projects/${GE_PROJECT_NUMBER}/locations/${GE_LOCATION}/authorizations?authorizationId=pingaic-runtime-invoke" \
  --data-binary "{\"name\":\"projects/${GE_PROJECT_NUMBER}/locations/${GE_LOCATION}/authorizations/pingaic-runtime-invoke\",\"serverSideOauth2\":{\"clientId\":\"OAUTH_CLIENT_ID\",\"clientSecret\":\"${OAUTH_CLIENT_SECRET}\",\"authorizationUri\":\"${AUTH_URI}\",\"tokenUri\":\"https://oauth2.googleapis.com/token\"}}"
```

3. Register the agent as a **Custom agent via A2A** with the live card and the
   authorization attached. Use `agentAuthorization`, which places the operator's
   token in the request header; `toolAuthorizations` would not.

```bash
CARD="$(jq -c . agent-card.json)"
jq -n --arg card "$CARD" \
  --arg auth "projects/${GE_PROJECT_NUMBER}/locations/${GE_LOCATION}/authorizations/pingaic-runtime-invoke" \
  '{displayName:"PingOne AIC Administrator Agent",description:"Provides user search, password reset, session/MFA management, group/role membership, direct assignments, and audit lookup.",a2aAgentDefinition:{jsonAgentCard:$card},authorizationConfig:{agentAuthorization:$auth}}' > agent.json
curl --fail-with-body --request POST \
  --header "Authorization: Bearer $(gcloud auth print-access-token)" \
  --header "Content-Type: application/json" \
  --header "X-Goog-User-Project: ${GE_PROJECT_ID}" \
  --data-binary @agent.json \
  "https://${GE_API_HOST}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/collections/default_collection/engines/${GE_APP_ID}/assistants/default_assistant/agents"
```

   To attach the authorization to an existing registration instead, send the
   same body with `PATCH .../agents/AGENT_ID?updateMask=a2aAgentDefinition,authorizationConfig`.
   The console equivalent is **Agents → Add agent → Custom agent via A2A**,
   pasting `agent-card.json` and selecting the authorization instead of
   **Skip & Finish**.
4. Grant the **IdP Admins** group Vertex AI User (`roles/aiplatform.user`) on
   the runtime project, and grant **Agent User** only to **IdP Admins** in the
   agent's User permissions. Each operator sees one Google consent prompt the
   first time and then uses the agent without further prompts.

Do not register the runtime as an ADK agent (it serves only A2A routes and
returns 400), and do not omit the authorization (Gemini Enterprise then sends no
token and the runtime returns 401).

## Set the Ping Identity registration icon

The A2A card contains the official logo URL in `iconUrl`. Gemini Enterprise also
has its own registration `icon` field; set it explicitly after import; without
it the registration shows the default initial-letter avatar instead of the Ping
Identity mark. Apply the included `gemini-enterprise-icon.json`, which sets
`icon.uri` to the card's `iconUrl`, the public HTTPS copy of the official
square mark the PingID Device Management agent shows as its small circular
avatar. Verified live 2026-09-25: Gemini Enterprise renders the avatar from
`icon.uri`; an icon uploaded as inline base64 `content` is stored but the UI
still shows the initial letter, so use the `uri` payload.

Run the following from the extracted bundle directory. Replace the example
resource name with the **existing Gemini Enterprise agent registration name**,
not the Agent Runtime reasoning engine name. Select the endpoint for the app's
location (`global`, `us`, or `eu`).

```bash
GE_AGENT_RESOURCE="projects/YOUR_PROJECT/locations/global/collections/default_collection/engines/YOUR_APP/assistants/default_assistant/agents/YOUR_AGENT"
GE_LOCATION="$(printf '%s' "$GE_AGENT_RESOURCE" | cut -d/ -f4)"
case "$GE_LOCATION" in
  global) GE_API_HOST="discoveryengine.googleapis.com" ;;
  us|eu) GE_API_HOST="${GE_LOCATION}-discoveryengine.googleapis.com" ;;
  *) printf '%s\n' "Expected a global, us, or eu Gemini Enterprise registration" >&2; exit 1 ;;
esac
curl --fail-with-body --request PATCH \
  --header "Authorization: Bearer $(gcloud auth print-access-token)" \
  --header "Content-Type: application/json" \
  --data-binary @gemini-enterprise-icon.json \
  "https://${GE_API_HOST}/v1alpha/${GE_AGENT_RESOURCE}?updateMask=icon"
```

This is an administrator's management API call and needs
`discoveryengine.agents.update`. The patch changes only `icon`; it does not change
registration routing, permissions, or authentication. See Google's
[Agent image fields](https://docs.cloud.google.com/gemini/enterprise/docs/reference/rest/v1alpha/projects.locations.collections.engines.assistants.agents#Image)
and [update method](https://docs.cloud.google.com/gemini/enterprise/docs/reference/rest/v1alpha/projects.locations.collections.engines.assistants.agents/patch).

## Verify branding and access

- Check the logo beside the registered agent in Gemini Enterprise.
- Search for a known test user and check the Ping Identity logo above the A2UI
  results. A2A/A2UI clients must be able to load the HTTPS asset in `iconUrl`.
- Confirm an IdP Admins member can use the agent after the one-time Google
  consent, and a user outside the group is denied.
- UI controls, confirmations and A2A tasks are temporary and require no database.
  After a runtime restart, redeployment or scale-to-zero, open a fresh workspace
  and request the change again. Old controls and approvals cannot be resumed.
- Open the test user's profile, inspect relationships/activity, and verify a
  form and its review. Reject a proposed change and check that AIC is unchanged.
  Approvals expire after 10 minutes and can be used once. Verify an uncertain
  write in AIC before requesting a fresh change.
- The A2A `iconUrl` and the panel header default to Ping Identity's square
  mark, loaded from pingidentity.com. The bundled files are the horizontal logo
  for the registration icon.
- The original SVG, PNG rendering, provenance, and checksums are included in
  `assets/` and `branding.json`. If customer policy requires hosting a copy, serve
  the official SVG from a public HTTPS asset URL, set `PING_ADMIN_LOGO_URL` in
  the deployment environment, update the runtime, and export a fresh bundle.

The bundle generator makes no Gemini Enterprise API calls. Live icon rendering,
the consent flow, and access enforcement require customer validation.
