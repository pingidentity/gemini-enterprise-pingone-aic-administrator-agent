# Plan: PingAIC Admin / Help Desk Agent

A customer-deployed Gemini/ADK agent for PingOne Advanced Identity Cloud
help-desk operations. The ADK agent runs inside an A2A service on the customer's
Agent Runtime and is registered manually in an existing Gemini Enterprise app
through direct A2A registration with a Google OAuth authorization; no Agent
Registry or Agent Gateway. A2UI v0.9 renders an interactive administration workspace.

- **Scope:** user account operations, group/role membership, app assignments,
  direct entitlements, and audit troubleshooting.
- **Model:** Gemini 3.5 Flash by default, with model and endpoint overrides in
  `PING_ADMIN_MODEL` and `PING_ADMIN_MODEL_LOCATION`. Model inference defaults to
  `global`; deployment and managed Sessions retain the customer's runtime region.
- **PingAIC authentication:** one shared privileged service identity. The runtime
  reads its ID/private JWK and separate audit key/secret from four Secret Manager
  resources. Only resource names are included in deployment configuration.
- **Operator access:** Gemini Enterprise grants **Agent User** to the customer's
  **IdP Admins** group; each operator completes one Google consent so Gemini
  Enterprise can call the runtime with that operator's token.
- **Accepted dependency:** Gemini Enterprise sign-in may depend on the IdP being
  administered. This is accepted for the current rollout; an independent
  emergency sign-in path is deferred.
- **Google authentication:** Gemini Enterprise invokes the runtime with each
  operator's Google OAuth2 access token (`cloud-platform` scope) obtained through
  the registered authorization, so the IdP Admins group needs runtime invocation
  IAM. This is separate from the runtime's PingAIC identity. Agent Registry and
  Agent Gateway are not used.
- **Customer ownership:** customers select their project, region, staging bucket,
  runtime service account, tenant, secrets and Gemini Enterprise permissions.
- **Branding:** the official Ping Identity logo appears in A2A `iconUrl`, the
  Gemini Enterprise registration icon, and the A2UI workspace header. Every
  manual registration bundle includes the original SVG, upload PNG, provenance,
  checksums, and an icon-only Gemini Enterprise update payload.

---

## 1. Scope: what the agent can do

Tool areas include
separate native OIDC and observed-shape SAML application creation tools.

### User account ops
| Ask | Tool | PingAIC endpoint |
| --- | --- | --- |
| Find a user by name/email/username | `search_users` | `GET /openidm/managed/alpha_user?_queryFilter=...` |
| View a user's profile & status | `get_user` | `GET /openidm/managed/alpha_user/{id}` |
| Reset a user's password | `reset_user_password` | `PATCH /openidm/managed/alpha_user/{id}` (`/password`) |
| Force logout / kill all sessions | `logout_user_sessions` | `POST /am/json/realms/root/realms/alpha/sessions/?_action=logoutByUser` with exact IDM user UUID |
| List active sessions | `list_user_sessions` | `GET /am/json/realms/root/realms/alpha/sessions?_queryFilter=username eq "<UUID>" and realm eq "/alpha"`; session handles are dropped |
| Enable / disable a user | `set_user_status` | `PATCH /openidm/managed/alpha_user/{id}` (`/accountStatus`) |
| Reset / unenroll MFA | `unenroll_mfa_device` | `DELETE /am/json/realms/root/realms/alpha/users/{id}/devices/2fa/{type}/{uuid}` |

### Group & role membership
| Ask | Tool | Endpoint |
| --- | --- | --- |
| List groups for a user | `list_user_groups` | `GET /openidm/managed/alpha_user/{id}/authzRoles` / `groups` |
| List members of a group | `list_group_members` | `GET /openidm/managed/alpha_group/{id}/members` |
| Add user to group/role | `change_user_membership` | `PATCH /openidm/managed/alpha_user/{id}` (add a `_ref` to `/groups/-`, or `internal/role/{id}` to `/authzRoles/-`) |
| Remove user from group/role | `change_user_membership` | `DELETE /openidm/managed/alpha_user/{id}/{groups or authzRoles}/{relationship _id}`; a PATCH remove would have to repeat the entire stored object |

