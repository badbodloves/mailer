# Transactional Mailer Web UI — Memory

## Architecture
- Separate FastAPI app at `transactional/web/`
- Reuses existing `mailer/` core (mailer_core.py, smtp_worker.py, etc.)
- Own SQLite DB `trans.db` — replaces config.ini
- Entry point: `transactional/web_server.py` on port 8001
- systemd: `deploy/transmailer.service`

## Implemented Features
- [x] Campaigns: create, edit all settings, start/stop, live stats, speed tracking
- [x] SMTP Management: add, edit, delete, test connection, bulk import (host,port,user,pass,proxy)
- [x] Leads: paste import, file upload, view per campaign, search, states
- [x] HTML Templates: create, edit, delete, preview with variable substitution
- [x] Content Pools: names, subjects, spintax files, alt_texts — all web-editable
- [x] Anti-fingerprint: toggle CSS classes, advanced structure, variation probability
- [x] Image/Logo: enable/disable, CID/Cloudinary mode, quantize, max colors, rotation
- [x] Redirect Links: enable/disable, target URL, rotation interval
- [x] Test Emails: pre-flight recipients, interval testing
- [x] Proxy: per-SMTP proxy, rotation config
- [x] Scheduling: HH:MM auto-start per campaign
- [x] Logs: SMTP error log viewer with auto-refresh
- [x] Auth: login/setup, session-based, PBKDF2
- [x] Auto-reset leads on start (same as bulk)
- [x] Global settings: Cloudinary, MXToolbox API key

## Remaining / Future
- [ ] Blacklist check button (MXToolbox integration in UI)
- [ ] Spintax resolution in content engine (needs proper integration)
- [ ] Logo upload + variant generation UI
- [ ] Redirect pre-generation button
- [ ] Campaign export/import
- [ ] Multi-campaign parallel execution
