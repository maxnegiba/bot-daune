# Auto Daune - System Architecture & Workflow Report

## 1. System Overview & Architecture
Auto Daune is a Django-based web application designed to automate and manage the process of filing auto insurance claims. It serves as an intelligent intermediary between victims of car accidents and insurance companies.

The application allows users to submit their documents (via WhatsApp or a custom Web Chat), automatically analyzes them using AI-powered OCR, collects necessary information, helps users generate and sign legal mandates, and facilitates email negotiations with insurers.

**Core Tech Stack:**
- **Backend Framework:** Django 6.0.1 (Python 3)
- **Database:** PostgreSQL (production), SQLite (local testing)
- **Asynchronous Task Queue:** Celery with Redis as the message broker.
- **AI & OCR:** OpenAI Vision API (via `gpt-4o` or similar models) combined with PyMuPDF and Pillow for image/document preprocessing.
- **Communication Channels:**
  - WhatsApp: Twilio API integration.
  - Web Chat: Custom frontend UI communicating with Django endpoints.
  - Email: SendGrid (SMTP for sending) and standard IMAP (for receiving insurer replies).
- **Admin Interface:** `django-unfold` for a modern Tailwind-based admin dashboard.

---

## 2. Core Workflows (FlowManager)
The heart of the application's conversational logic is the `FlowManager` (`apps/bot/flow.py`), which implements a State Machine based on the `Case.Stage` enum.

When a message or document arrives from Twilio (WhatsApp) or the Web Chat, it is routed through `FlowManager.process_message`. The flow depends heavily on the current stage of the `Case`:

1. **GREETING (`GREETING`):**
   - Greets the user and creates a case if none exists. Starts the document collection process.
2. **COLLECTING_DOCS (`DOCS`):**
   - The user uploads images/PDFs. The bot processes them using the OCR pipeline.
   - Evaluates the `Case` checklist (`has_id_card`, `has_car_identity`, `has_accident_report`, etc.).
   - If documents are missing, it asks for them. If complete, it moves to insurer selection.
3. **SELECTING_GUILTY_INSURER (`GUILTY_INS`):**
   - Replaces automatic guilt determination. The bot provides a list of registered insurers, and the user manually selects the guilty party's insurer.
4. **SELECTING_RESOLUTION (`RES_SEL`):**
   - The user chooses their compensation method: *Regie Proprie*, *Service Autorizat RAR*, or *Daună Totală*.
5. **SIGNING_MANDATE (`SIGN`):**
   - Generates a dynamic PDF mandate containing client and vehicle details (retrieved dynamically via the `VICTIM` role vehicle). Sends a signing link to the user.
6. **PROCESSING_INSURER (`INSURER`):**
   - The bot compiles the documents and sends a formal claim email to the insurer.
   - It acts as an email-to-chat relay: Insurer replies are forwarded to the user's chat, and user messages are delayed (debounced via a 30-minute Celery task `trigger_delayed_relay_task`) and batched into a single email reply to the insurer.
7. **OFFER_DECISION (`OFFER`):**
   - If the bot detects negotiation keywords in the user's chat (*accept*, *schimb*, *service*, *totala*), it triggers specific `_handle_offer_decision` actions (e.g., notifying the insurer of an accepted offer or a change in resolution choice) rather than blindly relaying the text.

**Human Intervention:**
If `Case.is_human_managed` becomes `True` (e.g., admin replies via the dashboard, or user chooses "Service RAR"), the bot silences its automatic responses, except for Web Chat document uploads which are processed silently.

---

## 3. Document Analysis & OCR
The document analysis pipeline (`DocumentAnalyzer` in `apps/claims/services.py`) is highly robust, supporting both images and PDFs.

1. **Preprocessing:**
   - Standard images (`.jpg`, `.png`, `.webp`) are loaded via Pillow.
   - PDFs are handled natively by `PyMuPDF` (`fitz`), which renders the first page into a PNG byte stream, seamlessly integrating into the vision pipeline.
2. **OpenAI Vision API:**
   - The preprocessed image is base64 encoded and sent to OpenAI alongside strict, document-specific prompts.
   - The AI identifies the document type (`tip_document`) and extracts key data (`date_extrase`).
3. **Multi-image Scanning & Fallbacks:**
   - If a single page fails identification (returns `UNKNOWN`), the system institutes a 15-second wait to group incoming pages.
   - `analyze_multiple` is then invoked, sending multiple base64-encoded images in a single prompt to collectively identify multi-page documents (like the *Cartea Identitate Vehicul* - CIV).
4. **Data Normalization (`_normalize_data`):**
   - Protects against OCR hallucinations. It forces uppercase for license plates/IBANs and removes whitespaces.
   - Includes guard clauses to prevent `NoneType is not iterable` crashes if `date_extrase` is empty.
5. **Specific Document Logic:**
   - **AMIABILA / PV_POLITIE:** Routes identically to extract the accident date. Prioritizes Rubric 6 ("Asigurat") over Rubric 9 ("Conducator") for driver name.
   - **CIV & RCA:** Deemed mandatory documents. Extracts VIN, Make, Model (CIV) and Policy info (RCA).
   - **CI (Buletin):** Extracts address, series, and number for the mandate.
   - **EXTRAS:** Explicitly instructed to look for keywords like 'Extras de cont' and extract the 24-character Romanian IBAN.

