"""Risk map for Function 2 (RiskScoringAgent): an animated, month-by-month view of the agent's output.

Reads the log files only; it does not run any agent (CLAUDE.md: the dashboard reads the logs).
Produce the logs first, then start the app:

    python -m src.sim.demo_risk_scoring --embedded-server --months all
    streamlit run dashboard/risk_map.py

Risk indicator from published water-quality thresholds - not a CKDu diagnosis.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import folium
import pandas as pd
import streamlit as st
from folium.plugins import TimestampedGeoJson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.sim import load_data  # noqa: E402

LOGS = ROOT / "logs"
RISK_SCORES = LOGS / "risk_scores.csv"
WELL_SCORES = LOGS / "well_scores.csv"
MESSAGES = LOGS / "messages.csv"
DIVISIONS = ROOT / "data" / "raw" / "ckdu_03_sampling_design_ds_divisions.csv"

# Tiers are a state of the water against drinking-water standards, so they use the fixed status
# palette (good / warning / serious / critical). Excellent and Good share "good"; the tier name is
# always shown next to the colour (legend, popups, tables), never colour alone. Unknown is grey.
TIER_COLORS = {
    "Excellent": "#0ca30c",
    "Good": "#0ca30c",
    "Poor": "#fab219",
    "Very Poor": "#ec835a",
    "Unsuitable": "#d03b3b",
    "Unknown": "#a3a29d",
}
LEGEND = [("Excellent / Good", "#0ca30c"), ("Poor", "#fab219"), ("Very Poor", "#ec835a"),
          ("Unsuitable", "#d03b3b"), ("Unknown (no score)", "#a3a29d")]
MAP_HEIGHT = 600
# Talawa sits right next to Nochchiyagama, so its name goes below its circle instead of beside it
LABEL_ANCHOR = {"Talawa": (-6, -14)}
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DISCLAIMER = ("Risk indicator derived from published water-quality thresholds (Cooray et al. 2019). "
              "It is not a CKDu diagnosis and does not claim that well water causes CKDu.")


@st.cache_data
def load_logs(mtimes: tuple) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame | None]:
    villages = pd.read_csv(RISK_SCORES)
    wells = pd.read_csv(WELL_SCORES)
    messages = pd.read_csv(MESSAGES) if MESSAGES.exists() else None
    for df in (villages, wells):
        df["tier"] = df["tier"].fillna("Unknown")
    return villages, wells, messages


def well_positions(wells: pd.DataFrame, divisions: pd.DataFrame) -> pd.DataFrame:
    """ILLUSTRATIVE well positions: a fixed spiral around the DS division's point.

    The data has no well coordinates, so these are layout positions only (deterministic, no
    randomness), not real well locations.
    """
    centre = divisions.set_index("ds_division")[["lat", "lon"]]
    rows = []
    for ds, ids in wells.groupby("ds_division")["well_id"]:
        for i, well_id in enumerate(sorted(ids.unique())):
            angle = i * 2.39996  # golden angle, spreads points evenly
            radius = 0.024 + 0.0065 * math.sqrt(i)  # degrees, about 2.5-5.5 km: clear of the village marker
            rows.append({"well_id": well_id, "ds_division": ds,
                         "lat": centre.loc[ds, "lat"] + radius * math.sin(angle),
                         "lon": centre.loc[ds, "lon"] + radius * math.cos(angle)})
    return pd.DataFrame(rows)


def _point(lat, lon, month, color, radius, weight, popup):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "times": [f"2025-{int(month):02d}-01"],
            "popup": popup,
            "icon": "circle",
            "iconstyle": {"fillColor": color, "fillOpacity": 0.95, "stroke": True,
                          "color": "#3b3b3b", "weight": weight, "radius": radius},
        },
    }


def animated_map(villages: pd.DataFrame, wells: pd.DataFrame, divisions: pd.DataFrame) -> folium.Map:
    pos = well_positions(wells, divisions).set_index("well_id")
    centre = divisions.set_index("ds_division")
    features = []
    for w in wells.itertuples():
        p = pos.loc[w.well_id]
        wqi = "n/a" if pd.isna(w.wqi) else f"{w.wqi:.1f}"
        features.append(_point(p["lat"], p["lon"], w.month, TIER_COLORS[w.tier], 3.5, 1,
                               f"<b>{w.well_id}</b><br>WQI {wqi}<br>{w.tier}"))
    for v in villages.itertuples():
        c = centre.loc[v.ds_division]
        wqi = "n/a" if pd.isna(v.wqi) else f"{v.wqi:.1f}"
        share = "n/a" if pd.isna(v.share_poor_or_worse) else f"{v.share_poor_or_worse:.0%}"
        drivers = ", ".join(json.loads(v.drivers.replace("'", '"'))) if isinstance(v.drivers, str) else ""
        popup = (f"<b>{v.ds_division}</b> ({MONTH_NAMES[int(v.month) - 1]})<br>"
                 f"village WQI (median) {wqi}<br><b>{v.tier}</b><br>"
                 f"wells Poor or worse: {share}<br>above standard: {drivers or 'none'}")
        features.append(_point(c["lat"], c["lon"], v.month, TIER_COLORS[v.tier], 10, 2.5, popup))

    m = folium.Map(location=[divisions["lat"].mean(), divisions["lon"].mean()], zoom_start=8,
                   tiles=None, height=MAP_HEIGHT)
    # OpenStreetMap needs no API key; faded so the risk colours carry the eye
    folium.TileLayer("OpenStreetMap", opacity=0.45).add_to(m)
    m.fit_bounds([[divisions["lat"].min() - 0.08, divisions["lon"].min() - 0.08],
                  [divisions["lat"].max() + 0.08, divisions["lon"].max() + 0.08]])
    TimestampedGeoJson(
        {"type": "FeatureCollection", "features": features},
        period="P1M", duration="P27D",  # each month's marks disappear before the next month
        transition_time=900, auto_play=False, loop=True, max_speed=2, loop_button=True,
        date_options="MMMM", time_slider_drag_update=True, add_last_point=False,
    ).add_to(m)
    for ds, c in centre.iterrows():
        folium.Marker(
            [c["lat"], c["lon"]],
            icon=folium.DivIcon(icon_anchor=LABEL_ANCHOR.get(ds, (-14, 8)), html=(
                f'<div style="font:600 12px sans-serif;color:#0b0b0b;white-space:nowrap;'
                f'text-shadow:0 0 3px #fff,0 0 3px #fff">{ds}</div>')),
        ).add_to(m)
    return m


def legend() -> str:
    chips = "".join(
        f'<span style="display:inline-flex;align-items:center;margin-right:14px">'
        f'<span style="width:12px;height:12px;border-radius:50%;background:{c};'
        f'display:inline-block;margin-right:6px"></span>{t}</span>'
        for t, c in LEGEND)
    return f'<div style="font-size:13px;margin:4px 0 8px">{chips}</div>'


def message_stats(messages: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Message counts per link, and well_readings -> risk_record latency per thread."""
    sent = messages[messages["direction"] == "sent"].copy()
    sent["type"] = sent["payload"].map(lambda p: json.loads(p).get("type"))
    links = (sent.groupby(["sender", "receiver", "type"]).size().rename("messages").reset_index())
    ts = pd.to_datetime(messages["timestamp"])
    out = messages.assign(ts=ts)
    start = out[(out["direction"] == "sent") & out["payload"].str.contains('"well_readings"')]
    end = out[(out["direction"] == "received") & out["payload"].str.contains('"risk_record"')]
    lat = (end.groupby("thread_id")["ts"].min() - start.groupby("thread_id")["ts"].min()).dropna()
    return links, lat.dt.total_seconds() * 1000


