# DocGen — Document Generation & Workflow Module for COMPASS

## Executive Summary

DocGen is a Django-based document generation and approval workflow module, designed as a first-class integrated component of the COMPASS eOffice platform. It enables staff to create, review, approve, and archive structured documents (letters, memos, office orders, circulars, etc.) using reusable templates, with full PDF export, QR-based traceability, and a visual template builder.

---

## 1. Strategic Fit within COMPASS

```
COMPASS (eOffice Platform)
├── Auth & RBAC (shared)           ← DocGen inherits roles/permissions
├── User Directory / HR Module     ← DocGen pulls signatory metadata
├── Notifications (shared)         ← DocGen triggers alerts/emails
├── Audit Trail (shared)           ← DocGen logs every action
├── File Storage (shared)          ← DocGen stores PDFs/assets
└── DocGen Module  ←─────────────── This document
    ├── Template Builder
    ├── Document Composer
    ├── Review & Approval Engine
    ├── PDF Generator
    └── QR Tracking & Archive
```

DocGen is mounted as a Django app (`docgen`) under the COMPASS project, reusing COMPASS's authentication, user model, notification bus, and storage backends. It exposes its own URL namespace (`/compass/docgen/`) and a clean internal API so other COMPASS modules can programmatically request document generation.

---

## 2. Core Concepts & Terminology

| Term | Meaning |
|---|---|
| **Template** | A reusable document skeleton with named placeholders, styling, and layout rules |
| **Document** | A concrete instance of a Template, filled with real data |
| **Placeholder** | A typed, named field in a template (text, date, table, signature, etc.) |
| **Workflow** | An ordered chain of review/approval steps assigned to a Document |
| **Stage** | One step in a Workflow (reviewer, approver, co-signer, etc.) |
| **Action** | What an actor does at a Stage: Review, Approve, Reject, Return, Endorse |
| **QR Token** | A unique, signed token embedded in each finalized Document for verification |
| **Revision** | A versioned snapshot of a Document at any point in its lifecycle |

---

## 3. Document Types Supported (Initial Scope)

- Official Letter (internal / external)
- Memorandum (inter-departmental)
- Office Order (administrative directive)
- Circular / Notice
- Endorsement
- Certificate (of service, appreciation, etc.)
- Meeting Agenda / Minutes
- Show Cause / Explanation Letter

Each type maps to one or more Templates and carries its own default Workflow schema.

---

## 4. Data Architecture

### 4.1 Core Models (Conceptual)

```
TemplateCategory
    └── Template
            ├── TemplatePlaceholder  (field definitions)
            ├── TemplateWorkflowSchema  (default approval chain)
            └── TemplateRevision  (version history of the template itself)

Document
    ├── DocumentField  (filled placeholder values)
    ├── DocumentRevision  (snapshots)
    ├── DocumentWorkflow  (active workflow instance)
    │       └── WorkflowStage  (each step, actor, status, timestamp)
    ├── DocumentAttachment
    ├── DocumentComment  (threaded, stage-scoped)
    ├── DocumentQRToken  (unique traceability token)
    └── DocumentPDF  (generated PDF artifacts)

Template Builder:
    TemplateBlock  (header, body, signature block, footer, table, etc.)
    TemplateAsset  (logos, watermarks, letterhead images)
```

### 4.2 Placeholder Field Types

- `SHORT_TEXT` — single line
- `LONG_TEXT` — multi-line / rich text
- `DATE` — with configurable format
- `NUMBER` — integer or decimal
- `CURRENCY`
- `DROPDOWN` — choices list
- `USER_LOOKUP` — pulls from COMPASS user directory
- `DEPARTMENT_LOOKUP`
- `TABLE` — dynamic row/column data
- `SIGNATURE` — draws from approver metadata
- `REFERENCE_NUMBER` — auto-generated, formatted
- `ATTACHMENT_LINK`
- `CONDITIONAL_BLOCK` — show/hide based on another field's value

---

## 5. Template Builder

### 5.1 Builder Interface

A drag-and-drop visual editor (browser-based) that operates on a canvas representing the document page. Key capabilities:

