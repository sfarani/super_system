# Super System — AI Coding Assistant Instructions

## Project Overview

**Super System** is my playground to test my ideas for the COMPASS. COMPASS (Comprehensive Organizational Management & Process Automation System) is a Django-based platform for PNRA (Pakistan Nuclear Regulatory Authority) managing employees, documents, training, competency analysis, and workflow automation. COMPASS has the following tech stack and core apps:

**Tech Stack**: Django 5.2 · Python 3.x · Celery/Redis · SQLite (dev) / PostgreSQL / MySQL (prod) · Docker

---

## Core Apps in the COMPASS

| App | Purpose |
|-----|---------|
| **Employees** | Custom User model (`AUTH_USER_MODEL = 'Employees.User'`), office hierarchy, P-number system (`P-10955`) |
| **IRIS** | Document management: OCR, text extraction, Elasticsearch indexing, category-based ACL, thumbnails |
| **FileStream** | Digital file workflow: correspondence, notings, digital signatures, mentions, HRD-only access (`allowed_offices = ['HRD']`) |
| **PRISM** | Competency Needs Analysis (IAEA SARCoN): KSA assessments, levels NA/0 B/1 M/2 H/3 |
| **Training** | Event management: nominations, approvals, evaluations (multi-stage workflow) |
| **Worq** | HR rationalization and tracking |
| **Dashboard, HRBlog, Notifications, Regulations, LookupData** | Supporting modules |

---

## Architecture Patterns of the COMPASS is organized by app, but there are cross-cutting patterns to follow:

**Employee-Centric**: Most models have `ForeignKey(Employee)`. Get current office:
```python
employee = request.user.employee
current_exp = employee.experience_history.filter(is_current=True).first()
current_office = current_exp.office if current_exp else None
```

**Access Control**:
- `FileStream/decorators.py` — office membership & noting participation checks
- IRIS — department-based categories with `is_global` flag for cross-department sharing
- `@permission_required('auth.change_user')` in permission_manager
- All authenticated views require `@login_required`

**Async Task Processing (IRIS)**:
- Celery fallback: auto-switches to sync when Redis/Celery unavailable; check via `CeleryStatus.is_available()`
- Task chain: Upload → `generate_thumbnail_task` → `process_pdf_ocr_task` → `extract_document_text_task` → `index_document_task`
- Upload pattern: Create `Document` → Create `DocumentVersion` → signals auto-trigger processing
- OCR intelligence: checks if PDF has text; if empty, triggers OCR before extraction
- See `Info/IRIS_CELERY_FALLBACK.md`, `Info/CELERY_QUICK_START.md`

**Search**: Elasticsearch DSL via `iris/elasticsearch_service.py`. Graceful degradation (search disabled if unavailable).

---

## Development Setup (COMPASS root)

### Python Environment (virtualenvwrapper)

- Environment manager: `virtualenvwrapper-win`
- Project environment name: `envSuper`
- Environment location: `C:\Users\Sahibzada\Envs\envSuper`
- Python executable to use for all project commands:
    - `C:\Users\Sahibzada\Envs\envSuper\Scripts\python.exe`

Recommended command patterns:

```powershell
# Option 1: activate by name
workon envSuper

# Option 2: run directly with env python (no activation needed)
C:\Users\Sahibzada\Envs\envSuper\Scripts\python.exe manage.py runserver
```

Rule for AI coding assistants in this project:

- Always run Django/manage.py commands with `envSuper`.
- If shell activation is uncertain, use the full python path from `envSuper` directly.

```bash
# Dev (no Celery)
python manage.py runserver

# Dev (with Celery — recommended)
redis-server                                       # Terminal 2
celery -A COMPASS worker -l INFO                   # Terminal 3 Linux
celery -A COMPASS worker -l INFO -P solo           # Terminal 3 Windows

# Helper scripts
./run_dev_env.sh      # Linux
manage_celery.cmd     # Windows

# Docker (Elasticsearch:9200, Kibana:5601, Redis:6379, Celery, Flower:5555)
docker-compose up -d
docker-compose logs -f celery

# Database
python manage.py makemigrations && python manage.py migrate
python manage.py import_sarcon_data   # PRISM/SARCoN lookup data

# Elasticsearch
python manage.py search_index --rebuild
./fix_elasticsearch_index.sh

# Status
python manage.py celery_status
```

