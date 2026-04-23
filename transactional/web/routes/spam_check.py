"""Spam Score Checker — Rspamd or SpamAssassin integration."""
import re
import subprocess
import logging
from html import escape

logger = logging.getLogger("trans.spam")

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


def check_rspamd(raw_mime: str, url: str = "http://127.0.0.1:11333/checkv2") -> dict:
    """Check via Rspamd HTTP API. Returns {score, action, symbols, raw}."""
    if not HAS_REQUESTS:
        return {"error": "requests library not installed"}
    try:
        resp = _requests.post(url, data=raw_mime.encode("utf-8"),
                               headers={"Content-Type": "text/plain"}, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            symbols = []
            for name, info in data.get("symbols", {}).items():
                symbols.append({
                    "name": name,
                    "score": info.get("score", 0),
                    "description": info.get("description", ""),
                })
            symbols.sort(key=lambda s: abs(s["score"]), reverse=True)
            return {
                "score": data.get("score", 0),
                "action": data.get("action", "unknown"),
                "threshold": data.get("required_score", 15),
                "symbols": symbols,
                "error": None,
            }
        return {"error": f"Rspamd returned {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def check_spamassassin(raw_mime: str) -> dict:
    """Check via spamc CLI. Returns {score, symbols, raw}."""
    try:
        result = subprocess.run(
            ["spamc", "-R"],
            input=raw_mime.encode("utf-8"),
            capture_output=True, timeout=30)
        output = result.stdout.decode("utf-8", errors="replace")

        score = 0.0
        threshold = 5.0
        symbols = []

        for line in output.splitlines():
            score_match = re.match(r"^([\d.]+)/([\d.]+)", line.strip())
            if score_match:
                score = float(score_match.group(1))
                threshold = float(score_match.group(2))
                continue
            rule_match = re.match(r"^\s*([\-\d.]+)\s+(\S+)\s+(.*)", line.strip())
            if rule_match:
                symbols.append({
                    "name": rule_match.group(2),
                    "score": float(rule_match.group(1)),
                    "description": rule_match.group(3).strip(),
                })

        symbols.sort(key=lambda s: abs(s["score"]), reverse=True)
        action = "no action" if score < threshold else "reject"
        return {
            "score": score,
            "action": action,
            "threshold": threshold,
            "symbols": symbols,
            "error": None,
        }
    except FileNotFoundError:
        return {"error": "spamc not found. Install: sudo apt install spamassassin spamc"}
    except Exception as e:
        return {"error": str(e)}


def check_spam(raw_mime: str, checker: str = "rspamd",
               url: str = "http://127.0.0.1:11333/checkv2") -> dict:
    if checker == "rspamd":
        return check_rspamd(raw_mime, url)
    elif checker == "spamassassin":
        return check_spamassassin(raw_mime)
    return {"error": f"Unknown checker: {checker}"}


def format_result_html(result: dict) -> str:
    if result.get("error"):
        return f'<div class="alert alert-danger">Spam check error: {escape(result["error"])}</div>'

    score = result.get("score", 0)
    action = result.get("action", "unknown")
    threshold = result.get("threshold", 15)
    symbols = result.get("symbols", [])

    if score <= 3:
        color = "var(--green)"
        rating = "Good"
    elif score <= 6:
        color = "var(--yellow)"
        rating = "Warning"
    else:
        color = "var(--red)"
        rating = "High Risk"

    action_badge = {
        "no action": "badge-running",
        "greylist": "badge-paused",
        "add header": "badge-draft",
        "rewrite subject": "badge-draft",
        "reject": "badge-failed",
    }.get(action, "badge-draft")

    html = f'<div style="padding:12px">'
    html += f'<div style="display:flex;gap:20px;align-items:center;margin-bottom:12px">'
    html += f'<div><span style="font-size:28px;font-weight:700;color:{color}">{score:.1f}</span>'
    html += f'<span style="font-size:14px;color:var(--fg2)"> / {threshold:.0f}</span></div>'
    html += f'<div><span class="badge {action_badge}" style="font-size:13px">{action}</span>'
    html += f'<div style="font-size:12px;color:var(--fg2);margin-top:2px">{rating}</div></div>'
    html += f'</div>'

    if symbols:
        html += '<table style="font-size:12px;width:100%"><thead><tr>'
        html += '<th>Score</th><th>Rule</th><th>Description</th></tr></thead><tbody>'
        for s in symbols[:20]:
            sc = s["score"]
            sc_color = "var(--red)" if sc > 0 else "var(--green)" if sc < 0 else "var(--fg2)"
            html += f'<tr><td style="color:{sc_color};font-weight:600;white-space:nowrap">'
            html += f'{sc:+.1f}</td>'
            html += f'<td style="font-family:monospace;white-space:nowrap">{escape(s["name"])}</td>'
            html += f'<td style="color:var(--fg2)">{escape(s["description"][:80])}</td></tr>'
        if len(symbols) > 20:
            html += f'<tr><td colspan="3" style="color:var(--fg2)">... {len(symbols)-20} more rules</td></tr>'
        html += '</tbody></table>'

    html += '</div>'
    return html