- **Page Setup:** paper size (A4/Letter), orientation, margins
- **Block Palette:** Header Block, Letterhead Block, Subject Line, Body Paragraph, Table Block, Signature Block, Footer Block, Page Number, Date Stamp, Reference Number
- **Placeholder Insertion:** click to insert `{{field_name}}` tokens inline within any text block; configure each placeholder's type, label, validation, and whether it is required
- **Conditional Sections:** mark a block as conditional on a field value (e.g. show "penalty clause" only if document type = Show Cause)
- **Styling Panel:** font, size, line spacing, alignment, borders, shading — all constrained to an approved COMPASS style guide palette
- **Letterhead & Branding:** upload/select organisation logo, configure header/footer with org name, address, and document reference
- **Preview Mode:** renders a live preview with sample data before saving the template
- **Template Locking:** once a template is published, it can be versioned but not destructively edited (previous documents retain their template snapshot)

### 5.2 Template Versioning

Every save of a published template creates a new `TemplateRevision`. Documents always reference the specific revision they were created from, ensuring historical fidelity.

### 5.3 Template Permissions

- **Template Author:** can create and edit drafts
- **Template Publisher:** can publish/retire templates (usually a designated admin role)
- **Template Viewer:** all staff; can use published templates

---

## 6. Document Lifecycle

```
DRAFT → UNDER_REVIEW → UNDER_APPROVAL → APPROVED → FINALIZED → ARCHIVED
          ↑                 ↑
          └── RETURNED ─────┘   (at any review/approval stage)
              REJECTED (terminal, with reason)
              WITHDRAWN (by originator, before finalization)
```

### 6.1 Stage Descriptions

| Status | Who acts | What happens |
|---|---|---|
| DRAFT | Originator | Creates, fills fields, can preview PDF |
| UNDER_REVIEW | Reviewer(s) | Can annotate, comment, return for correction |
| UNDER_APPROVAL | Approver(s) | Can approve, reject, or return |
| APPROVED | System / Final Approver | Triggers PDF generation + QR stamping |
| FINALIZED | System | Immutable, PDF locked, QR active |
| ARCHIVED | System / Admin | Long-term storage, searchable, read-only |

---

## 7. Review & Approval Engine

### 7.1 Workflow Schema

Each Template defines a default `WorkflowSchema` — an ordered list of stages. When a Document is created from a Template, its workflow is instantiated from this schema. The originator can adjust the workflow within allowed bounds (e.g. add a co-reviewer) before submission.

### 7.2 Stage Types

- **Sequential:** each stage begins only when the previous is complete
- **Parallel:** multiple actors must act before the workflow advances (configurable: all-must-approve vs. any-one-approves)
- **Conditional Branch:** the next stage depends on the outcome of the current one

### 7.3 Actor Resolution

Actors in a stage can be defined as:

- A specific named user (from COMPASS user directory)
- A role (e.g. "Head of Department of originator")
- A position title (e.g. "Director General")
- Dynamic resolution: "N+1 of originator" (auto-resolved at runtime via COMPASS org chart)

### 7.4 Actions at Each Stage

- **Endorse / Review:** acknowledges review, forwards without authority signature
- **Approve:** formal approval; triggers next stage
- **Approve with Comments:** approved but with remarks attached
- **Return for Correction:** sends back to originator (or a specified prior stage) with comments
- **Reject:** terminates the workflow with a rejection reason
- **Delegate:** reassigns the stage to another eligible actor (logged)
- **Request Clarification:** pauses workflow, sends query back to originator

### 7.5 Deadlines & Escalation

- Each stage can carry an SLA (e.g. must act within 2 working days)
- COMPASS notification bus sends reminders at configurable intervals
- Auto-escalation: if SLA breached, notify the actor's supervisor and optionally reassign

---

## 8. PDF Generation System

### 8.1 Generation Triggers

- **Preview:** available at any lifecycle stage; watermarked "DRAFT" or "PREVIEW"
- **On Approval:** generated automatically when document reaches APPROVED status
- **On-demand re-generate:** for admins, if template layout changes require re-issue (new version, old retained)

### 8.2 PDF Contents

