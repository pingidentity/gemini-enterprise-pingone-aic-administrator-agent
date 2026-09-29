# Google Cloud Marketplace listings: PingOne AIC Administrator Agent

Two Producer Portal products are required (per the Google partner deck,
"Step by step (current) approach for customer tenant deployable agents"):

## 1. AAAS listing: public, handles transactions

**Product type:** AI Agent as a Service (A2A). Publicly searchable; the
listing customers buy through, including private offers.

- **Agent Card:** `aaas-listing/agent-card.json` (A2A 1.0 spelling; generated
  by `scripts/marketplace_card.py`). Hosted at
  `gs://tech-partner-ping-public-branding/marketplace/agent-card.json` :
  bucket has Object Versioning, same project as the product.
- **Product details:** name/tagline/categories from `aaas-listing/product-details.md`.
- **Pricing:** `aaas-listing/pricing.md` (subscription model; plans in the
  Pricing section of Producer Portal; submit early; it takes up to 4 business
  days and must pass before the Technical integration review).
- **Technical integration:** entitlement handling (account + entitlement
  approvals), Agent Card Save and validate, Technical integration review.

## 2. VM listing: hidden, carries the deployment package

**Product type:** Virtual machine. NOT publicly searchable: after the product
exists, Product details → Product metadata → turn **Product searchable** OFF
(the listing is reachable only by direct URL; Google reviews each such
request). This is the listing whose Deployment Package is the Terraform zip
that deploys Agent Engine into the customer's project.

- **Deployment package:** `vm-listing/pingone-aic-administrator-agent.zip`
  (source tar built by `vm-listing/package_agent.py`; rebuild with
  `python3 package_agent.py --source <agent source> --output assets/source.tar.gz`,
  then re-zip the listed files).
- **Upload:** GCS bucket in the Marketplace product's project, selected in
  Producer Portal → Deployment package (manual configuration, custom UI
  deployment, `source_image` variable, the 7 required roles).
- **Upload convention: versioned object names, never overwrites.** Google's
  anonymous test harness fetches the package through a shared cache keyed by
  object path: an overwrite of an existing object keeps serving the previous
  generation anonymously while the authenticated view shows the new one
  (verified 2026-09-28: the tester validated a stale build twice for this
  reason). Ship every package update under a fresh object name and point the
  portal at it:

  ```bash
  gcloud storage cp vm-listing/pingone-aic-administrator-agent.zip \
    "gs://gemini-enterprise-agent-pingone-aic-administrator/deployment/pingone-aic-administrator-agent-v<VERSION>-<N>.zip"
  gcloud storage objects update \
    "gs://gemini-enterprise-agent-pingone-aic-administrator/deployment/pingone-aic-administrator-agent-v<VERSION>-<N>.zip" \
    --add-acl-grant=entity=AllUsers,role=READER
  ```

  The public-read ACL is required on every new object (fresh generations are
  born private; the tester's fetch returns 403 without it) and ACL grants are
  exempt from the org policy that blocks IAM bindings for Google-side
  principals. Then update the package path in Producer Portal → Deployment
  package and re-validate. Current object:
  `deployment/pingone-aic-administrator-agent-v1.0.4-3.zip`.
- **Validation:** requires the VM in the package (it is there, idle, not part
  of the serving path). Validate metadata with Google's CFT CLI before every
  upload (`go install github.com/GoogleCloudPlatform/cloud-foundation-toolkit/cli@latest`,
  then `cft blueprint metadata -p vm-listing -v`) and confirm the anonymous
  fetch of the exact object returns the new bytes, not the portal's cache.
- **Getting-started doc:** `vm-listing/README.md`: host a Google-specific
  copy on a Ping URL and email the draft to the Partner Engineer.

## 3. The commercial link between the two

Issue the **private offer from the AAAS listing**; after acceptance, hand the
customer the hidden VM listing's URL for deployment into their tenant.
Entitlements are product-specific: the two products are coordinated by Ping
(order records / onboarding), not merged by Marketplace. Confirm the exact
two-product flow with the Google Partner Engineer before first private offer.

## Producer Portal checklist

1. AAAS: product details + pricing → submit (any order; pricing first).
2. AAAS: Agent Card → select the GCS object → Save and validate.
3. AAAS: Technical integration → submit after pricing approval.
4. VM: product details + pricing + deployment package → submit.
5. VM: after approval, set Product searchable = off.
6. Issue private offer from the AAAS listing; include the VM listing URL in
   onboarding.
