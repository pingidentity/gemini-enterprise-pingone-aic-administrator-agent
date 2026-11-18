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

- **Deployment package:** `vm-listing/pingone-aic-administrator-agent.zip`,
  built from the current agent source by `vm-listing/build_package.sh`. The
  zip and `assets/source.tar.gz` are build outputs, not committed: build
  them for every upload, and before running the Terraform from a checkout.
  `assets/class_methods.json` stays committed; regenerate it from a working
  SDK deployment when the agent's A2A methods or card change.
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
  vm-listing/build_package.sh
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
  `deployment/pingone-aic-administrator-agent-v1.0.4-5.zip`; the next upload
  is `-v1.0.4-6.zip`. A validated package goes live only when the listing's
  update is published (Overview → Publish).
- **Validation VM image:** validation scans the image for OS vulnerabilities
  and fails on outdated Debian packages. Rebuild it from the latest
  `debian-12` image with `apt-get full-upgrade` applied, as a new versioned
  image (`pingone-aic-administrator-agent-image-vYYYYMMDD`, family
  `pingone-aic-administrator-agent`, storage location `us`) carrying the
  listing's license
  `projects/forgerock-public/global/licenses/cloud-marketplace-5342f74725db910d-df1ebeb69c0ba664`.
  Then point `source_image` in `variables.tf`, `metadata.yaml` and
  `metadata.display.yaml` at it, ship a new package, and select the image
  under Deployment package → Image 1. Current image:
  `pingone-aic-administrator-agent-image-v20261111`.
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