- Rendered document body (from template + filled fields)
- Document metadata header: reference number, date, originator, classification
- Approval trail block: list of all actors, their actions, timestamps, and digital signature lines
- QR code: bottom of every page (links to COMPASS verification endpoint)
- Watermark: "DRAFT" / "CONFIDENTIAL" / "FOR OFFICIAL USE ONLY" based on classification
- Page numbers, document reference in footer

### 8.3 PDF Storage

PDFs are stored in COMPASS's shared file storage (S3-compatible or local, as configured). Each generated PDF is recorded as a `DocumentPDF` record with version, generation timestamp, and hash (SHA-256) for integrity verification.

---

## 9. QR Code Tracking & Traceability

### 9.1 QR Token Design

Each finalized document receives a unique `DocumentQRToken`:

- Contains a signed URL pointing to the COMPASS public verification endpoint
- Encodes: document reference number, document type, finalization date, hash of the PDF
- The token is cryptographically signed (HMAC or asymmetric key) so it cannot be forged
- Embedded on every page of the final PDF (bottom margin)

### 9.2 Verification Endpoint

A publicly accessible (or internally accessible, policy-dependent) endpoint at `/compass/docgen/verify/<token>/` that:

- Decodes and verifies the QR token
- Displays: document title, reference number, type, originator, approval date, final approver
- Shows current status (valid / superseded / revoked)
- Allows download of the official PDF (subject to access policy)
- Does NOT expose sensitive content to unauthenticated users unless explicitly configured

### 9.3 Physical Document Tracing

When a DocGen document is printed and physically circulated, scanning the QR code brings up its full digital record in COMPASS — bridging the physical-digital gap, a key eOffice requirement.

### 9.4 Revocation & Supersession

- If a document is formally superseded (e.g. an amended Office Order replaces an earlier one), the old QR token's verification page shows "SUPERSEDED BY [reference]"
- Revocation (e.g. legal hold) is an admin action that invalidates the QR and flags the PDF

---

## 10. Reference Number Generation

Each document gets an auto-generated, formatted reference number at the point of submission (not creation, to avoid gaps):

```
COMPASS/[ORG_CODE]/[DOC_TYPE_CODE]/[YEAR]/[SEQUENCE]
Example: COMPASS/HQ/OO/2026/00047
```

- Sequence is per document type per year, zero-padded
- Reference numbers are immutable once assigned
- The system maintains a `ReferenceNumberSequence` table with atomic increment to prevent duplicates under concurrent load

---

## 11. COMPASS Integration Points

| Integration | Mechanism |
|---|---|
| **Authentication & SSO** | Inherits COMPASS session/token auth; no separate login |
| **User Directory** | Internal API call to COMPASS HR/User module for actor lookup |
| **Org Chart** | Resolves dynamic roles (N+1, HOD) via COMPASS org chart service |
| **Notification Bus** | Publishes events (`document.submitted`, `stage.completed`, etc.) consumed by COMPASS notifications |
| **Audit Log** | Every DocGen action writes to COMPASS's central audit trail |
| **File Storage** | Uses COMPASS's configured storage backend (no DocGen-specific storage config) |
| **Search** | DocGen documents are indexed into COMPASS's global search (Elasticsearch or similar) |
| **Dashboard Widgets** | DocGen exposes a widget API for COMPASS home dashboard: "Pending Approvals", "Recent Documents" |
| **Inbox Integration** | Workflow tasks appear in the COMPASS unified inbox/task tray |
| **Reporting Module** | DocGen exposes aggregate data to COMPASS reports (documents per type, SLA compliance, etc.) |

---

## 12. Access Control & Security

### 12.1 Role-Based Access (inherits COMPASS RBAC)

| Role | Permissions |
|---|---|
| `docgen.originator` | Create documents from published templates |
| `docgen.reviewer` | Review and comment on documents assigned to them |
| `docgen.approver` | Approve/reject documents at assigned stages |
| `docgen.template_author` | Create/edit template drafts |
| `docgen.template_publisher` | Publish and retire templates |
| `docgen.admin` | Full access, override workflows, revoke QR tokens |
| `docgen.viewer` | Read-only access to finalized documents in their scope |

### 12.2 Document-Level Access

- Documents are visible only to: originator, current/past actors in workflow, and admins
- Finalized documents may be made accessible to a broader audience (configurable per document type)
- Department-scoping: staff only see documents originating from or addressed to their department (unless wider access granted)