**DB config**: SQLite default; `.env`: `USE_POSTGRES=True` or `USE_MYSQL=True`
**Timezone**: `USE_TZ=False`, `Asia/Karachi`

---

## Code Conventions

### Models
- Categorical fields: `models.CharField(choices=...)` (see `Employee.TITLE_CHOICES`)
- Validators: `RegexValidator` — CNIC `^\d{5}-\d{7}-\d{1}$`, P-number `^P-\d{5}$`
- FK `on_delete`: `CASCADE` (owned), `PROTECT` (referenced), `SET_NULL` (soft)
- Timestamps: `auto_now_add=True` (created), `auto_now=True` (updated)
- Upload paths: UUID-based functions e.g. `document_upload_path(instance, filename)` in `iris/models.py`
- **Always create migrations after model changes**

### Forms
- Styling: `django-widget-tweaks` with `{% render_field %}` + Tailwind classes. Do **not** use `crispy_forms`.
- Autocomplete: `django-select2` for all searchable dropdowns (see `FileStream/forms.py`)
- Always extend `forms.ModelForm`

### Views & URLs
- Namespace URLs: `path('iris/', include('iris.urls', namespace='iris'))`
- Notifications: `sweetify` (`SWEETIFY_SWEETALERT_LIBRARY = 'sweetalert2'`)
- Context: shared CDN links via `COMPASS.context_processors.global_cdn_links`

### Settings
- Env vars: `python-decouple` — `config('VAR_NAME', default='value', cast=bool)`
- Static/Media: `STATIC_ROOT`, `MEDIA_ROOT` via `.env`

### Celery Tasks
- Always `@shared_task`; retry pattern: `@shared_task(bind=True, max_retries=3)` with exponential backoff
- Test both with and without Celery (fallback scenario must mirror async functionality)

### Key Files

| Concern | File |
|---------|------|
| Config & routing | `COMPASS/settings.py`, `COMPASS/urls.py`, `COMPASS/celery.py` |
| IRIS tasks / fallback / ACL | `iris/tasks.py`, `iris/celery_utils.py`, `iris/models.py` |
| FileStream ACL | `FileStream/decorators.py`, `FileStream/models.py` |
| Employee / User model | `Employees/models.py`, `Employees/utils.py` |
| Docs | `Info/` directory |

---

## Active Development (see `Info/TODO.md`)

- **IRIS**: Dashboard, enhanced ACL, advanced search, file sharing
- **FileStream**: Access control expansion (beyond HRD), dashboard, sections→cases rename
- **PRISM**: Survey time-bounding, Sets of KSAs, gap analysis
- **All apps**: Data sanity checks, employee/directorate rationalization

---

---

# Tailwind CSS Design System (Will use this for the Super System UI so that they are compatible with the COMPASS when I migrate the templates)

All templates use **Tailwind CSS** (Bootstrap deprecated). Styling is UI-only — **never** modify form field names, HTML structure, data attributes, template variables, or business logic when refactoring templates.

**Reference templates**: `FileStream/templates/FileStream/file_list.html`, `todo_list.html`, `partials/todo_modal.html`

---

## Core Rules

| Rule | Value |
|------|-------|
| Corners | `rounded-none` always — never `rounded-lg`, `rounded-md`, `rounded-full` |
| Borders | `border border-gray-300` (default) · `border-b border-blue-800` (header divider) |
| Card shadow | `shadow-sm` |
| Button shadow | `shadow-md` |
| Focus | `focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500` |
| Labels | `uppercase tracking-wide` with Lucide icon prefix |
| Section headers | `bg-gradient-to-r from-blue-600 to-blue-700` |
| Transitions | `transition-all` (multi), `transition-colors` (color), `transition-shadow` (shadow) |
| Breakpoints | Mobile-first: no prefix · `md:` 768px+ · `lg:` 1024px+ |

---

## Typography Classes

