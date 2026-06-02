-- Seed: Default admin invitation
-- Creates the default demo admin account: admin / cpoadmin

INSERT INTO ocpp.invitations (email, name, company, status, credentials_user, credentials_pass, token)
SELECT 'admin@opencpo.io', 'Admin', 'OpenCPO Demo', 'active', 'admin', 'bb2ca3d2da7697276ed629be5879f3ad5bd8b1fdfd3ec598cf483ccd2c64a15f', 'demo-admin-token'
WHERE NOT EXISTS (SELECT 1 FROM ocpp.invitations WHERE credentials_user = 'admin');