### App assignments & entitlements
| Ask | Tool | Endpoint |
| --- | --- | --- |
| List a user's direct assignments | `list_user_assignments` | `GET /openidm/managed/alpha_user/{id}/assignments` |
| Grant a direct assignment | `change_user_assignment` | `PATCH /openidm/managed/alpha_user/{id}` (add to `/assignments`) |
| Revoke a direct assignment | `change_user_assignment` | `PATCH /openidm/managed/alpha_user/{id}` (remove from `/assignments`) |
| Look up an application | `find_application` | `GET /openidm/managed/alpha_application?_queryFilter=...` |

### Application creation
| Ask | Tool | Endpoint |
| --- | --- | --- |
| Create OIDC client | `create_oidc_application` | `PUT /am/json/realms/root/realms/{realm}/realm-config/agents/OAuth2Client/{clientId}` |
| Create observed SAML application record | `create_saml_application` | `POST /openidm/managed/alpha_application` with `Accept-API-Version: resource=1.0` |

### Audit / troubleshoot
| Ask | Tool | PingAIC endpoint |
| --- | --- | --- |
| Recent logins/failures for a user | `get_user_activity` | `GET /monitoring/logs?source=am-authentication` |
| Recent activity for a user (any event) | `get_user_activity` | `GET /monitoring/logs?source=am-access` (filtered) |
| Follow a user's activity live | `get_user_activity` with `live=True` | `GET /monitoring/logs/tail` (filtered), continued with `_pagedResultsCookie` |

15 tools total, including separate native OIDC client creation and the
confirmation-gated observed-shape SAML managed-application record creation.
User lookup uses case-insensitive substring matching across common identity fields,
with additional support for two-part `First Last` and explicit `Last, First`
name searches; multiple matches remain ambiguous and require operator selection.
The SAML tool uses `POST /openidm/managed/alpha_application` with
`Accept-API-Version: resource=1.0`, locks v1 to explicit
`templateName="saml"`, `templateVersion="1.1.0"`, `ssoEntities`, and
`appTypeSpecificConfig` inputs, and supplies no defaults or inferred SAML
semantics. Observed-only response fields `ownerId` and `appNameCap` are
excluded from the public create contract until their writability is justified.
It redacts secret-shaped response/error fields and is validated with
mocked HTTP only; live endpoint acceptance and functional SAML behavior remain
unverified pending a separately authorized non-production contract test. It
intentionally excludes hosted-IdP creation, `FR_COT` association, remote-SP
metadata import, certificate/private-key setup, and read-back. Every write tool
follows the existing agent's pattern: validate inputs at the tool layer, refuse
without a required confirmation phrase, return
`{status, data | message}` dicts. Session logout keeps a login username at the
tool boundary, resolves it after confirmation to exactly one IDM `_id` UUID, and
uses the documented AM `logoutByUser` request; zero, multiple, or missing-ID
matches are refused without a broad/per-session fallback.

**Notably out of scope for v1**: creating brand-new users, deleting users,
password history retrieval, bulk operations, journey/tree management. If any of
these should be in v1, flag them now.

---

## 2. Auth flow: service identity + Secret Manager

### On the PingAIC side
- Create one PingOne AIC service account and register its public RSA JWK.
- Keep the private JWK secret and grant the service account only the required
  scopes (`fr:idm:*`, `fr:am:*`, and optional `fr:iga:*`) and admin groups.
- Use the documented JWT bearer grant, not `private_key_jwt`, client credentials,
  or `client_secret_basic`.
- POST to `https://<tenant-env-fqdn>:443/am/oauth2/access_token` with
  `client_id=service-account`, the JWT bearer `grant_type`, signed assertion,
  and space-separated scopes. Assertion claims are `iss=sub=service-account-id`,
  exact token URL `aud`, `exp=now+899`, and unique `jti`; header is RS256 and
  includes optional `kid`.

Every call is attributed to the PingAIC service account; human attribution
lives in Gemini Enterprise access logs.

### On the GCP side
- Keep the runtime GCP service account (Agent Runtime) distinct from the
  PingAIC service-account ID.
- Store `pingaic-admin-service-account-id` and `pingaic-admin-private-jwk` in
  Secret Manager.
- Grant the runtime GCP service account `roles/secretmanager.secretAccessor`
  only on the four credential secrets, including the monitoring pair below.