| Element | Classes |
|---------|---------|
| Page title | `text-2xl font-bold text-gray-900` |
| Section header | `text-xs font-bold text-white uppercase tracking-wider` |
| Card / subsection title | `text-sm font-semibold text-gray-900 uppercase tracking-wide` |
| Form label | `text-xs font-semibold text-gray-700 uppercase tracking-wide` |
| Body text | `text-sm text-gray-600` |
| Helper / secondary | `text-xs text-gray-500` |

---

## Icon Guidelines (Lucide)

Use `{% lucide "icon-name" size="18" class="inline-block mr-1.5" %}` or `<svg><use href="#lucide/name"/></svg>`.

**Common mappings**: `Search` · `Filter` · `Calendar` · `Heading2` · `AlignLeft` · `AlertCircle` · `Loader` · `CheckCircle` · `Link` · `Tag` · `User` · `Clock` · `ListTodo`

**Two alignment patterns — use the right one:**

| Pattern | When to use | Key classes |
|---------|-------------|-------------|
| **Horizontal** | Labels, buttons, headers (icon + text on same line) | `inline-flex items-center` + `mr-1.5` / `mr-2` / `mr-2.5` |
| **Vertical stack** | Empty states, stat cards (icon above title above subtitle) | `flex flex-col items-center text-center` + `mx-auto` on icon |

---

## Spacing Standards

| Context | Padding |
|---------|---------|
| Section / gradient headers | `px-5 py-3` |
| Cards | `p-5` |
| Form fields & buttons | `px-4 py-2.5` / `px-5 py-2.5` |
| Section gap | `mb-6` |
| Card inner gap | `space-y-4` or `space-y-6` |

---

## Component Reference

### Buttons
```html
<!-- Primary -->
<button class="inline-flex items-center px-5 py-2.5 bg-gradient-to-r from-blue-600 to-blue-700 hover:from-blue-700 hover:to-blue-800 text-white text-xs font-bold uppercase tracking-wide rounded-none shadow-md transition-all">
    <svg class="w-4 h-4 mr-2"><use href="#lucide/save"/></svg> Save
</button>

<!-- Secondary -->
<button class="inline-flex items-center px-5 py-2.5 bg-gray-200 hover:bg-gray-300 text-gray-900 text-xs font-bold uppercase tracking-wide rounded-none shadow-sm transition-colors">
    <svg class="w-4 h-4 mr-2"><use href="#lucide/x"/></svg> Cancel
</button>

<!-- Danger -->
<button class="inline-flex items-center px-5 py-2.5 bg-red-600 hover:bg-red-700 text-white text-xs font-bold uppercase tracking-wide rounded-none shadow-md transition-all">
    <svg class="w-4 h-4 mr-2"><use href="#lucide/trash-2"/></svg> Delete
</button>

<!-- Link / text-only -->
<button class="inline-flex items-center text-blue-600 hover:text-blue-700 text-xs font-bold uppercase tracking-wide">
    <svg class="w-4 h-4 mr-1"><use href="#lucide/edit"/></svg> Edit
</button>
```

### Form Inputs

All inputs share the base class string: `w-full px-4 py-2.5 border border-gray-300 rounded-none text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all`

```html
<!-- Labeled field -->
<div>
    <label class="block text-xs font-semibold text-gray-700 mb-2 uppercase tracking-wide">
        <svg class="w-4 h-4 mr-1.5 inline-block"><use href="#lucide/search"/></svg> Search
    </label>
    <input type="text" class="w-full px-4 py-2.5 border border-gray-300 rounded-none text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all" placeholder="Enter search term">
</div>

<!-- Textarea (add resize-vertical) -->
<textarea rows="3" class="w-full px-4 py-2.5 border border-gray-300 rounded-none text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all resize-vertical"></textarea>

<!-- Checkbox -->
<input type="checkbox" class="w-5 h-5 text-blue-600 rounded-none focus:ring-2 focus:ring-blue-500">
```

