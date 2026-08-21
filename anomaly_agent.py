"""
AI Anomaly Agent
================
Reads ANY Excel file of business metrics, auto-detects the date column and
numeric metric columns, detects statistically significant up/down breaks,
writes a business-context-aware summary, and emails an alert.

Usage:
    # Point it at any Excel file — no config editing needed for a new dataset
    python anomaly_agent.py --excel path/to/your_data.xlsx

    # Use a saved config instead (for fine-tuned settings / recipients)
    python anomaly_agent.py --config config.yaml

    # Force a dry run (no email sent, just prints to console)
    python anomaly_agent.py --excel data.xlsx --dry-run
"""

import argparse
import csv
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
import yaml


# ----------------------------------------------------------------------
# 1. LOAD CONFIG
# ----------------------------------------------------------------------
def load_config(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ----------------------------------------------------------------------
# 2. AUTO-DETECT COLUMNS (so ANY excel file works without editing config)
# ----------------------------------------------------------------------
def auto_detect_columns(df):
    """Guess the date column and numeric metric columns from any dataframe."""
    date_col = None

    # 1. Look for a column already datetime-typed, or with 'date'/'time' in its name
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            date_col = col
            break
    if date_col is None:
        name_candidates = [c for c in df.columns if any(
            kw in c.lower() for kw in ["date", "time", "day", "period"])]
        for col in name_candidates:
            try:
                pd.to_datetime(df[col])
                date_col = col
                break
            except Exception:
                continue
    if date_col is None:
        # last resort: try every column, use first that parses cleanly as dates
        for col in df.columns:
            try:
                parsed = pd.to_datetime(df[col], errors="coerce")
                if parsed.notna().mean() > 0.9:
                    date_col = col
                    break
            except Exception:
                continue
    if date_col is None:
        raise ValueError(
            "Could not auto-detect a date column. Add 'date_column: <name>' "
            "under 'excel:' in your config to specify it manually."
        )

    # 2. Every other numeric column becomes a metric to monitor
    metric_cols = [
        c for c in df.columns
        if c != date_col and pd.api.types.is_numeric_dtype(df[c])
    ]
    if not metric_cols:
        raise ValueError(
            "No numeric columns found to monitor besides the date column. "
            "Check your Excel file has numeric metric columns."
        )

    return date_col, metric_cols


# ----------------------------------------------------------------------
# 3. READ EXCEL (auto-detects structure if not explicitly configured)
# ----------------------------------------------------------------------
def read_excel(cfg):
    excel_cfg = cfg["excel"]
    sheet = excel_cfg.get("sheet_name", 0)
    raw = pd.read_excel(excel_cfg["path"], sheet_name=sheet)

    date_col = excel_cfg.get("date_column")
    metric_cols = excel_cfg.get("metric_columns")

    # If configured columns don't actually exist in THIS file (e.g. config was
    # written for a different dataset), ignore them and auto-detect instead.
    if date_col and date_col not in raw.columns:
        print(f"[INFO] Configured date_column '{date_col}' not found in this file — auto-detecting instead.")
        date_col = None
    if metric_cols:
        missing = [m for m in metric_cols if m not in raw.columns]
        if missing:
            print(f"[INFO] Configured metric_columns {missing} not found in this file — auto-detecting instead.")
            metric_cols = None

    if not date_col or not metric_cols:
        detected_date, detected_metrics = auto_detect_columns(raw)
        date_col = date_col or detected_date
        metric_cols = metric_cols or detected_metrics
        print(f"[AUTO-DETECT] Date column: '{date_col}'")
        print(f"[AUTO-DETECT] Metric columns: {metric_cols}")
        # Store back into cfg so the rest of the pipeline uses the same columns
        excel_cfg["date_column"] = date_col
        excel_cfg["metric_columns"] = metric_cols

    raw[date_col] = pd.to_datetime(raw[date_col])
    df = raw.sort_values(date_col).reset_index(drop=True)
    return df


# ----------------------------------------------------------------------
# 3. DETECT ANOMALIES
# ----------------------------------------------------------------------
def _check_point(series, idx, window, min_history, method, det):
    """Check a single index in the series for anomaly. Returns (is_anomaly, prev_val, detail)."""
    val = series.iloc[idx]
    prev_val = series.iloc[idx - 1] if idx >= 1 else None
    is_anomaly, detail = False, {}

    if method == "zscore":
        hist_window = series.iloc[max(0, idx - window):idx]
        if len(hist_window) >= min_history - 1:
            mean = hist_window.mean()
            std = hist_window.std()
            if std and std > 0:
                z = (val - mean) / std
                if abs(z) > det["zscore_threshold"]:
                    is_anomaly = True
                    detail = {"method": "zscore", "zscore": round(z, 2),
                              "rolling_mean": round(mean, 2), "rolling_std": round(std, 2)}

    elif method == "pct_change":
        if prev_val not in (None, 0):
            pct = ((val - prev_val) / abs(prev_val)) * 100
            if abs(pct) > det["pct_change_threshold"]:
                is_anomaly = True
                detail = {"method": "pct_change", "pct_change": round(pct, 1)}

    return is_anomaly, prev_val, detail


def detect_anomalies(df, cfg):
    """
    scan_mode 'latest' (default, use for daily production runs): only checks
    the most recent row -> that's "did today break the pattern?"
    scan_mode 'all_history': checks every row -> useful for backtesting /
    demoing the detector against a full historical dataset.
    """
    det = cfg["detection"]
    metrics = cfg["excel"]["metric_columns"]
    date_col = cfg["excel"]["date_column"]
    window = det["window"]
    min_history = det["min_history"]
    method = det["method"]
    scan_mode = det.get("scan_mode", "latest")

    anomalies = []

    for metric in metrics:
        if metric not in df.columns:
            print(f"[WARN] Metric '{metric}' not found in Excel columns, skipping.")
            continue

        series = df[metric]
        if len(series) < min_history:
            continue

        indices = range(min_history, len(series)) if scan_mode == "all_history" else [len(series) - 1]

        for idx in indices:
            is_anomaly, prev_val, detail = _check_point(series, idx, window, min_history, method, det)
            if is_anomaly:
                val = series.iloc[idx]
                direction = "up" if val >= (prev_val if prev_val is not None else val) else "down"
                anomalies.append({
                    "metric": metric,
                    "date": df[date_col].iloc[idx],
                    "latest_value": val,
                    "previous_value": prev_val,
                    "direction": direction,
                    "detail": detail,
                })

    return anomalies


# ----------------------------------------------------------------------
# 4. WRITE SUMMARY (template-based, with optional Claude API enrichment)
# ----------------------------------------------------------------------
def get_business_context(metric, cfg):
    """Return configured context if present, else a sensible generic fallback
    so ANY new metric (from any Excel file) still gets a useful line."""
    configured = cfg.get("business_context", {}).get(metric)
    if configured:
        return configured
    readable = metric.replace("_", " ")
    return f"'{readable}' is being monitored automatically — no custom business context configured for it yet. Investigate the underlying cause of this change."


def template_summary(anomaly, cfg):
    ctx = get_business_context(anomaly["metric"], cfg)
    arrow = "📈 UP" if anomaly["direction"] == "up" else "📉 DOWN"
    prev = anomaly["previous_value"]
    latest = anomaly["latest_value"]
    change_str = ""
    if prev not in (None, 0):
        pct = ((latest - prev) / abs(prev)) * 100
        change_str = f" ({pct:+.1f}% vs previous period)"

    lines = [
        f"{arrow}  {anomaly['metric']} — {anomaly['date'].strftime('%Y-%m-%d')}",
        f"  Previous: {prev}   →   Latest: {latest}{change_str}",
    ]
    if anomaly["detail"].get("method") == "zscore":
        lines.append(f"  Z-score: {anomaly['detail']['zscore']} (threshold breach)")
    if ctx:
        lines.append(f"  Business context: {ctx}")
    return "\n".join(lines)


def ai_enrich_summary(anomalies, cfg):
    """Optional: call Claude API for a sharper, narrative summary."""
    if not cfg.get("ai_summary", {}).get("enabled"):
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[WARN] ai_summary.enabled=true but ANTHROPIC_API_KEY not set. Skipping AI enrichment.")
        return None

    try:
        import anthropic
    except ImportError:
        print("[WARN] anthropic package not installed. Run: pip install anthropic")
        return None

    client = anthropic.Anthropic(api_key=api_key)
    model = cfg["ai_summary"].get("model", "claude-sonnet-4-6")

    bullet_lines = []
    for a in anomalies:
        ctx = get_business_context(a["metric"], cfg)
        bullet_lines.append(
            f"- {a['metric']}: {a['previous_value']} -> {a['latest_value']} "
            f"({a['direction']}) on {a['date'].strftime('%Y-%m-%d')}. Context: {ctx}"
        )

    prompt = (
        "You are a business analyst. Below are detected metric anomalies with "
        "raw numbers and business context. Write a short, sharp executive summary "
        "(4-6 sentences max) explaining what likely happened and why it matters. "
        "Be direct and avoid restating raw numbers already implied.\n\n"
        + "\n".join(bullet_lines)
    )

    response = client.messages.create(
        model=model,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if hasattr(block, "text"))


# ----------------------------------------------------------------------
# 5. LOG ANOMALIES TO CSV (history, for trend-tracking over time)
# ----------------------------------------------------------------------
def log_anomalies(anomalies, log_path="anomalies_log.csv"):
    file_exists = os.path.isfile(log_path)
    with open(log_path, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["run_timestamp", "metric_date", "metric", "direction",
                              "previous_value", "latest_value", "zscore_or_pct"])
        run_ts = datetime.now().isoformat(timespec="seconds")
        for a in anomalies:
            detail = a["detail"]
            stat = detail.get("zscore", detail.get("pct_change", ""))
            writer.writerow([
                run_ts, a["date"].strftime("%Y-%m-%d"), a["metric"], a["direction"],
                a["previous_value"], a["latest_value"], stat,
            ])
    print(f"[OK] Logged {len(anomalies)} anomaly(ies) to {log_path}")


# ----------------------------------------------------------------------
# 6. BUILD HTML EMAIL
# ----------------------------------------------------------------------
def build_html_email(anomalies, cfg, ai_summary=None):
    rows_html = ""
    for a in anomalies:
        ctx = get_business_context(a["metric"], cfg)
        color = "#c0392b" if a["direction"] == "down" else "#2980b9"
        arrow = "&#9660;" if a["direction"] == "down" else "&#9650;"
        prev, latest = a["previous_value"], a["latest_value"]
        pct_str = ""
        if prev not in (None, 0):
            pct = ((latest - prev) / abs(prev)) * 100
            pct_str = f"{pct:+.1f}%"
        rows_html += f"""
        <tr>
          <td style="padding:12px;border-bottom:1px solid #eee;">
            <span style="color:{color};font-weight:600;">{arrow} {a['metric']}</span><br/>
            <span style="color:#666;font-size:13px;">{a['date'].strftime('%Y-%m-%d')}</span>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:right;">
            {prev} &rarr; <b>{latest}</b><br/>
            <span style="color:{color};font-size:13px;">{pct_str}</span>
          </td>
          <td style="padding:12px;border-bottom:1px solid #eee;color:#555;font-size:13px;">
            {ctx}
          </td>
        </tr>"""

    ai_block = ""
    if ai_summary:
        ai_block = f"""
        <div style="background:#f6f7f9;border-left:4px solid #5b5fc7;padding:14px 16px;margin-bottom:20px;border-radius:4px;">
          <div style="font-weight:600;margin-bottom:6px;color:#333;">Executive Summary</div>
          <div style="color:#444;font-size:14px;line-height:1.5;">{ai_summary}</div>
        </div>"""

    html = f"""
    <html>
    <body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f2f3f5;padding:24px;">
      <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
        <div style="background:#1a1a2e;padding:20px 24px;">
          <h2 style="color:#fff;margin:0;font-size:18px;">⚠️ Anomaly Alert — {len(anomalies)} metric(s) broke pattern</h2>
          <div style="color:#aaa;font-size:13px;margin-top:4px;">{datetime.now().strftime('%A, %d %B %Y — %H:%M')}</div>
        </div>
        <div style="padding:24px;">
          {ai_block}
          <table style="width:100%;border-collapse:collapse;">
            {rows_html}
          </table>
        </div>
        <div style="background:#fafafa;padding:14px 24px;color:#999;font-size:12px;">
          Sent automatically by AI Anomaly Agent
        </div>
      </div>
    </body>
    </html>
    """
    return html


# ----------------------------------------------------------------------
# 7. SEND EMAIL
# ----------------------------------------------------------------------
def send_email(subject, plain_body, html_body, cfg):
    email_cfg = cfg["email"]

    if cfg.get("dry_run", True) or not email_cfg.get("enabled", True):
        print("\n" + "=" * 60)
        print("[DRY RUN] Email NOT sent. Would have sent:")
        print(f"To: {', '.join(email_cfg['recipients'])}")
        print(f"Subject: {subject}")
        print("-" * 60)
        print(plain_body)
        print("=" * 60 + "\n")
        return

    password = os.environ.get("EMAIL_APP_PASSWORD")
    if not password:
        print("[ERROR] EMAIL_APP_PASSWORD env var not set. Cannot send email.")
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = email_cfg["sender_email"]
    msg["To"] = ", ".join(email_cfg["recipients"])
    msg["Subject"] = subject
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
            server.starttls()
            server.login(email_cfg["sender_email"], password)
            server.sendmail(email_cfg["sender_email"], email_cfg["recipients"], msg.as_string())
        print(f"[OK] Email sent to {len(email_cfg['recipients'])} recipient(s).")
    except Exception as e:
        print(f"[ERROR] Failed to send email: {e}")


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
DEFAULT_CFG = {
    "excel": {},  # date_column / metric_columns left empty -> auto-detected
    "business_context": {},
    "detection": {
        "scan_mode": "latest",
        "method": "zscore",
        "window": 30,
        "zscore_threshold": 4.5,
        "pct_change_threshold": 20,
        "min_history": 30,
    },
    "ai_summary": {"enabled": False},
    "email": {"enabled": False},
    "dry_run": True,
}


def main():
    parser = argparse.ArgumentParser(description="AI Anomaly Agent")
    parser.add_argument("--config", default="config.yaml",
                         help="Path to config.yaml (email/thresholds/context). "
                              "If missing, runs with safe defaults + auto-detected columns.")
    parser.add_argument("--excel", help="Path to any Excel file to check. "
                                         "Overrides excel.path in the config. "
                                         "Date column and metric columns are auto-detected "
                                         "if not specified in the config.")
    parser.add_argument("--sheet", help="Sheet name/index to read (overrides config).")
    parser.add_argument("--dry-run", action="store_true",
                         help="Force dry run: never sends email, just prints to console.")
    args = parser.parse_args()

    if os.path.exists(args.config):
        cfg = load_config(args.config)
    else:
        print(f"[INFO] No config file at '{args.config}' — using safe defaults "
              f"(dry-run, auto-detected columns).")
        cfg = DEFAULT_CFG.copy()

    cfg.setdefault("excel", {})
    if args.excel:
        cfg["excel"]["path"] = args.excel
    if args.sheet:
        cfg["excel"]["sheet_name"] = args.sheet
    if "path" not in cfg["excel"]:
        print("[ERROR] No Excel file specified. Use --excel path/to/file.xlsx "
              "or set excel.path in your config.")
        sys.exit(1)

    # --dry-run flag always wins (safety). Otherwise use config's dry_run
    # (which you set to false once, in config.yaml, so you never have to touch it again).
    if args.dry_run:
        cfg["dry_run"] = True

    print(f"[{datetime.now().isoformat()}] Reading Excel: {cfg['excel']['path']}")
    df = read_excel(cfg)
    print(f"Loaded {len(df)} rows.")

    anomalies = detect_anomalies(df, cfg)

    if not anomalies:
        print("No anomalies detected. All metrics within normal range.")
        return

    print(f"\n{len(anomalies)} anomaly(ies) detected!\n")

    # Try AI-enriched summary first, fall back to template
    ai_summary = ai_enrich_summary(anomalies, cfg)

    body_parts = []
    if ai_summary:
        body_parts.append("EXECUTIVE SUMMARY:\n" + ai_summary + "\n\n" + "-" * 60 + "\n")

    body_parts.append("DETECTED ANOMALIES:\n")
    for a in anomalies:
        body_parts.append(template_summary(a, cfg))
        body_parts.append("")

    plain_body = "\n".join(body_parts)
    html_body = build_html_email(anomalies, cfg, ai_summary)
    subject_prefix = cfg.get("email", {}).get("subject_prefix", "[Anomaly Alert]")
    subject = f"{subject_prefix} {len(anomalies)} metric(s) broke pattern — {datetime.now().strftime('%Y-%m-%d')}"

    print(plain_body)
    log_anomalies(anomalies)
    send_email(subject, plain_body, html_body, cfg)


if __name__ == "__main__":
    main()