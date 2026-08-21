#!/bin/bash
# ============================================================
# Daily runner for AI Anomaly Agent.
# Set your env vars below (or export them permanently in ~/.zshrc / ~/.bash_profile)
# then add this script to cron for a fully automated daily check.
# ============================================================

# --- Set these before running (or export in your environment / shell profile) ---
export EMAIL_APP_PASSWORD="your_MAIL_APP_PASSWORD"
export ANTHROPIC_API_KEY="sk-ant-...optional-for-AI-summary"

# --- Paths ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# --- Which Excel file to check today ---
# Point this at whatever file you want checked. Swap it any time — no code
# changes needed, columns are auto-detected if not in config.yaml.
EXCEL_FILE="data/superstore_daily_metrics.xlsx"

# --- Run the agent, log output with timestamp ---
echo "=== Run started: $(date) ===" >> agent.log
python3 anomaly_agent.py --config config.yaml --excel "$EXCEL_FILE" >> agent.log 2>&1
echo "=== Run finished: $(date) ===" >> agent.log
echo "" >> agent.log

# ============================================ ================
# TO SCHEDULE DAILY AT 9:00 AM (Linux/Mac):
#   1. chmod +x run_daily.sh
#   2. crontab -e
#   3. Add this line:
#      0 9 * * * /full/path/to/anomaly_agent/run_daily.sh
#
# TO SCHEDULE ON WINDOWS:
#   Use Task Scheduler -> Create Basic Task -> Daily 9:00 AM ->
#   Action: "python.exe" with argument "anomaly_agent.py --config config.yaml --excel data\your_file.xlsx"
#   Start in: the anomaly_agent folder
#
# TO CHANGE FREQUENCY (e.g. weekly instead of daily):
#   cron:  0 9 * * 1   (every Monday at 9 AM)
#
# TO SWITCH TO A DIFFERENT DATASET:
#   Just change EXCEL_FILE above to a new path. Columns are auto-detected.
# ============================================================