### Section Header (Gradient)
```html
<div class="bg-white rounded-none shadow-sm border border-gray-300 overflow-hidden mb-6">
    <div class="bg-gradient-to-r from-blue-600 to-blue-700 px-5 py-3 border-b border-blue-800">
        <h3 class="text-xs font-bold text-white flex items-center uppercase tracking-wider">
            <svg class="w-4 h-4 mr-2.5"><use href="#lucide/filter"/></svg> Section Title
        </h3>
    </div>
    <div class="p-5"><!-- content --></div>
</div>
```

### Cards
```html
<!-- Stat card — side-by-side layout -->
<div class="bg-white rounded-none shadow-sm border border-gray-300 p-5 hover:shadow-md transition-shadow">
    <div class="flex items-center justify-between">
        <div>
            <p class="text-xs font-semibold text-gray-600 mb-2 uppercase tracking-wide">Total Items</p>
            <p class="text-3xl font-bold text-gray-900">{{ count }}</p>
        </div>
        <div class="bg-blue-100 rounded-none p-4">
            <svg class="w-8 h-8 text-blue-600"><use href="#lucide/inbox"/></svg>
        </div>
    </div>
</div>

<!-- Stat card — centered vertical stack -->
<div class="bg-white rounded-none shadow-sm border border-gray-300 p-5 hover:shadow-md transition-shadow">
    <div class="flex flex-col items-center text-center">
        <div class="bg-blue-100 rounded-none p-4 mb-4">
            <svg class="w-8 h-8 text-blue-600 mx-auto"><use href="#lucide/inbox"/></svg>
        </div>
        <p class="text-xs font-semibold text-gray-600 mb-2 uppercase tracking-wide">Total Items</p>
        <p class="text-3xl font-bold text-gray-900">{{ count }}</p>
    </div>
</div>

<!-- Content card with priority indicator -->
<div class="bg-white rounded-none shadow-sm border border-gray-200 p-4 priority-HIGH">
    <h4 class="font-semibold text-gray-900 text-sm mb-2">Card Title</h4>
    <p class="text-xs text-gray-600 mb-3">Content goes here</p>
</div>
<style>
    .priority-HIGH   { border-left: 4px solid #ef4444; }
    .priority-MEDIUM { border-left: 4px solid #f59e0b; }
    .priority-LOW    { border-left: 4px solid #10b981; }
</style>
```

### Modal
```html
<div id="modal" class="fixed inset-0 z-50 flex items-center justify-center hidden">
    <div class="absolute inset-0 bg-black opacity-50" onclick="closeModal()"></div>
    <div class="relative bg-white rounded-none shadow-lg w-full max-w-3xl mx-4 max-h-[90vh] overflow-y-auto border border-gray-300">
        <div class="sticky top-0 bg-gradient-to-r from-blue-600 to-blue-700 px-6 py-4 border-b border-blue-800 flex items-center justify-between">
            <h3 class="text-sm font-bold text-white uppercase tracking-wider">Modal Title</h3>
            <button onclick="closeModal()" class="text-white hover:text-blue-100 transition-colors">
                <svg class="w-5 h-5"><use href="#lucide/x"/></svg>
            </button>
        </div>
        <div class="p-6"><!-- content --></div>
        <div class="flex items-center justify-end space-x-3 px-6 pb-6 pt-4 border-t border-gray-300">
            <button onclick="closeModal()" class="inline-flex items-center px-5 py-2.5 bg-gray-200 hover:bg-gray-300 text-gray-900 text-xs font-bold uppercase tracking-wide rounded-none shadow-sm transition-colors">
                <svg class="w-4 h-4 mr-1.5"><use href="#lucide/x"/></svg> Cancel
            </button>
            <button type="submit" class="inline-flex items-center px-5 py-2.5 bg-gradient-to-r from-blue-600 to-blue-700 hover:from-blue-700 hover:to-blue-800 text-white text-xs font-bold uppercase tracking-wide rounded-none shadow-md transition-all">
                <svg class="w-4 h-4 mr-1.5"><use href="#lucide/save"/></svg> Save
            </button>
        </div>
    </div>
</div>
```

