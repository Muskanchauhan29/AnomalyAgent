"""
AI Anomaly Agent — Streamlit Frontend
======================================
Upload ANY Excel file, auto-detects date/metric columns, offers to
auto-aggregate raw transactional data (multiple rows per date) into daily
totals, runs anomaly detection, and shows results with charts + email alert.

Run locally:
    streamlit run app.py

Deploy for free (so anyone can use it via a link):
    1. Push this folder to a GitHub repo (must include app.py, anomaly_agent.py,
       requirements.txt)
    2. Go to https://share.streamlit.io -> "New app" -> connect your repo
    3. Set main file path to "app.py" -> Deploy
    (Email sending in the deployed app needs EMAIL_APP_PASSWORD added as a
    "Secret" in the Streamlit Cloud app settings, not committed to the repo.)
"""

import io
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from anomaly_agent import (
    auto_detect_columns,
    _check_point,
    get_business_context,
    build_html_email,
    send_email,
)

st.set_page_config(page_title="AI Anomaly Agent", page_icon="📊", layout="wide")

st.title("📊 AI Anomaly Agent")
st.caption("Upload any Excel or CSV file of business metrics — the agent finds the date column, "
           "the metrics, and flags anything that broke pattern.")

# ---------------------------------------------------------------------
# Sidebar: settings
# ---------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Detection settings")
    scan_mode = st.radio(
        "Scan mode",
        ["all_history", "latest"],
        format_func=lambda x: "Full history (find every anomaly)" if x == "all_history"
        else "Latest row only (production check)",
    )
    method = st.selectbox("Method", ["zscore", "pct_change"])
    window = st.slider("Rolling window (days)", 5, 60, 30)
    zscore_threshold = st.slider("Z-score threshold", 1.5, 6.0, 4.5, 0.1)
    pct_change_threshold = st.slider("% change threshold", 5, 200, 20)
    min_history = st.slider("Minimum history needed", 5, 60, 30)

    st.divider()
    st.header("📧 Email alert (optional)")
    enable_email = st.checkbox("Send email if anomalies found")
    sender_email = st.text_input("Sender Gmail", value="", disabled=not enable_email)
    recipient_email = st.text_input("Recipient email", value="", disabled=not enable_email)
    app_password = st.text_input("Gmail App Password", value="", type="password",
                                  disabled=not enable_email,
                                  help="Create one at myaccount.google.com/apppasswords")

det_cfg = {
    "scan_mode": scan_mode,
    "method": method,
    "window": window,
    "zscore_threshold": zscore_threshold,
    "pct_change_threshold": pct_change_threshold,
    "min_history": min_history,
}

# ---------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------
uploaded = st.file_uploader("Upload Excel or CSV file", type=["xlsx", "xls", "csv"])

