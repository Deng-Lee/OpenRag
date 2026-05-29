from __future__ import annotations

import altair as alt
import pandas as pd


def query_type_bar(rows: pd.DataFrame, metric: str) -> alt.Chart | None:
    if rows.empty or "query_type" not in rows.columns or metric not in rows.columns:
        return None
    data = rows.dropna(subset=[metric]).copy()
    if data.empty:
        return None
    grouped = data.groupby("query_type", dropna=False)[metric].mean().reset_index()
    grouped["query_type"] = grouped["query_type"].fillna("unknown")
    return (
        alt.Chart(grouped)
        .mark_bar()
        .encode(
            x=alt.X("query_type:N", sort="-y", title="Query 类型"),
            y=alt.Y(f"{metric}:Q", title=metric),
            tooltip=["query_type", metric],
        )
        .properties(height=260)
    )


def change_type_bar(rows: pd.DataFrame) -> alt.Chart | None:
    if rows.empty or "change_type" not in rows.columns:
        return None
    grouped = rows.groupby("change_type").size().reset_index(name="count")
    return (
        alt.Chart(grouped)
        .mark_bar()
        .encode(
            x=alt.X("change_type:N", title="变化类型"),
            y=alt.Y("count:Q", title="Query 数"),
            color=alt.Color("change_type:N", legend=None),
            tooltip=["change_type", "count"],
        )
        .properties(height=240)
    )
