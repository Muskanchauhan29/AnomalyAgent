# 📊 AI Anomaly Agent

A working prototype that reads business metrics from Excel/CSV files, detects statistically significant anomalies (sudden spikes or drops), explains each one with business context, and sends automated email alerts.

Tested end-to-end and fully functional — see the demo section below.

---

## Overview

| Capability | Status |
|---|---|
| Excel/CSV reader (auto-detects columns) | ✅ Done |
| Anomaly detection (rolling z-score / % change) | ✅ Done |
| Config-driven thresholds and settings | ✅ Done |
| Dry-run testing mode | ✅ Done |
| Business-context summaries per metric | ✅ Done |
| HTML email alerts | ✅ Done |
| CSV history logging | ✅ Done |
| Gmail SMTP integration | ✅ Done |
| Daily scheduling (cron-ready) | ✅ Done |
| Optional AI-enriched summaries (Anthropic API) | Optional |

---

## How to Run

```bash
pip install -r requirements.txt

# 1. Point the config at your Excel file
#    Edit config.yaml: excel.path, date_column, metric_columns

# 2. Dry run — prints results to console, sends no email
python anomaly_agent.py --config config.yaml --dry-run

# 3. Live run — set dry_run: false in config.yaml first
export EMAIL_APP_PASSWORD="your_gmail_app_password"
export ANTHROPIC_API_KEY="sk-ant-..."   # optional, for AI-enriched summaries
python anomaly_agent.py --config config.yaml
```

You can also point the agent at any file directly, without touching the config — column detection happens automatically:

```bash
python anomaly_agent.py --excel path/to/any_file.xlsx
```

---

## Dataset Used

`data/superstore_daily_metrics.xlsx` — built from the Kaggle "Superstore Sales" dataset (2015–2018). The raw order-level records were aggregated into **daily business metrics**: `Total_Sales`, `Order_Count`, `Avg_Order_Value`, `Unique_Customers`.

This is the shape any input file needs: **one row per date**, with numeric metric columns. If you swap in your own data, keep that same structure (or let the raw-data auto-aggregation feature handle the conversion for you).

---

## Demo

**Production mode** (`detection.scan_mode: "latest"`) checks only the most recent row in the file — effectively asking "did today break the pattern?" On this dataset's final day (2018-12-30), nothing had broken pattern, so the agent correctly reported no anomaly. This is the mode used by the scheduled daily job.

**Backtest / demo mode** — to see the detector work across the *entire* history (useful for demonstrations), set `scan_mode: "all_history"` in `config.yaml` and re-run with `--dry-run`. On this dataset, it identifies roughly 38 genuine spikes across four years of data, for example:

```
📈 UP  Total_Sales — 2015-03-18
  Previous: 3,960.36  →  Latest: 28,106.72  (+609.7% vs previous period)
  Z-score: 21.02 (threshold breach)
  Business context: Total daily revenue across all orders. Drops here directly hit revenue targets.
```

**Note on thresholds:** the rolling `window` and `zscore_threshold` were tuned higher (30 days / 4.5) than typical defaults, because raw daily sales and order counts are naturally volatile — a smaller window with a lower threshold would flag ordinary day-to-day variation as false "anomalies." Smoother metrics (e.g. monthly totals) generally work well with a lower threshold (2.0–3.0).

---

## Detection Methods

- **Z-score (default)** — computes a rolling mean and standard deviation, and flags a value if it falls more standard deviations from that mean than the configured threshold. Well suited to noisy or seasonal data.
- **Percentage change** — flags a value if it differs from the previous period by more than a configured percentage. Simpler, and works well for metrics with low seasonality.

Switch between them via `detection.method` in `config.yaml`.

---

## Scheduling

Runs once per invocation; scheduling is handled externally.

- **Linux/Mac**: `chmod +x run_daily.sh` → `crontab -e` → `0 9 * * * /full/path/run_daily.sh`
- **Windows**: Task Scheduler → Daily at 9:00 AM → run `anomaly_agent.py --config config.yaml`
- **Cloud**: AWS Lambda + EventBridge, or a scheduled GitHub Actions workflow

Recommended default: **once daily, after the previous day's data is final** (e.g. 9:00 AM) — frequent enough to act on, without the noise of real-time checks.

---

## Setup Notes

1. **Gmail App Password required** (a normal Gmail password will not work):
   - Enable 2-Step Verification on your Google account
   - Generate an [App Password](https://myaccount.google.com/apppasswords)
   - `export EMAIL_APP_PASSWORD="your_16_char_app_password"`
2. Update `sender_email` and `recipients` in `config.yaml`
3. Set `dry_run: false` once you're ready to send real alerts

---

## Extending This Project

- **Multiple files/sheets**: loop over an array of configs in `main()`
- **Slack/Teams alerts**: add a `send_slack()` function alongside `send_email()`, calling a webhook
- **Persistent history**: anomalies are already appended to `anomalies_log.csv` on every run for trend analysis

---

## What I'd Improve Next

- Day-of-week / seasonal decomposition instead of a single global threshold
- Move history logging from a flat CSV to a proper database
- Support monitoring multiple files or sheets in a single run