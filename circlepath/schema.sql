PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
 username TEXT PRIMARY KEY, display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
 is_manager INTEGER NOT NULL DEFAULT 0, trained_roles TEXT NOT NULL DEFAULT '[]',
 confidentiality INTEGER NOT NULL DEFAULT 0, email TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cases (
 id TEXT PRIMARY KEY, alias TEXT NOT NULL, briefing TEXT NOT NULL, report TEXT NOT NULL DEFAULT '',
 report_policy TEXT NOT NULL DEFAULT 'office-only', stage TEXT NOT NULL,
 prior_stage TEXT, ai_allowed INTEGER NOT NULL DEFAULT 0, is_synthetic INTEGER NOT NULL DEFAULT 1,
 readiness INTEGER NOT NULL DEFAULT 0, guardian_required INTEGER NOT NULL DEFAULT 0,
 support_needs TEXT NOT NULL DEFAULT '', closure_reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 username TEXT NOT NULL REFERENCES users(username), role TEXT NOT NULL,
 status TEXT NOT NULL, conflict_note TEXT NOT NULL DEFAULT '', reviewed INTEGER NOT NULL DEFAULT 0,
 UNIQUE(case_id, username)
);
CREATE TABLE IF NOT EXISTS consents (
 case_id TEXT NOT NULL REFERENCES cases(id), party TEXT NOT NULL, status TEXT NOT NULL,
 recorded_by TEXT NOT NULL REFERENCES users(username), updated_at TEXT NOT NULL,
 PRIMARY KEY(case_id, party)
);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), title TEXT NOT NULL,
 owner TEXT NOT NULL REFERENCES users(username), due_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'Open', origin TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 author TEXT NOT NULL REFERENCES users(username), stage TEXT NOT NULL,
 happened TEXT NOT NULL, agreed TEXT NOT NULL, barriers TEXT NOT NULL,
 private INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), task TEXT NOT NULL,
 body TEXT NOT NULL, sources TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pending',
 provider TEXT NOT NULL, model TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(username),
 reviewed_by TEXT, review_note TEXT NOT NULL DEFAULT '', trace_id TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repairs (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), commitment TEXT NOT NULL,
 owner TEXT NOT NULL REFERENCES users(username), due_at TEXT NOT NULL,
 verification_plan TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Agreed',
 evidence TEXT NOT NULL DEFAULT '', agreement_confirmed INTEGER NOT NULL,
 verified_by TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS availability (
 username TEXT NOT NULL REFERENCES users(username), start_at TEXT NOT NULL, end_at TEXT NOT NULL,
 PRIMARY KEY(username, start_at), CHECK(end_at > start_at)
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), kind TEXT NOT NULL,
 start_at TEXT NOT NULL, end_at TEXT NOT NULL, location TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'Scheduled', sequence INTEGER NOT NULL DEFAULT 0,
 created_by TEXT NOT NULL REFERENCES users(username), updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 recipient TEXT NOT NULL REFERENCES users(username), kind TEXT NOT NULL,
 message TEXT NOT NULL, dedupe TEXT NOT NULL UNIQUE, read_at TEXT,
 email_status TEXT NOT NULL DEFAULT 'Disabled', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_members (
 event_id TEXT NOT NULL REFERENCES events(id), username TEXT NOT NULL REFERENCES users(username),
 status TEXT NOT NULL DEFAULT 'Invited', PRIMARY KEY(event_id, username)
);
CREATE TABLE IF NOT EXISTS audit (
 id TEXT PRIMARY KEY, case_id TEXT, actor TEXT NOT NULL, action TEXT NOT NULL,
 subject_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learnings (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 lesson TEXT NOT NULL, approved_by TEXT NOT NULL REFERENCES users(username), created_at TEXT NOT NULL
);