### 12.3 Security Hardening

- All PDFs stored with server-side encryption
- QR tokens signed with a rotating key (annual rotation, old keys retained for verification)
- Immutable audit trail for every action
- CSRF, rate limiting, and input sanitisation on all endpoints
- Sensitive field values (e.g. salary figures) masked in non-privileged views

---

## 13. Notifications & Communication

DocGen publishes the following events to the COMPASS notification bus:

- Document submitted for review
- Stage completed / document advanced
- Document returned for correction (with comments)
- Document approved / rejected / finalized
- SLA reminder (approaching deadline)
- SLA breach (escalation)
- Template published / retired
- QR verification attempted on a revoked document

Notification channels (configured in COMPASS): in-app bell, email.

---

## 14. Search, Filtering & Archive

### 14.1 Search Scope

All documents are searchable within COMPASS global search by:

- Reference number (exact or partial)
- Title / subject
- Document type
- Originator name / department
- Date range
- Status
- Content (full-text, if indexed)
- Tags / classification labels

### 14.2 Archive & Retention

- Finalized documents move to ARCHIVE status after a configurable retention period (e.g. 5 years active, then cold archive)
- Archive policy per document type, configurable by admins
- Archived documents remain searchable and QR-verifiable; PDFs remain accessible

---

## 15. Reporting & Analytics

DocGen provides a reporting dashboard within COMPASS showing:

- Volume by document type / department / period
- Average time per workflow stage (SLA compliance rate)
- Bottleneck analysis (which actors/stages cause the most delays)
- Rejection / return rate by template or department
- Template usage frequency
- Pending approvals count by actor
- Documents finalized per month (trend chart)

---

## 16. API Surface (for COMPASS internal use)

DocGen exposes a clean internal REST API (Django REST Framework) under `/api/docgen/` for:

- Other COMPASS modules to trigger document creation programmatically
- Mobile/thin clients to consume document data
- Reporting integrations

Key endpoint groups:
- `/api/docgen/templates/` — CRUD on templates
- `/api/docgen/documents/` — CRUD and lifecycle actions
- `/api/docgen/documents/{id}/workflow/` — workflow management
- `/api/docgen/documents/{id}/pdf/` — PDF generation and retrieval
- `/api/docgen/documents/{id}/qr/` — QR token info
- `/api/docgen/verify/{token}/` — Public QR verification

---

## 17. Technology Choices

| Concern | Choice | Rationale |
|---|---|---|
| Backend | Django (COMPASS standard) | Consistency with platform |
| REST API | Django REST Framework | COMPASS standard |
| Real-time (workflow notifications) | Django Channels / ASGI | Already being integrated into COMPASS |
| Template storage | DB + file storage | Structured metadata in DB, raw template assets in file storage |
| PDF generation | WeasyPrint or ReportLab (HTML→PDF) | WeasyPrint preferred for CSS-based layouts |
| QR generation | `qrcode` Python library | Lightweight, well-maintained |
| Template Builder UI | React or Alpine.js + Django views | Rich interactive canvas; React recommended for builder complexity |
| Full-text search | COMPASS search backend (Elasticsearch or Whoosh) | No separate search infra |
| Task queue | Celery (COMPASS standard) | Async PDF generation, SLA checks, notifications |

---

## 18. Phased Delivery Plan

### As and when the app becomes fully ready

---

## 19. Key Design Decisions & Constraints

1. **No standalone auth** — DocGen relies entirely on COMPASS identity; it should never have its own login page.
2. **Template immutability after publishing** — prevents retroactive document falsification; enforced at the model and API layer.
3. **PDF as the record of truth** — the PDF (not the database fields) is the archival artefact; it must be reproducible and hash-verifiable.
4. **QR on every page** — not just the cover, because physical documents may be separated.
5. **All workflow actions are audited** — including views, downloads, and failed verification attempts.
6. **Celery for all heavy lifting** — PDF generation and SLA checks are never synchronous HTTP operations.
7. **Locale/timezone** — all dates stored as UTC, displayed in COMPASS's configured local timezone.

---

This plan gives you a complete, integration-aware blueprint for DocGen. 