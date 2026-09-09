# Daily AI Supervisor

A daily automation that triages your Gmail and sends birthday and anniversary
greetings, built to the specification in `Daily_AI_Supervisor_Automation_PRD.md`.

A supervisor runs two bounded workers once a day and produces one consolidated
report:

- **Gmail triage** — reads mail received since the last successful run,
  classifies it, summarises what matters, drafts suggested replies, and labels
  clearly low-value mail `ToDelete`.
- **Occasion greetings** — reads a contacts workbook from Google Drive, finds
  today's birthdays and anniversaries, writes a personalised greeting, generates
  an image, and sends it.

The governing design rule is that **the LLM never decides an API operation**. It
emits a structured judgement; a deterministic policy engine approves or refuses
it; only then does application code call Google. Email content is treated as
untrusted data throughout.

---

## Safety posture

- `dry_run: true` is the shipped default. Runs classify, report, and write
  nothing to Gmail. Leave it on until you have read a few days of reports.
- Mail is **never deleted**. The `gmail.modify` scope cannot permanently delete,
  so this is enforced by the credential, not just by our code.
- Replies are **never sent automatically**. Suggested reply text appears in the
  digest for you to copy, edit, and send yourself.
- Every external write — applied, refused, failed, or would-have-happened — is
  recorded in the `audit_log` table with its reason.

---

## Setup

### 1. Install

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 2. Google Cloud

1. Create a project at <https://console.cloud.google.com>.
2. Enable the **Gmail API** and the **Google Drive API**.
3. Configure the OAuth consent screen as **External**, and add your own address
   under **Test users**.
4. Create an **OAuth 2.0 Client ID** of type **Desktop app**, download the JSON,
   and save it in the project root as `credentials.json`.