---

## 4. Asynchronous Task Processing (Celery)
To ensure the web server remains responsive, heavy operations are offloaded to Celery workers (`apps/claims/tasks.py`):

- **`analyze_document_task(document_id)`:** Orchestrates the `DocumentAnalyzer`. Parses OCR results, updates client/vehicle models (e.g., splitting names into `first_name` and `last_name`), and updates the `Case` checklist using atomic database updates (`Case.objects.filter().update(...)`) to prevent race conditions during concurrent uploads.
- **`send_claim_email_task(case_id)`:** Compiles the initial email to the insurer. It sanitizes attachment filenames (replacing slashes with underscores to prevent directory traversal errors). **Safety check:** If the victim vehicle is missing a make or license plate, it aborts the insurer email and alerts the admin.
- **`check_email_replies_task()`:** Runs periodically to poll the IMAP server. Extracts attachments from insurer replies, saves them as `CaseDocument` records, triggers OCR, and forwards links to the user.
- **`trigger_delayed_relay_task(case_id)`:** A 30-minute debounce mechanism. Groups multiple quick messages from the client into a single email reply to the insurer, preventing spam.
- **`send_24h_reminders_task()`:** Monitors unresponsiveness in the `PROCESSING_INSURER` stage using `last_message_to_insurer_at` and `last_message_from_insurer_at` timestamps. Sends a reminder to either the insurer or client after 24 hours of inactivity.

---

## 5. Data Models
The core schema (`apps/claims/models.py`) is built around the `Case`:

- **`Client`:** Stores personal info (First/Last name, phone, CNP, IBAN). Now includes `address`, `id_series`, and `id_number` to support dynamic mandate generation.
- **`Case`:** The central state machine tracking the `stage`, resolution choice, document checklist (booleans like `has_car_identity`, `has_victim_rca`), and timestamps for email flow.
- **`InvolvedVehicle`:** Linked to a `Case`. Differentiated by `Role` (`VICTIM` vs `OFFENDER`). Stores make, model, license plate, VIN, and RCA details. Initial Web Chat login automatically creates a `VICTIM` vehicle based on the provided license plate.
- **`CaseDocument`:** Stores uploaded files, the identified `doc_type`, and the raw `ocr_data` JSON.
- **`CommunicationLog`:** Tracks all messages (In/Out) across WhatsApp, Web Chat, and Email for auditing and chat history display.

---

## 6. Admin Features & UI
The Django Admin interface is heavily customized to serve as a CRM and live chat dashboard for agents.

- **Admin Chat Dashboard (`/bot/admin/dashboard/`):**
  - A custom interface using client-side JavaScript polling (1-second intervals) to simulate real-time communication without requiring WebSockets.
  - Features visual distinctions: generated avatars (initials) for clients and gradient-styled bubbles for admin messages, explicitly avoiding a "WhatsApp-clone" aesthetic.
  - Replying from this dashboard automatically sets `is_human_managed = True` on the case.
- **Search Capabilities:**
  - `ClientAdmin` and `CaseAdmin` override `get_search_results` to allow searching by the license plate of vehicles marked as `VICTIM`.
- **Known Admin Nuances:**
  - UUID fields are strictly excluded from `search_fields` to prevent 500 Internal Server Errors in PostgreSQL environments (`icontains` fails on UUID casts).
  - When combining querysets involving UUID primary keys with the OR operator (`|`) and `.distinct()`, the system uses `Q` objects within a single `.filter()` to avoid PostgreSQL `DataError` crashes.
  - `django-unfold` limits raw Tailwind utility classes in custom templates, necessitating inline styles or standard CSS for features like custom gradients.

---

## 7. Security, Deployment & Environment
- **Security:**
  - File uploads validate extensions and MIME types (`.pdf`, `.jpg`, `.mp4` etc.). Max size is 100MB.
  - Actions like WhatsApp webhooks and Web Chat endpoints are protected by CSRF validation (except Twilio webhooks which use `RequestValidator`) and rate limiting (`rate_limit` decorator using Redis cache).
  - The Web Chat logout endpoint explicitly flushes the active Django backend session (`request.session.flush()`).
- **Deployment (`DEPLOY.md`):**
  - Production requires Nginx, Gunicorn, PostgreSQL, Redis, and Celery running via `systemd`.
  - PDF generation (WeasyPrint) requires specific system-level graphics libraries (`libcairo2`, `libpango`).
- **Local Testing:**
  - Local execution requires overriding production settings via `.env` variables (`DB_ENGINE=django.db.backends.sqlite3`, `DEBUG=True`).
  - Celery task tests bypass Redis connection issues by setting `CELERY_TASK_ALWAYS_EAGER=True` and using memory brokers.

---

## 8. Technical Nuances & Edge Cases (Summary)
- **Race Conditions:** Solved via `case.objects.filter().update()` for flag updates instead of `case.save()`. Client instances are refreshed from the database (`client.refresh_from_db()`) before updates in async tasks.
- **Email Security:** Filenames derived from Document display names are sanitized (slashes replaced with underscores) before attachment to prevent traversal attacks.
- **Silent Processing:** The Web Chat UI permits document uploads even when a case is `is_human_managed`. These trigger OCR and DB updates "silently" via `silent=True`, without interrupting the human agent's conversation flow.
