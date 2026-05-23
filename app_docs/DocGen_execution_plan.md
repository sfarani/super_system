# DocGen Phased Implementation and Execution Plan

## Objective
Implement DocGen as a COMPASS-aligned Django module that supports template-driven document creation, workflow approvals, PDF output, QR verification, and audit-ready traceability.

## Delivery Principles
- Build in vertical slices that are testable end-to-end.
- Preserve template/document immutability through versioning.
- Keep heavy operations asynchronous by design.
- Default to secure-by-default access and audit logging.

## Phase 0 - Foundation and Guardrails
Duration: 1-2 days

Scope:
- App bootstrapping and URL namespace under /compass/docgen/.
- Core domain model skeleton and database constraints.
- Reference number sequence and immutable numbering workflow.
- Basic QR verification endpoint scaffold.
- Admin registrations for quick operational visibility.

Exit Criteria:
- Core models migrated.
- Basic admin CRUD available.
- Reference number assignment works and is unique.
- Verify endpoint returns valid/revoked/not-found states.

Status:
- Completed (models, admin, URLs, migrations, and verification endpoint implemented and tested).

## Phase 1 - Template Builder Backend
Duration: 3-5 days

Scope:
- Template lifecycle APIs: draft, publish, retire.
- Template revision locking and non-destructive updates.
- Placeholder definitions with validation metadata.
- Workflow schema definition per template revision.

Deliverables:
- Service layer for template revision cloning and publish workflow.
- API endpoints for template management.
- Tests for immutability and version integrity.

Exit Criteria:
- Published template revisions are immutable.
- New edits always create a new revision.
- Placeholder uniqueness and field type constraints enforced.

Status:
- In progress (template lifecycle APIs and draft-only placeholder/workflow-stage CRUD APIs implemented with tests).

## Phase 2 - Document Composer and Lifecycle
Duration: 4-6 days

Scope:
- Document creation from template revision.
- Placeholder data capture and validation.
- Document status transitions with rule checks.
- Document revision snapshoting on major transitions.

Deliverables:
- Document create/update/submit endpoints.
- Status transition policy service.
- Tests for invalid transitions and revision history.

Exit Criteria:
- DRAFT to SUBMITTED path stable.
- Invalid transitions blocked and audited.
- Snapshots created for key lifecycle changes.

Status:
- In progress (document create API, field capture API, submit API, stage-aware routing for UNDER_REVIEW/UNDER_APPROVAL, delegated action handling, transition APIs, timeline API, and tests implemented).

## Phase 3 - Review and Approval Engine
Duration: 5-7 days

Scope:
- Workflow stage instantiation from template schema.
- Sequential and parallel stage execution.
- Actions: review, approve, return, reject, delegate, clarify.
- SLA timestamps and breach flags.

Deliverables:
- Workflow action API endpoints.
- Stage progression engine.
- Comments and action audit metadata.

Exit Criteria:
- Stage state machine works for sequential and parallel modes.
- Return/reject paths are deterministic.
- Actor resolution hooks ready for COMPASS directory/org chart integration.

## Phase 4 - PDF, QR, and Verification Hardening
Duration: 4-6 days

Scope:
- HTML-to-PDF rendering pipeline with versioned artifacts.
- PDF hash generation and storage metadata.
- QR token signing and verification logic.
- Revocation and supersession behavior.

Deliverables:
- PDF generation service and artifact model integration.
- QR signing utility and verification endpoint hardening.
- Tests for tampered token and superseded document behavior.

Exit Criteria:
- Finalized documents generate signed verifiable PDFs.
- Verification endpoint distinguishes valid/revoked/superseded.
- Hash integrity data persisted.

## Phase 5 - Security, Permissions, and Audit
Duration: 3-5 days

Scope:
- Role-based permissions (originator/reviewer/approver/admin/viewer).
- Document-level visibility rules.
- Action and view/download audit trail events.

Deliverables:
- Permission classes/decorators.
- Access policy service.
- Audit event publisher hooks.

Exit Criteria:
- Access is denied by default and explicitly granted.
- Every lifecycle and workflow action is auditable.

## Phase 6 - Integrations and Async Jobs
Duration: 5-8 days

Scope:
- Celery tasks for PDF generation and SLA reminder/escalation.
- Notification bus events for workflow actions.
- Internal APIs for COMPASS modules.

Deliverables:
- Async task orchestration and retries.
- Event payload schema and emitters.
- Integration tests (mocked adapters).

Exit Criteria:
- Heavy jobs are asynchronous with retries.
- Notification and escalation events emitted reliably.

## Phase 7 - UX, Search, and Reporting APIs
Duration: 5-8 days

Scope:
- Template builder frontend (initially metadata-driven forms, then drag/drop).
- Document inbox and pending actions views.
- Search/filter endpoints and reporting aggregations.

Deliverables:
- User-facing pages or API endpoints for operations.
- Dashboard aggregates and SLA analytics primitives.

Exit Criteria:
- Operators can create, submit, review, approve, and verify documents from UI/API.
- Reporting endpoints provide actionable metrics.

## Immediate Next Sprint Tasks
1. Add initial API documentation in app_docs for template and document endpoints.
2. Prepare Phase 3 workflow action endpoints (request clarification, parallel-stage behavior, conditional branching hooks).
3. Add permission checks scaffolding for originator/reviewer/approver roles.
4. Add actor-resolution adapters for role/position/dynamic assignment (COMPASS integration hooks).
5. Add SLA reminder/escalation task stubs for future Celery integration.

## Definition of Done per Phase
- Code implemented with tests.
- Migrations applied successfully.
- Basic API docs updated.
- Security and error paths verified.
- Changes logged in app_docs notes.
