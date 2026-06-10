# TenantSentry.ai — Data Security & Privacy Policy

## Summary

TenantSentry.ai is an AI-powered commercial lease auditing platform built for Australian tenants and tenant advisors. This document describes how we handle lease documents and personal data.

---

## 1. Data Hosting — Australia Only

All data (lease PDFs, audit results, user accounts) is stored exclusively in **Australian data centres**.

| Component | Provider | Region |
|---|---|---|
| Database & file storage | Supabase | AWS ap-southeast-2 (Sydney) |
| Application server | Self-hosted / AWS | AWS ap-southeast-2 (Sydney) |
| AI inference | Anthropic Claude API | Requests transited to Anthropic; no data retained by Anthropic |

**No lease document or personal data leaves Australia.**

---

## 2. Tenant Data Isolation

Each tenant organisation's data is **logically isolated** using Supabase Row-Level Security (RLS) policies scoped to `organisation_id`.

- No tenant can query, view, or download another tenant's lease documents or audit results.
- Admin access is restricted to TenantSentry.ai staff via RBAC roles and requires explicit `ADMIN_TOKEN` authorisation.
- Channel partner accounts (F23 white-label) can only access clients they have explicitly been granted access to.

---

## 3. No AI Training

Your lease documents and audit results are **never used to train any AI model**.

- Documents are passed to Anthropic's Claude API for analysis and are **not retained** by Anthropic for training purposes. See [Anthropic's Privacy Policy](https://www.anthropic.com/legal/privacy).
- TenantSentry.ai does not use your data to fine-tune, retrain, or improve any model.
- Only fully anonymised, aggregated statistics (clause type frequencies, risk score distributions with no PII) are used for internal product improvement.

---

## 4. Data Retention

| Data type | Retention period |
|---|---|
| Uploaded lease PDFs | Deleted 90 days after audit completion, or immediately on written request |
| Audit results (clause analyses, risk flags) | Retained for active subscription + 12 months post-cancellation |
| User account data | Deleted within 30 days of account closure |
| Anonymised aggregate statistics | Retained indefinitely (no PII) |

**Right to deletion:** You may request deletion of all your data at any time by emailing security@tenantsentry.ai.

---

## 5. Encryption

- **In transit:** TLS 1.3 for all API and web traffic (HTTPS enforced; HTTP redirects to HTTPS).
- **At rest:** AES-256 encryption for all stored documents and database fields (Supabase default encryption).
- **API keys:** All third-party API keys (Anthropic, VoyageAI) are stored as environment variables; never committed to source control or exposed in API responses.

---

## 6. Access Controls

- Production database credentials are never shared with development environments.
- All admin API routes require `Authorization: Bearer <token>` or an equivalent httpOnly session cookie.
- Partner portal routes require separate `partner_token` credentials.
- Source code access is restricted to TenantSentry.ai engineering staff.

---

## 7. Compliance

| Obligation | Status |
|---|---|
| Australian Privacy Act 1988 (Cth) — Australian Privacy Principles | ✅ Compliant |
| Notifiable Data Breaches scheme | ✅ Mandatory notification within 30 days of discovery |
| GDPR | Not applicable (AU-only service and data residency) |

---

## 8. Incident Response

In the event of a suspected data breach:

1. Affected tenants are notified within **72 hours** of confirmed discovery.
2. The Office of the Australian Information Commissioner (OAIC) is notified if required under the NDB scheme.
3. A post-incident report is provided to affected tenants within 30 days.

---

## 9. Contact

| | |
|---|---|
| Security issues | security@tenantsentry.ai |
| Privacy enquiries | privacy@tenantsentry.ai |
| Privacy Policy (full) | https://tenantsentry.ai/privacy |
| Security API endpoint | `GET /api/security` |

---

*Last updated: 10 June 2026. This policy is reviewed quarterly.*
