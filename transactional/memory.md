# Transactional Web UI — Kompletter Rebuild-Plan

## Grundprinzip
Die Web-UI bildet JEDE config.ini Option + JEDES GUI-Feature ab.
Keine Proxies in SMTP-Imports. Keine Leads in Campaigns. Saubere Trennung.

## Sidebar (= alte GUI Tabs)
1. **Campaign** — Mailing starten/stoppen, Live-Stats, Konfiguration
2. **HTML Editor** — Templates verwalten, alle Previews
3. **Macros** — Spintax-Pools + File-Import (Betreffe.txt → Macro "Betreffe")
4. **SMTP** — SMTP-Listen verwalten (OHNE Proxy), Bulk-Import, Checker
5. **Leads** — Lead-Listen verwalten, Import, Bulk-Upload
6. **Logos** — Logo-Upload, Varianten generieren
7. **Redirects** — Google Share Links generieren, manuell hinzufügen
8. **Config** — ALLE Einstellungen (Proxy, Sending, Content, Image, Redirect)
9. **Logs** — SMTP Error Log, Events

## Seite: Campaign (/campaigns)
Kein Erstell-Formular mit Details. Eine Campaign = ein Versand-Lauf.
Zum Starten wählt man:
- SMTP-Liste (Dropdown aus DB)
- Lead-Liste (Dropdown aus DB)  
- HTML Template(s) (Multi-Select oder alle nutzen)
- Absendername: Feld ODER Macro-Referenz {from_name} → zieht aus Macro "from_name"
- Absender-Email: Feld oder leer = SMTP account email
- Betreff: Feld ODER Macro-Referenz {subject} → zieht aus Macro "subject"
- Alle Config-Einstellungen werden aus der globalen Config gezogen

Controls:
- START / PAUSE / STOP
- Pre-Generate Logos
- Pre-Generate Redirects  
- Blacklist Check
- Schedule (HH:MM)

Live Stats:
- Progress bar
- Total / Sent / Failed / Pending
- Speed (mails/sec)
- Elapsed time / ETA
- Lead Preview (erste 8)

## Seite: HTML Editor (/templates)
- Template-Liste (Dropdown/Accordions)
- Großer Code-Editor (monospace)
- Buttons: Preview Raw | Preview Processed | Preview + AF | Preview + Advanced AF | 3-Way Diff | Raw MIME | Text:Image Ratio
- Save / Save As / Delete
- Upload HTML-Dateien (Multi-Upload)
- HTML Rotate Every N → Konfiguration

## Seite: Macros (/macros) — NEU!
Ersetzt "Content Pools" komplett.
- Macro-Liste: Name + Werte (Zeilen)
- Import von .txt Files: Dateiname = Macro-Name (Betreffe.txt → {Betreffe})
- Bulk-Import: mehrere .txt Dateien gleichzeitig
- Standard-Macros die die Engine kennt:
  - {from_name} → Names pool
  - {subject} → Subjects pool
  - Beliebige {TAG} → Spintax file TAG.txt
  - {email}, {email_user}, {domain} → Built-in
  - [RANDSTR:N:charset:case] → Built-in
- Edit: Werte bearbeiten (textarea, eine pro Zeile)
- Jeder Macro hat: Name, Werte (one per line), Rotate Every
- Alt-Texts als eigener Macro

## Seite: SMTP (/smtps)
- SMTP-LISTEN (nicht einzelne SMTPs!)
  - Liste erstellen: Name + Bulk-Import (host,port,user,pass)
  - KEIN Proxy im Import-Format!
  - Mehrere Listen möglich
  - Pro Liste: Anzahl Accounts, Edit, Delete
  - Bulk Checker mit Threads
- Einzelne SMTPs innerhalb einer Liste editierbar

## Seite: Leads (/leads)
- Lead-LISTEN (wie beim Bulk Mailer)
  - Liste erstellen: Name + Bulk-Import / File-Upload
  - Mehrere Listen möglich
  - Pro Liste: Lead-Count, Browse, Delete
  - Exclude Rules beim Import

## Seite: Config (/config) — Alles aus config.ini
### [sending]
- threads (1-200, default 40)
- normal_delay (default 0.3)
- provider_delay (default 6.0)
- warmup_delay (default 30.0)
- warmup_count (default 5)
- smtp_timeout (default 30)
- ignore_ssl_errors (toggle)
- schedule_time (HH:MM)

### Proxy (eigener Bereich in Config)
- Mode: Off / Single / List
- Proxy-Feld (single) oder Textarea (list)
- Rotate Every N
- Formate: ip:port:user:pass | user:pass@ip:port | socks5://ip:port

### [content]
- antifingerprint_classes (toggle)
- advanced_antifingerprint (toggle)
- structure_variation (0.0-1.0)

### [IMAGE_API]
- enabled (toggle)
- mode: cid / cloudinary
- quantize (toggle)
- downscale (toggle)
- logo_max_colors (2-256)
- logo_rotate_every

### [CLOUDINARY]
- cloud_name
- api_key
- api_secret

### [redirect]
- enabled (toggle)
- target_url
- rotate_every
- gen_threads

### [test]
- test_recipients
- test_interval
- interval_recipients

### [sender]
- from_name (default, or use {from_name} macro)
- from_email (default, empty = SMTP account)
- subject (default, or use {subject} macro)

## DB-Änderungen
- trans_smtp_lists: id, name, created_at
- trans_smtps: + list_id FK
- trans_lead_lists: id, name, file_origin, lead_count, created_at
- trans_leads: campaign_id → list_id
- trans_macros: id, name, values_text, rotate_every
- trans_config: alle Settings als JSON (eine Row)
- trans_campaigns: referenziert smtp_list_id, lead_list_id, speichert status/stats

## Implementation Order
1. DB rewrite — neue Tabellen
2. Config page — alle Einstellungen
3. SMTP page — Listen-basiert
4. Leads page — Listen-basiert
5. Macros page — mit File-Import
6. HTML Editor — mit allen Previews + HTML Rotation
7. Campaign page — alles zusammen, Start/Stop
8. Campaign runner — nutzt config + Listen + Macros
