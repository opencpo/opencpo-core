-- Migration: Add demo invitation management tables
-- Created: 2026-04-18

CREATE TABLE IF NOT EXISTS ocpp.invitations (
    id SERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    name TEXT NOT NULL,
    company TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'requested',  -- requested, pending, active, revoked, expired
    credentials_user TEXT,     -- generated username for demo login
    credentials_pass TEXT,     -- generated password (stored hashed)
    token TEXT UNIQUE,         -- invitation/login token
    login_count INTEGER DEFAULT 0,
    total_page_views INTEGER DEFAULT 0,
    last_login TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,    -- 30 days after approval
    metadata JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS ocpp.page_views (
    id SERIAL PRIMARY KEY,
    invitation_id INTEGER REFERENCES ocpp.invitations(id),
    path TEXT NOT NULL,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    ip TEXT,
    user_agent TEXT
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_invitations_status ON ocpp.invitations(status);
CREATE INDEX IF NOT EXISTS idx_invitations_email ON ocpp.invitations(email);
CREATE INDEX IF NOT EXISTS idx_invitations_token ON ocpp.invitations(token);
CREATE INDEX IF NOT EXISTS idx_page_views_invitation_id ON ocpp.page_views(invitation_id);
CREATE INDEX IF NOT EXISTS idx_page_views_timestamp ON ocpp.page_views(timestamp);