if uploaded is not None:
    file_ext = uploaded.name.rsplit(".", 1)[-1].lower()
    try:
        if file_ext == "csv":
            try:
                raw = pd.read_csv(uploaded)
            except UnicodeDecodeError:
                uploaded.seek(0)
                raw = pd.read_csv(uploaded, encoding="latin-1")
        else:
            raw = pd.read_excel(uploaded)
    except Exception as e:
        st.error(f"Could not read this file: {e}")
        st.stop()

    st.success(f"Loaded **{len(raw)} rows**, **{len(raw.columns)} columns** from `{uploaded.name}`")

    with st.expander("Preview data"):
        st.dataframe(raw.head(10), use_container_width=True)

    # --- Auto-detect columns, but let the user override ---
    try:
        detected_date, detected_metrics = auto_detect_columns(raw)
    except ValueError as e:
        st.error(str(e))
        st.stop()

    col1, col2 = st.columns([1, 2])
    with col1:
        date_col = st.selectbox("Date column", raw.columns, index=list(raw.columns).index(detected_date))
    with col2:
        metric_cols = st.multiselect("Metric columns to monitor", raw.columns.tolist(),
                                      default=detected_metrics)

    # --- Detect if this is raw/transactional data (multiple rows per date) ---
    # and offer to auto-aggregate into one row per date, so ANY excel works —
    # not just ones already shaped as daily metrics.
    dup_dates = raw[date_col].duplicated().sum()
    aggregate = False
    agg_func = "sum"
    if dup_dates > 0:
        st.warning(
            f"⚠️ This file has multiple rows per date ({dup_dates} duplicate dates) — looks like "
            f"raw/transactional data, not daily totals. The agent can auto-aggregate it into one "
            f"row per date before checking for anomalies."
        )
        c1, c2 = st.columns([1, 1])
        with c1:
            aggregate = st.checkbox("Aggregate to one row per date", value=True)
        with c2:
            agg_func = st.selectbox("Aggregation", ["sum", "mean", "count"],
                                     help="sum = daily totals (e.g. total sales), "
                                          "mean = daily average, count = number of rows/orders per day",
                                     disabled=not aggregate)

    business_context_inputs = {}
    with st.expander("✏️ Add business context per metric (optional, improves the summary)"):
        for m in metric_cols:
            business_context_inputs[m] = st.text_input(f"Context for {m}", key=f"ctx_{m}")

    run = st.button("🔍 Detect anomalies", type="primary")

    # Run detection and STORE results in session_state so they survive the
    # page rerun that happens when the "Send email" button is clicked later.
    if run:
        df = raw.copy()
        df[date_col] = pd.to_datetime(df[date_col])

        if aggregate:
            if agg_func == "count":
                df = df.groupby(date_col).size().reset_index(name=f"{metric_cols[0]}_count") \
                    if metric_cols else df.groupby(date_col).size().reset_index(name="Row_Count")
            else:
                df = df.groupby(date_col)[metric_cols].agg(agg_func).reset_index()
            metric_cols_run = [c for c in df.columns if c != date_col]
        else:
            metric_cols_run = metric_cols

        df = df.sort_values(date_col).reset_index(drop=True)

        cfg = {
            "excel": {"date_column": date_col, "metric_columns": metric_cols_run},
            "business_context": {k: v for k, v in business_context_inputs.items() if v},
            "detection": det_cfg,
        }

        anomalies = []
        for metric in metric_cols_run:
            series = df[metric]
            if len(series) < min_history:
                continue
            indices = range(min_history, len(series)) if scan_mode == "all_history" else [len(series) - 1]
            for idx in indices:
                is_anomaly, prev_val, detail = _check_point(series, idx, window, min_history, method, det_cfg)
                if is_anomaly:
                    val = series.iloc[idx]
                    direction = "up" if val >= (prev_val if prev_val is not None else val) else "down"
                    anomalies.append({
                        "metric": metric, "date": df[date_col].iloc[idx],
                        "latest_value": val, "previous_value": prev_val,
                        "direction": direction, "detail": detail,
                    })

        # Persist everything needed to render results + send email later
        st.session_state["results"] = {
            "df": df, "date_col": date_col, "metric_cols": metric_cols_run,
            "cfg": cfg, "anomalies": anomalies,
        }

    # --- Render results from session_state (persists across reruns) ---
    if "results" in st.session_state:
        res = st.session_state["results"]
        df, date_col_r, metric_cols_r = res["df"], res["date_col"], res["metric_cols"]
        cfg, anomalies = res["cfg"], res["anomalies"]

        st.divider()

        if not anomalies:
            st.info("✅ No anomalies detected. All metrics within normal range.")
        else:
            st.warning(f"⚠️ **{len(anomalies)} anomaly(ies) detected**")

            # --- Results table ---
            table_rows = []
            for a in anomalies:
                ctx = get_business_context(a["metric"], cfg)
                pct = None
                if a["previous_value"] not in (None, 0):
                    pct = ((a["latest_value"] - a["previous_value"]) / abs(a["previous_value"])) * 100
                table_rows.append({
                    "Date": a["date"].strftime("%Y-%m-%d"),
                    "Metric": a["metric"],
                    "Direction": "📈 UP" if a["direction"] == "up" else "📉 DOWN",
                    "Previous": a["previous_value"],
                    "Latest": a["latest_value"],
                    "% Change": f"{pct:+.1f}%" if pct is not None else "",
                    "Z-score": a["detail"].get("zscore", ""),
                    "Business context": ctx,
                })
            results_df = pd.DataFrame(table_rows)
            st.dataframe(results_df, use_container_width=True, hide_index=True)

            # --- Charts, one per metric with anomalies marked ---
            st.subheader("📈 Charts")
            anomaly_metrics = sorted({a["metric"] for a in anomalies})
            for metric in anomaly_metrics:
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=df[date_col_r], y=df[metric], mode="lines", name=metric,
                    line={"color": "#5b5fc7"},
                ))
                m_anomalies = [a for a in anomalies if a["metric"] == metric]
                fig.add_trace(go.Scatter(
                    x=[a["date"] for a in m_anomalies],
                    y=[a["latest_value"] for a in m_anomalies],
                    mode="markers", name="Anomaly",
                    marker={"color": "#c0392b", "size": 10, "symbol": "x"},
                ))
                fig.update_layout(title=metric, height=320, margin={"t": 40, "b": 20})
                st.plotly_chart(fig, use_container_width=True)

            # --- Download CSV ---
            csv_buf = io.StringIO()
            results_df.to_csv(csv_buf, index=False)
            st.download_button("⬇️ Download results as CSV", csv_buf.getvalue(),
                                file_name="anomalies_detected.csv", mime="text/csv")

            # --- Email ---
            if enable_email:
                if not (sender_email and recipient_email and app_password):
                    st.error("Fill in sender email, recipient email, and app password in the sidebar to send an alert.")
                else:
                    if st.button("📧 Send email alert now"):
                        os.environ["EMAIL_APP_PASSWORD"] = app_password
                        email_cfg = {
                            "dry_run": False,
                            "email": {
                                "enabled": True, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                                "sender_email": sender_email, "recipients": [recipient_email],
                                "subject_prefix": "[Anomaly Alert]",
                            },
                            "business_context": cfg["business_context"],
                        }
                        html_body = build_html_email(anomalies, email_cfg)
                        plain_body = "\n\n".join(
                            f"{a['metric']} {a['direction']} on {a['date'].strftime('%Y-%m-%d')}: "
                            f"{a['previous_value']} -> {a['latest_value']}"
                            for a in anomalies
                        )
                        subject = f"[Anomaly Alert] {len(anomalies)} metric(s) broke pattern"
                        try:
                            with st.spinner("Sending..."):
                                send_email(subject, plain_body, html_body, email_cfg)
                            st.success(f"✅ Email sent to {recipient_email}!")
                        except Exception as e:
                            st.error(f"Failed to send email: {e}")
else:
    st.session_state.pop("results", None)
    st.info("👆 Upload an Excel file to get started. Works with any daily/weekly business data — "
            "including raw transactional data, which the agent can auto-aggregate into daily totals.")