> **A consent screen left in Testing mode expires refresh tokens after seven
> days.** You will need to re-run `auth` weekly. To avoid that, publish the app
> (it stays private to you; Google does not review an app that only requests
> access to the owner's own data).

### 3. Secrets

```bash
cp .env.example .env
```

Fill in `ANTHROPIC_API_KEY` and `GEMINI_API_KEY`. Optionally set
`TOKEN_ENCRYPTION_KEY` to encrypt the stored OAuth token at rest:

```bash
.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`.env` also holds every setting that identifies **you**, so that `config.yaml`
can stay a committable template:

| Variable | Overrides | Notes |
|---|---|---|
| `DAILY_DRY_RUN` | `dry_run` | `false` acts for real. Ships `true`. |
| `DAILY_DRIVE_FILE_ID` | `occasion.drive_file_id` | Your contacts workbook. |
| `DAILY_SENDER_NAME` | `occasion.sender_name` | Signs greetings. |
| `DAILY_NOTIFY_RECIPIENT` | `notifications.recipient` | Blank = your own inbox. |
| `DAILY_PROTECTED_SENDERS` | `gmail.protected_senders` | Comma-separated. Real bank/family domains belong here. |
| `DAILY_TIMEZONE` | `scheduler.timezone` | Optional. |
| `DAILY_DATABASE_URL` | `database.url` | Optional. |

An unset **or empty** variable falls back to the `config.yaml` default, so a
stray blank line cannot silently clear a setting. `DAILY_DRY_RUN` is the one
exception to leniency: an unparseable value raises rather than defaulting,
because guessing there could turn a dry run into a live one.

### 4. Contacts workbook

Put an `.xlsx` file in Google Drive with a worksheet named `Contacts`:

| EventType | EventDate | FullName | EmailAddress | MobileNumber | Relationship |
|---|---|---|---|---|---|
| Birthday | 1990-09-07 | Rahul Sharma | rahul@example.com | | Friend |
| Anniversary | 2015-09-07 | Amit & Neha | amit@example.com | | Family |

The first four columns are required. `Relationship` is optional and sets the
greeting's tone — `Family`, `Close Friend`, `Friend`, `Colleague`, `Client`.
Anything else, or blank, falls back to warm-but-professional.

Copy the file ID out of the share URL — the part between `/d/` and `/edit` —
into **`DAILY_DRIVE_FILE_ID`** in `.env`, and set **`DAILY_SENDER_NAME`** to the
name greetings should be signed with. **Greetings will not send while the sender
name is empty.** Both live in `.env` rather than `config.yaml` so they never
reach the repository.

### 5. Authorise and verify

```bash
.venv\Scripts\python.exe -m daily_ai_automation.main auth
.venv\Scripts\python.exe -m daily_ai_automation.main setup
```

`setup` checks every prerequisite and names anything missing.

---

## Usage

```bash
# One dry run: classify and report, write nothing.
python -m daily_ai_automation.main run

# Force a second run on a day that already has one.
python -m daily_ai_automation.main run --force

# Recent runs, and the report for one of them.
python -m daily_ai_automation.main history
python -m daily_ai_automation.main report last
```

Reports are written to `data/reports/` regardless of whether the email is sent.

### Going live

1. Run in dry-run mode for two or three days.
2. Read the reports. Check the "would be labeled" list against your own
   judgement.
3. Tune `gmail.auto_label_threshold` in `config.yaml`, and add any senders you
   care about to `DAILY_PROTECTED_SENDERS` in `.env`.
4. Set `DAILY_DRY_RUN=false` in `.env`.
5. Run once by hand and check Gmail before scheduling it.

### Undoing a labeling

Nothing is deleted, so this is always reversible. In Gmail, open the `ToDelete`
label, select the messages, and remove the label. To find out *why* something
was labeled:

```sql
sqlite3 data/automation.db "SELECT resource_id, outcome, reason, confidence
  FROM audit_log WHERE action='ADD_LABEL' ORDER BY timestamp DESC LIMIT 20;"
```

### Scheduling

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```

Registers a daily task at 09:00 that wakes the machine and catches up on a
missed start. Pass `-At 07:30` for a different time, or `-Unregister` to remove.

---

## Configuration

`config.yaml` is fully commented. The values worth knowing:

| Key | Meaning |
|---|---|
| `dry_run` | Global kill-switch for every outbound side effect. Override with `DAILY_DRY_RUN`. |
| `gmail.auto_label_threshold` | Minimum model confidence before `ToDelete` can be applied. |
| `gmail.auto_label_allowed_categories` | The only categories eligible for labeling, whatever the confidence. |
| `gmail.protected_senders` | Substrings matched against `From`; a match vetoes all automation. Set real values via `DAILY_PROTECTED_SENDERS`. |
| `gmail.max_emails_per_run` | Cost guard. The run warns when it hits this. |
| `occasion.send_enabled` | Turn greetings off without disabling the whole workflow. |
| `ai.classification_model` | `claude-haiku-4-5`. Triage is rubric-following, and the policy engine vetoes a wrong verdict. |
| `ai.content_model` | `claude-opus-5`. Greetings are low-volume and go to real people under your name. |

---

## Development

```bash
.venv\Scripts\python.exe -m pytest          # full suite; no network access
.venv\Scripts\python.exe -m pytest -m live   # opt-in real-model quality check
```

Every external service is faked at the integration boundary (`tests/fakes.py`).
The densest coverage is on `supervisor/policies.py`, because that is the
component that can damage a real mailbox.

Schema changes go through Alembic:

```bash
.venv\Scripts\python.exe -m alembic revision --autogenerate -m "what changed"
.venv\Scripts\python.exe -m alembic upgrade head
```

Moving to PostgreSQL is a one-line change to `database.url`.

### Layout

```
daily_ai_automation/
├── supervisor/      orchestration, retry, state, and the policy engine
├── agents/          the two workers; they propose, they do not execute
├── integrations/    the only code that talks to Gmail, Drive, Claude, Gemini
├── repositories/    data access; owns the idempotency guarantees
├── reporting/       digest rendering and delivery
└── prompts/         versioned system prompts
```

**Structural rule:** worker code never calls a `GmailClient` write method
directly. It asks `supervisor/policies.py` for a verdict and acts on that.

---

## How duplicates are prevented

| Workflow | Key | Enforced by |
|---|---|---|
| Gmail | `message_id` | Primary key on `email_processing` |
| Occasions | `(event_type, recipient_email, event_year)` | Unique constraint on `occasion_send_log` |
| Daily run | `run_date` | Unique constraint on `automation_run` |

For greetings the send-log row is committed **before** the Gmail send, so a
crash between the two leaves a claim in place and the next run skips that
recipient rather than greeting them twice. A send that fails outright releases
the claim so tomorrow can retry.
