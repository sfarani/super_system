# DocGen API Reference (Current Implementation)

Base prefix: /compass/docgen/

## Health and Verification

- GET /
  - Purpose: module health
  - Response: module/status/message

- GET /verify/{token}/
  - Purpose: QR verification lookup
  - Response behavior:
    - 200 valid:
      {
        "valid": true,
        "status": "valid",
        "reference_number": "...",
        "document_type": "...",
        "finalized_at": "..."
      }
    - 200 revoked:
      {
        "valid": false,
        "status": "revoked",
        "reference_number": "...",
        "document_type": "...",
        "finalized_at": "...",
        "revoked_reason": "..."
      }
    - 200 superseded:
      {
        "valid": false,
        "status": "superseded",
        "reason": "document_superseded",
        "reference_number": "...",
        "document_type": "...",
        "finalized_at": "...",
        "superseded_by": "..."
      }
    - 400 tampered token payload signature:
      {
        "valid": false,
        "status": "tampered",
        "reason": "token_tampered"
      }
    - 404 unknown token:
      {
        "valid": false,
        "reason": "token_not_found"
      }

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
  - Rendering: document data is rendered into HTML and converted to a binary PDF via xhtml2pdf
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
      "forced": false,
      "archived_at": "2026-05-23T18:22:11Z"
    }

- GET /documents/archive/
  - Purpose: list archived documents with query filters
  - Query params (optional):
    - q: partial match on reference number or title
    - document_type: exact enum match
    - archived_after: ISO datetime
    - archived_before: ISO datetime
    - limit: max rows (default 50, max 200)
  - Response:
    {
      "count": 1,
      "retention_days": 365,
      "results": [
        {
          "id": 10,
          "title": "Memo for Review",
          "reference_number": "COMPASS/HQ/MEM/2026/00001",
          "document_type": "MEMORANDUM",
          "status": "ARCHIVED",
          "archived_at": "2026-05-23T18:22:11Z",
          "finalized_at": "2026-05-20T09:10:00Z",
          "template_revision": 5
        }
      ]
    }

## RBAC Enforcement Flag

- Setting: DOCGEN_ENFORCE_RBAC (default False)
- Location: super_system/settings.py
- Behavior:
  - False: API works without role enforcement (dev bootstrap mode)
  - True: mutating endpoints require formal Django permissions via user or group assignments
    - Required codenames:
      - template_author
      - template_publisher
      - admin
      - originator
      - reviewer
      - approver
    - Mapping by endpoint class:
      - Template write operations: template_author or template_publisher or admin
      - Document create/update/submit: originator or admin
      - Workflow actions: reviewer or approver or admin

  ## Archive Retention Setting

  - Setting: DOCGEN_DEFAULT_ARCHIVE_DAYS (default 365)
  - Location: super_system/settings.py
  - Use: fallback retention window before finalized documents are archive-eligible

## Retention Policy Admin Control

- Model: RetentionPolicy
- Location: Django admin (DocGen app)
- Fields:
  - name (unique)
  - archive_retention_days (>0)
  - is_active
- Runtime behavior:
  - If an active RetentionPolicy exists, its archive_retention_days overrides DOCGEN_DEFAULT_ARCHIVE_DAYS.
  - If no active RetentionPolicy exists, DOCGEN_DEFAULT_ARCHIVE_DAYS is used.

## Actor Resolution Adapters

- Submit-time workflow actor resolution is adapter-driven.
- Setting: DOCGEN_ACTOR_RESOLUTION_ADAPTER
  - Default: DocGen.adapters.LocalActorResolutionAdapter
  - Purpose: allows swapping actor resolution to COMPASS-integrated adapters without changing workflow submit logic.

- Built-in adapters:
  - DocGen.adapters.LocalActorResolutionAdapter
    - USER -> actor_value
    - ROLE -> role:{actor_value}
    - POSITION -> position:{actor_value}
    - DYNAMIC + N+1_OF_ORIGINATOR -> dynamic:n+1:{originator_id}
  - DocGen.adapters.CompassActorResolutionAdapter
    - Uses COMPASS directory/org-chart APIs when configured and falls back to local mapping on failure.

- COMPASS adapter settings:
  - DOCGEN_COMPASS_DIRECTORY_USER_URL
  - DOCGEN_COMPASS_ORGCHART_ROLE_URL
  - DOCGEN_COMPASS_ORGCHART_POSITION_URL
  - DOCGEN_COMPASS_ORGCHART_MANAGER_URL
  - DOCGEN_COMPASS_API_TIMEOUT_SECONDS
  - DOCGEN_COMPASS_API_TOKEN

## SLA Reminder and Escalation Processing

- Service: DocGen.services.process_sla_events()
- Task hook: DocGen.tasks.process_sla_events_task
- Management command: python manage.py docgen_process_sla
  - Output counters: checked, reminders_sent, escalations_sent, notifications_sent, notification_failures

- Trigger behavior (for active UNDER_REVIEW / UNDER_APPROVAL stages with due_at):
  - Reminder event (action=sla_reminder)
    - Fired once when current time is within reminder window before due_at.
    - Stage field updated: reminder_sent_at
  - Escalation event (action=sla_escalation)
    - Fired once when stage is overdue past escalation threshold.
    - Stage fields updated: escalated_at, escalation_level

- Settings:
  - DOCGEN_SLA_REMINDER_MINUTES_BEFORE_DUE (default 60)
  - DOCGEN_SLA_ESCALATION_MINUTES_AFTER_DUE (default 120)

## Notification Bus Adapter

- Notification publishing is adapter-driven.
- Setting: DOCGEN_NOTIFICATION_ADAPTER
  - Default: DocGen.notifications.LocalNotificationAdapter

- Built-in adapters:
  - DocGen.notifications.LocalNotificationAdapter
    - No-op local adapter (always success) for dev/test bootstrap.
  - DocGen.notifications.HttpNotificationAdapter
    - Sends POST JSON events to configured endpoint.

- HTTP adapter settings:
  - DOCGEN_NOTIFICATION_HTTP_ENDPOINT
  - DOCGEN_NOTIFICATION_TIMEOUT_SECONDS
  - DOCGEN_NOTIFICATION_API_TOKEN

- Current emitted SLA event types:
  - docgen.stage.sla_reminder
  - docgen.stage.sla_escalation

- Current emitted document lifecycle/workflow event types:
  - docgen.document.create
  - docgen.document.set_fields
  - docgen.document.submit
  - docgen.document.review
  - docgen.document.endorse
  - docgen.document.approve
  - docgen.document.approve_with_comments
  - docgen.document.request_clarification
  - docgen.document.return
  - docgen.document.reject
  - docgen.document.delegate
  - docgen.document.finalize
  - docgen.document.archive

- Current emitted template lifecycle event types:
  - docgen.template.create
  - docgen.template.clone_revision
  - docgen.template.publish
  - docgen.template.retire

- Integration-test coverage:
  - Mocked COMPASS actor-resolution adapter HTTP calls (success + fallback)
  - Mocked notification HTTP adapter publish calls (success + failure)
