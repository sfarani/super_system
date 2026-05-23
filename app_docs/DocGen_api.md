# DocGen API Reference (Current Implementation)

Base prefix: /compass/docgen/

## Health and Verification

- GET /
  - Purpose: module health
  - Response: module/status/message

- GET /verify/{token}/
  - Purpose: QR verification lookup
  - Response: valid/status/reference_number/document_type/finalized_at

## Template APIs

- GET /templates/
  - Purpose: list templates
  - Query: status (optional)

- POST /templates/
  - Purpose: create template + initial revision v1
  - Body:
    {
      "category_id": 1,
      "title": "Office Memo",
      "code": "OFFICE_MEMO",
      "description": "...",
      "layout_schema": {"blocks": []}
    }

- POST /templates/{template_id}/revisions/clone/
  - Purpose: clone latest revision into next draft revision

- POST /templates/{template_id}/publish/
  - Purpose: publish selected or latest revision
  - Body (optional): {"revision_id": 12}

- POST /templates/{template_id}/retire/
  - Purpose: retire template

## Template Revision Structure APIs

- GET /template-revisions/{revision_id}/placeholders/
- POST /template-revisions/{revision_id}/placeholders/
  - Draft revisions only
  - Body:
    {
      "name": "recipient",
      "label": "Recipient",
      "field_type": "SHORT_TEXT",
      "is_required": true,
      "display_order": 1,
      "config": {}
    }

- PATCH /template-placeholders/{placeholder_id}/
- DELETE /template-placeholders/{placeholder_id}/
  - Draft revisions only

- GET /template-revisions/{revision_id}/workflow-stages/
- POST /template-revisions/{revision_id}/workflow-stages/
  - Draft revisions only
  - Body:
    {
      "stage_order": 1,
      "title": "Initial Review",
      "mode": "SEQUENTIAL",
      "actor_type": "ROLE",
      "actor_value": "reviewer",
      "required_action": "REVIEW",
      "sla_hours": 24,
      "is_mandatory": true
    }

- PATCH /template-workflow-stages/{stage_id}/
- DELETE /template-workflow-stages/{stage_id}/
  - Draft revisions only

## Document APIs

- GET /documents/
  - Purpose: list recent documents

- POST /documents/
  - Purpose: create draft document
  - Body:
    {
      "template_revision_id": 5,
      "document_type": "MEMORANDUM",
      "title": "Memo for Review",
      "subject": "Subject"
    }

- POST /documents/{document_id}/fields/
  - Purpose: upsert placeholder values
  - Body:
    {
      "fields": [
        {
          "placeholder_name": "recipient",
          "value_text": "Director",
          "value_json": {}
        }
      ]
    }

- POST /documents/{document_id}/submit/
  - Purpose: assign reference, instantiate workflow, route to first stage

## Workflow Action APIs

- POST /documents/{document_id}/actions/{action}/
  - Supported actions:
    - review
    - endorse
    - approve
    - approve_with_comments
    - request_clarification
    - return
    - reject
    - delegate
  - Common body:
    {
      "comment": "optional note",
      "stage_id": 123,
      "next_stage_order": 3
    }
  - stage_id:
    - Optional. Targets a specific active stage within the current stage order.
  - next_stage_order:
    - Optional conditional-branch hook to route progression to a specific later stage order.
  - Delegate body requirement:
    {
      "delegate_to": "target_actor",
      "comment": "optional note"
    }

## Workflow Progression Notes

- Stage execution mode is carried from template to instantiated document stages.
- Supported runtime policies:
  - SEQUENTIAL: normal one-stage progression
  - PARALLEL_ALL: all active stages in the same order must complete before advancing
  - PARALLEL_ANY: first completion advances and sibling active stages auto-close
- Actor values are resolved through an adapter hook during submit (currently stubbed for integration).

## Timeline API

- GET /documents/{document_id}/timeline/
  - Purpose: get audit-style timeline from metadata events and document revisions
  - Response:
    {
      "document_id": 1,
      "status": "UNDER_REVIEW",
      "reference_number": "COMPASS/HQ/MEM/2026/00001",
      "events": [...],
      "revisions": [...]
    }

## Finalization API

- POST /documents/{document_id}/finalize/
  - Purpose: transition APPROVED document to FINALIZED, issue QR token, and persist a hashed PDF artifact record
  - Guard: document must be in APPROVED status
  - Response:
    {
      "document_id": 1,
      "status": "FINALIZED",
      "finalized_at": "...",
      "qr_token": "...",
      "verify_url": "/compass/docgen/verify/{token}/",
      "pdf_version": 1,
      "pdf_sha256": "..."
    }

## Archive API

- POST /documents/{document_id}/archive/
  - Purpose: transition FINALIZED document to ARCHIVED
  - Guard: finalized retention window must be reached unless force is used
  - Body (optional):
    {
      "force": false
    }
  - Response:
    {
      "document_id": 1,
      "status": "ARCHIVED",
      "retention_days": 365,
      "forced": false
    }

## RBAC Enforcement Flag

- Setting: DOCGEN_ENFORCE_RBAC (default False)
- Location: super_system/settings.py
- Behavior:
  - False: API works without role enforcement (dev bootstrap mode)
  - True: mutating endpoints require DocGen roles/groups
    - Template author/publisher/admin for template write operations
    - Originator/admin for document create/update/submit
    - Reviewer/approver/admin for workflow actions

  ## Archive Retention Setting

  - Setting: DOCGEN_DEFAULT_ARCHIVE_DAYS (default 365)
  - Location: super_system/settings.py
  - Use: default retention window before finalized documents are archive-eligible