### Status Badges & Alerts
```html
<!-- Priority badge (swap colors/icon per level) -->
<!-- High:   bg-red-100 text-red-700   / lucide/alert-circle    -->
<!-- Medium: bg-amber-100 text-amber-700 / lucide/circle         -->
<!-- Low:    bg-green-100 text-green-700 / lucide/arrow-down     -->
<!-- Urgent: bg-red-100 text-red-800   / lucide/alert-triangle   -->
<span class="inline-flex items-center px-2.5 py-1 bg-red-100 text-red-700 rounded-none font-bold text-xs uppercase tracking-wide">
    <svg class="w-3 h-3 mr-1"><use href="#lucide/alert-circle"/></svg> High
</span>

<!-- Counter badge -->
<span class="inline-flex items-center justify-center w-7 h-7 bg-blue-100 text-blue-700 rounded-none text-xs font-bold">42</span>

<!-- Info box -->
<div class="bg-blue-50 border border-blue-200 rounded-none p-3">
    <p class="text-xs text-gray-700">
        <svg class="w-4 h-4 mr-2 text-blue-600 inline-block"><use href="#lucide/info"/></svg>
        <span class="font-semibold">Note:</span> Information text here
    </p>
</div>

<!-- Error box -->
<div class="bg-red-50 border border-red-200 rounded-none p-4">
    <ul class="list-disc list-inside text-sm text-red-700"><li>Error message</li></ul>
</div>
```

### Empty State
```html
<div class="flex flex-col items-center justify-center text-center py-12">
    <svg class="w-16 h-16 mb-3 text-gray-400 opacity-50 mx-auto"><use href="#lucide/inbox"/></svg>
    <p class="text-sm font-semibold text-gray-900 mb-1">No items found</p>
    <p class="text-xs text-gray-500">Try adjusting your search criteria</p>
</div>
```

### Table Header
```html
<thead class="bg-gradient-to-r from-gray-700 to-gray-800 border-b-2 border-gray-900">
    <tr>
        <th class="px-5 py-3.5 text-left text-xs font-bold text-white uppercase tracking-wider">Column</th>
    </tr>
</thead>
```

---

## Bootstrap → Tailwind Quick Map

| Bootstrap | Tailwind |
|-----------|----------|
| `row` | `grid grid-cols-*` |
| `col-md-*` | `md:col-span-*` |
| `btn btn-primary` | `inline-flex items-center ... bg-gradient-to-r from-blue-600 to-blue-700 ...` |
| `form-control` | `border border-gray-300 rounded-none px-4 py-2.5 ...` |
| `rounded` / `rounded-lg` | `rounded-none` |
| `shadow` | `shadow-sm` (cards) / `shadow-md` (buttons) |
| `card` | `bg-white border border-gray-300 shadow-sm rounded-none` |
| `text-secondary` | `text-gray-600` |
| `badge` | `inline-flex items-center px-2.5 py-1 rounded-none ...` |
| `alert alert-danger` | `bg-red-50 border border-red-200 text-red-700 rounded-none` |

---

## Migration Checklist (Bootstrap → Tailwind)

- [ ] Remove all Bootstrap classes; replace `col-*` grid with Tailwind `grid` / `md:col-span-*`
- [ ] Replace all `rounded*` variants with `rounded-none`
- [ ] Add Lucide icon prefix to every label
- [ ] Apply gradient header to every section container
- [ ] Update all buttons to Tailwind patterns
- [ ] Add `focus:ring-2 focus:ring-blue-500` to all inputs
- [ ] Add hover `transition-*` to interactive elements
- [ ] Verify responsive at 320px / 768px / 1024px+
- [ ] WCAG AA color contrast · focus indicators · keyboard navigation · ARIA labels
- [ ] **Do not** change field names, data attributes, template variables, or view/service/model logic

---

## DO / DON'T

**DO**: `rounded-none` everywhere · icon on every label · gradient section headers · `uppercase tracking-wide` on labels & headers · `transition-all` on hover · `focus:ring-2` on all inputs · mobile-first responsive grid · compose Tailwind utilities only

**DON'T**: Mix Bootstrap + Tailwind in same template · Use `rounded-lg/md/full` · Hardcode hex colors · Use Bootstrap color names (`primary`, `danger`, `warning`) · Omit focus states · Separate icon and text that should be inline · Forget `items-center` on horizontal icon-text groups · Hardcode P-numbers or CNICs in code