- Deploy with `--service-account` when a dedicated runtime SA is desired.

### At startup
`config.py` accepts either both local-only plaintext values
`PING_ADMIN_SERVICE_ACCOUNT_ID` / `PING_ADMIN_PRIVATE_JWK`, or both deployed
Secret Manager refs `PING_ADMIN_SERVICE_ACCOUNT_ID_SECRET` /
`PING_ADMIN_PRIVATE_JWK_SECRET`. It rejects partial or mixed configurations,
loads refs with ADC, validates RSA private material, and redacts secret values
from errors and config repr/equality.

Monitoring uses a separate API credential pair: deployed runtimes receive only
`PING_ADMIN_AUDIT_API_KEY_SECRET` and `PING_ADMIN_AUDIT_API_SECRET_SECRET`
resource names; local-only plaintext equivalents are available for tests. The
`x-api-key` and `x-api-secret` headers are scoped to `/monitoring/logs` and are
never attached to IDM, AM, or token requests. Username filtering is only sent
when `PING_ADMIN_AUDIT_USERNAME_FIELD` is configured with the tenant's
confirmed documented payload path; no field name is inferred.

Then the client creates the fixed RS256 JWT bearer assertion, requests a token
from the tenant-root endpoint, and retains the existing caching, expiry, and
401 handling. For key rotation, register a new public JWK first, publish the
matching private JWK as a new secret version, deploy/restart, verify token
issuance, and retire the old public key only after all old tokens expire.

---

## 3. Deployment components

- `ping_admin_agent/agent.py`: ADK agent and confirmation-gated tools.
- `ping_admin_agent/a2a.py`: native and compatibility cards, extension
  negotiation, ADK executor, and complete A2A responses.
- `ping_admin_agent/runtime.py`: serializable `A2aAgent` subclass for managed
  Agent Runtime, compatibility discovery, and deployed endpoint URLs.
- `deployment/deploy.py`: create/update/list/info/delete, explicit customer
  environment file, Secret Manager reference validation and live-card export.
- `deployment.env.example`: customer-owned project, runtime and tenant settings.
- `scripts/smoke_runtime.py`: read-only search through the deployed A2A streaming
  API, checking completed structured UI output without displaying user records.

## 4. Deployment and registration contract