def tier_cell(tier: str) -> str:
    light_text = tier in ("Excellent", "Good", "Unsuitable")
    return f"background-color:{TIER_COLORS.get(tier, '#a3a29d')};color:{'#ffffff' if light_text else '#0b0b0b'}"


def main() -> None:
    st.set_page_config(page_title="CKDu well risk map", layout="wide")
    st.title("Well-water risk map — RiskScoringAgent (Function 2)")
    st.caption(DISCLAIMER)

    if not (RISK_SCORES.exists() and WELL_SCORES.exists()):
        st.warning("No agent output yet. Run the agents first, then refresh this page:")
        st.code("python -m src.sim.demo_risk_scoring --embedded-server --months all")
        st.stop()

    mtimes = tuple(p.stat().st_mtime for p in (RISK_SCORES, WELL_SCORES) if p.exists())
    villages, wells, messages = load_logs(mtimes)
    divisions = load_data.load_ds_divisions(DIVISIONS)
    if "month" not in villages.columns or villages["month"].isna().all():
        st.error("logs/risk_scores.csv is from an older run without a month column. Re-run the demo.")
        st.stop()
    villages = villages.dropna(subset=["month"])
    months = sorted(villages["month"].astype(int).unique())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Villages (DS divisions)", villages["ds_division"].nunique())
    c2.metric("Wells", wells["well_id"].nunique())
    c3.metric("Months simulated", len(months))
    c4.metric("risk_record messages", len(villages))

    st.subheader("Animated map — press ▶ to step through the months")
    st.markdown(legend(), unsafe_allow_html=True)
    st.caption("Large circles: village tier (median well WQI). Small dots: individual wells. "
               "Well positions are illustrative (arranged around the division's point); the data "
               "has no well coordinates. Click any circle for details.")
    # full standalone HTML at a fixed height: the default _repr_html_ sizes by aspect ratio and
    # overflows the frame, hiding the southern divisions and the play/time controls
    # HTML built only from our own log files (trusted input for st.iframe)
    st.iframe(animated_map(villages, wells, divisions).get_root().render(), height=MAP_HEIGHT + 20)

    st.subheader("Village WQI by month")
    grid = villages.pivot_table(index="ds_division", columns="month", values="wqi", aggfunc="first")
    tiers = villages.pivot_table(index="ds_division", columns="month", values="tier", aggfunc="first")
    grid.columns = [MONTH_NAMES[m - 1] for m in grid.columns]
    tiers.columns = grid.columns
    st.dataframe(grid.style.format("{:.1f}", na_rep="n/a")
                 .apply(lambda _: tiers.map(tier_cell), axis=None), width="stretch")

    st.subheader("Inspect one month")
    month = st.select_slider("Month", options=months, format_func=lambda m: MONTH_NAMES[m - 1])
    vm = villages[villages["month"] == month].sort_values("wqi", ascending=False)
    cols = ["ds_division", "wqi", "tier", "drivers", "share_poor_or_worse", "worst_tier",
            "n_wells", "n_rejected", "weight_covered", "error"]
    st.dataframe(vm[[c for c in cols if c in vm.columns]].style
                 .format({"wqi": "{:.1f}", "share_poor_or_worse": "{:.0%}", "weight_covered": "{:.0%}"},
                         na_rep="n/a")
                 .apply(lambda s: s.map(tier_cell), subset=["tier"]),
                 hide_index=True, width="stretch")

    village = st.selectbox("Wells in village", sorted(vm["ds_division"]))
    wv = wells[(wells["month"] == month) & (wells["ds_division"] == village)].sort_values("wqi", ascending=False)
    st.dataframe(wv[["well_id", "wqi", "tier"]].style.format({"wqi": "{:.1f}"}, na_rep="n/a")
                 .apply(lambda s: s.map(tier_cell), subset=["tier"]),
                 hide_index=True, width="stretch")

    if messages is not None and not messages.empty:
        st.subheader("Agent messages (logs/messages.csv)")
        links, latency = message_stats(messages)
        a, b = st.columns([2, 1])
        a.dataframe(links, hide_index=True, width="stretch")
        if not latency.empty:
            b.metric("Median readings → risk_record latency", f"{latency.median():.0f} ms")
            b.metric("Slowest", f"{latency.max():.0f} ms")
            b.caption("The demo sends every month at once and the agent scores one message at a "
                      "time, so this includes queueing, not only scoring time.")


main()
