# Transactional Mailer Web UI — Rebuild Plan

## What exists in GUI that's MISSING in web:

### Campaign Tab
- [x] Start/Stop — exists but broken threading
- [ ] PAUSE (graceful, resume later)  
- [ ] Pre-Generate Logos button
- [ ] Pre-Generate Redirects button
- [ ] Blacklist Check button
- [ ] Scheduler (HH:MM with Schedule/Cancel buttons)
- [ ] Lead Preview (first N lines)
- [ ] Events Log (real-time sidebar)
- [ ] Speed in mails/sec, elapsed time, ETA
- [ ] Test Mail: Pre-check send + Interval test config
- [ ] Mailing Source: select leads file + SMTP file (now DB-based)

### HTML Editor Tab
- [ ] Template selector dropdown
- [ ] Code editor (monospace, large)
- [ ] Preview Raw HTML
- [ ] Preview Processed (variables resolved)
- [ ] Preview + AntiFingerprint
- [ ] Preview + Advanced AF
- [ ] 3-Way Diff view
- [ ] Raw MIME preview
- [ ] Text:Image Ratio analysis
- [ ] Save / Save As

### Files Tab (not needed as-is, replaced by DB uploads)
- [x] Leads import — exists
- [x] SMTP import — exists

### Logos Tab
- [ ] Logo upload (multiple files)
- [ ] Logo list with thumbnails
- [ ] Variant generation: enter lead count → calculate template count
- [ ] Generate & Preview button
- [ ] Variant info: count, avg/min/max size

### Redirects Tab
- [ ] Google Share redirect generation: target URL, count, threads
- [ ] Manual add / bulk add
- [ ] Redirect pool table (short URL + created)
- [ ] Delete/clear

### Config Tab
- [x] Thread count — exists but per-campaign
- [ ] Proxy mode: Off / Single Proxy / Proxy List  
- [ ] Proxy field + rotation
- [ ] All sending params with descriptions
- [ ] Spintax file editor (create/edit named pools)
- [ ] All path configs → now DB-based content pools

### Logs Tab
- [x] SMTP error log — exists
- [ ] Auto-refresh toggle
- [ ] Reset IN_PROGRESS button
- [ ] Delete DB button

## Architecture Changes

### Proxy handling
OLD: per-SMTP proxy in smtps.txt line
NEW: Campaign-level proxy config:
- proxy_mode: off / single / list
- proxy_value: single proxy string OR multi-line proxy list
- proxy_rotate_every: N
- SMTPs imported WITHOUT proxy (clean separation)

### Campaign structure
One campaign = one complete mailing configuration:
- Sender: from_name, from_email, subject (with pools)
- Content: template selection, AF settings, logo settings
- Delivery: SMTP selection, proxy config, threading, delays
- Testing: pre-check recipients, interval test
- Leads: imported per-campaign

## Implementation Order
1. Rewrite campaigns.py route + template (biggest piece)
2. Add HTML editor with preview endpoints
3. Add logo management
4. Add redirect management  
5. Fix proxy to be campaign-level
6. Add missing DB fields