Follow [the customer deployment guide](README.md#customer-deployment-to-agent-runtime).
The deployer packages `PingAdminA2aAgent`, with pinned SDK dependencies and no
resolved PingAIC credentials in the serialized application. Managed setup creates
the executor, selects Vertex AI model access and managed Sessions, and advertises
the actual runtime URL. A2A 1.0 objects remain internal; Google's SDK implements
the 0.3 HTTP+JSON wire routes used by Gemini Enterprise.

After deployment, the script fetches the live authenticated 0.3 card into
`deployment-output/agent-card.json`. The customer creates a Google OAuth client
and a `cloud-platform` authorization, registers the agent as **Custom agent via
A2A** with that authorization attached, grants the IdP Admins group runtime
invocation IAM, and assigns **Agent User** to **IdP Admins**. The script does not
create the authorization, configure IAM, create an app, or register an agent
automatically. Agent Registry and Agent Gateway are not used on this path.

Each export also produces `manual-registration.zip` and its extracted files.
The customer sets the separate Gemini Enterprise registration icon using the
bundled PNG or `gemini-enterprise-icon.json` with `updateMask=icon`. This management
step changes branding only. A2A/A2UI use the public official logo URL, optionally
overridden with `PING_ADMIN_LOGO_URL` for a customer-hosted copy of the same asset.

Registering the runtime as an ADK agent fails because it serves only A2A
routes, and omitting the authorization yields 401 because Gemini Enterprise then
has no token to send. The live integration remains a customer validation item:
the consent flow, invocation IAM, A2UI rendering, and group access denial.

A2UI negotiates Gemini's composite v0.9 catalog or the Basic fallback. Composite
workspaces are rooted in the `Canvas` component, so Gemini Enterprise shows an
opener card in the chat and renders the workspace in its side panel; the surface
is created once per conversation and refreshed in place afterwards. Structured
tool observations feed deterministic account, relationship, activity, application,
form and review views. Streaming loading surfaces are retired; final responses
contain complete snapshots. UI actions use server-bound targets and validated
fields. Mutating tools require native ADK confirmation plus an exact, single-use
server approval, including when the request originates in chat.

The agent is a front end to PingOne AIC and needs no separate database. UI
controls, confirmations, session references and the SDK's A2A tasks are shared
by the instance's worker processes through a SQLite file on its local disk,
because Agent Runtime routes each request to any worker. Agent Runtime retains
chat history in managed Sessions. The deployer limits the service to one
instance, but a restart or scale-to-zero still discards open views and
confirmations. A fresh request starts a new session and requires a new
confirmation. Confirmations expire after ten
minutes and are consumed before execution; uncertain API outcomes require
inspection in AIC before a fresh request. See README for the lifecycle and
restart behavior.

Native Agent Runtime A2A is Preview. Live deployment and Gemini Enterprise visual
rendering still require the customer's verification; offline tests do not prove
those environment-dependent behaviors.

---

## 5. Testing plan

Offline tests use fake model/tool responses and mocked provider calls. The
runtime integration tests exercise the installed SDK and actual HTTP adapters.

- `tests/test_runtime_a2a.py`: packaging round-trip, deployed card URLs, actual
  REST 0.3 routes, streaming, follow-up turns, concurrent requests,
  per-request extension negotiation, and final structured UI messages.
- `tests/test_admin_ui.py`: views against both vendored catalogs, UI action
  round trips, approvals, replay and tampering protection, and restart behavior
  over the real 0.3 REST routes.
- `tests/test_deployment.py`: settings, secrets, SDK deployment options, customer
  file loading, and authenticated card export.
- `tests/test_auth.py`: RS256 signature, exact claims/header, `exp=now+899`,
  unique `jti`, malformed/public-only JWK rejection, and redaction.
- `tests/test_config.py`: plaintext/ref pair validation, Secret Manager loading,
  missing/mixed values, token URL default/override, and redaction.
- `tests/test_ping_client.py`: exact JWT bearer form fields with `auth is None`,
  token caching & expiry, 401 handling, and API URL construction.
- `tests/test_agent_tools.py`: validation for each write tool, including the
  SAML exact observed shape, strict `confirm=True` gate, registration, and
  error mapping.
- SAML creation tests are mocked-only: no test creates or mutates a real tenant
  application. Live endpoint acceptance and functional SAML behavior remain
  unverified until a separately authorized non-production contract test.

Manual smoke via `python -m scripts.try_api` before Agent Engine deploy:
- `search-users --q "smith"` → returns at least one row
- `get-user <id>` → returns the profile
- `list-user-groups <id>` → returns groups
- destructive operations only in a controlled tenant; SAML creation requires a
  separately authorized live contract test and is not covered by offline tests.

---

## 6. Customer rollout

1. Create the PingAIC service account, register its public JWK, assign scopes and
   groups, and configure the separate monitoring API credentials.
2. Run the offline suite with Python 3.11 and `requirements-dev.txt`.
3. Create the customer's project resources, dedicated runtime service account,
   staging bucket and four secrets with the IAM described in the README.
4. Fill `deployment.env` from `deployment.env.example` and run
   `python -m deployment.deploy --env-file deployment.env --create`.
5. Create the OAuth client and `cloud-platform` authorization, register the A2A
   agent with it attached, grant the IdP Admins group runtime invocation IAM, and
   restrict **Agent User** to **IdP Admins**.
6. Run `python -m scripts.smoke_runtime <resource-name> --search-term smith`
   using a known test user, then verify two rendered searches in Gemini Enterprise.
7. Verify confirmation behavior and authorized test operations before granting
   production access. SAML creation still needs its separate live contract test.

## 7. Customer-specific validation

- Select the target realm and confirm the tenant's managed object names.
- Confirm monitoring payload fields before enabling username filtering.
- Validate the target tenant's MFA device types and SAML creation contract.
- Verify build, deployment, model, Secret Manager, managed Sessions and runtime
  invocation permissions under their respective Google identities; test Gemini
  Enterprise group access separately, including denial outside IdP Admins.
- Rotate signing keys by registering the new public key, deploying the matching
  private-key secret version, verifying token issuance and retiring the old key
  after old tokens expire.
