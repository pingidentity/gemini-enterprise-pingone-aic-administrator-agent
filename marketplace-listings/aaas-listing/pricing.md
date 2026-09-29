# AAAS listing: Pricing (Producer Portal: Pricing section)

**Model:** Subscription-based (flat monthly fee, priced per operator seat).
Partial months prorated; monthly auto-renew.

> Business decision: placeholders below. Ping's Marketplace commercial team
> owns the final numbers; the Pricing review (up to 4 business days) must
> pass before the Technical integration review, so submit early.

## Plans (draft, ≤ 24 allowed)

| Plan ID | Name | Billing | Unit | Price |
| --- | --- | --- | --- | --- |
| `admin-agent-standard` | PingOne AIC Administrator Agent, Standard | Monthly, auto-renew | per operator seat | $49 / seat / month |
| `admin-agent-trial` | Free trial add-on | n/a | n/a | 14 days, $0 credit (optional free trial on the paid plan) |

## Notes

- The agent itself runs in the **customer's project** (their Agent Engine,
  their Gemini model spend); the subscription covers the software, updates,
  and support, not infrastructure.
- No usage-based metric is required at launch (the agent does not need to
  report usage to Google for a subscription model).
- Private offers are issued against this listing per the two-listing flow;
  see `../README.md` step 3.
- Free trial prerequisites (signed vendor agreement, Solution Validation
  form, Project Info Form, approved pricing review) apply if the trial plan
  is configured.
