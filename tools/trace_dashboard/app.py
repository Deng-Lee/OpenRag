from __future__ import annotations

import pandas as pd
import streamlit as st

from charts import change_type_bar, query_type_bar
from db import get_engine
from queries import (
    DISPLAY_METRICS,
    compare_query_results,
    extract_metric,
    flatten_metrics,
    get_eval_judgments,
    get_query_results,
    get_run_summary,
    get_trace_run,
    get_trace_snapshots,
    get_trace_spans,
    list_datasets,
    list_eval_runs,
    list_workspaces,
)


st.set_page_config(page_title="OpenRag Trace Dashboard", layout="wide")


@st.cache_data(ttl=30)
def cached_workspaces() -> pd.DataFrame:
    return list_workspaces(get_engine())


@st.cache_data(ttl=30)
def cached_datasets(workspace_id: int | None) -> pd.DataFrame:
    return list_datasets(workspace_id, get_engine())


@st.cache_data(ttl=30)
def cached_runs(workspace_id: int | None, dataset_id: int | None) -> pd.DataFrame:
    return list_eval_runs(workspace_id, dataset_id, get_engine())


@st.cache_data(ttl=30)
def cached_summary(eval_run_id: int, source_scope: str) -> dict:
    return get_run_summary(eval_run_id, source_scope, get_engine())


@st.cache_data(ttl=30)
def cached_query_results(eval_run_id: int, source_scope: str) -> pd.DataFrame:
    return get_query_results(eval_run_id, source_scope, get_engine())


@st.cache_data(ttl=30)
def cached_trace(trace_id: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    engine = get_engine()
    return (
        get_trace_run(trace_id, engine),
        get_trace_spans(trace_id, engine),
        get_trace_snapshots(trace_id, None, engine),
    )


def choose_row(label: str, frame: pd.DataFrame, name_columns: list[str]) -> int | None:
    if frame.empty:
        st.info(f"暂无可选的 {label}")
        return None
    labels: dict[str, int] = {}
    for row in frame.to_dict(orient="records"):
        parts = [str(row.get(col) or "") for col in name_columns if row.get(col) is not None]
        display = " / ".join(parts) or f"#{row['id']}"
        labels[f"#{row['id']} · {display}"] = int(row["id"])
    return st.selectbox(label, list(labels.keys()), format_func=str, key=label).split(" · ", 1)[0].lstrip("#")


def metric_cards(metrics: dict) -> None:
    cols = st.columns(len(DISPLAY_METRICS))
    for col, metric in zip(cols, DISPLAY_METRICS):
        value = extract_metric(metrics, metric)
        if isinstance(value, (int, float)):
            col.metric(metric, f"{value:.4f}")
        elif value is None:
            col.metric(metric, "-")
        else:
            col.metric(metric, str(value))


def render_trace_detail(trace_id: str, eval_query_id: int | None = None) -> None:
    if not trace_id:
        st.info("请选择包含 trace_id 的 query。")
        return

    run, spans, snapshots = cached_trace(trace_id)
    st.subheader(f"Trace 详情 · {trace_id}")
    if run.empty:
        st.warning("没有找到 trace_runs 记录。")
    else:
        st.dataframe(run, use_container_width=True, hide_index=True)

    st.markdown("#### Span 时间线")
    if spans.empty:
        st.info("没有 trace_spans。")
    else:
        st.dataframe(
            spans[["stage", "status", "duration_ms", "started_at", "ended_at", "error_message"]],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("#### Top50 快照")
    if snapshots.empty:
        st.info("没有 trace_snapshots。")
    else:
        stages = ["全部"] + sorted(snapshots["stage"].dropna().unique().tolist())
        stage = st.selectbox("Stage", stages)
        view = snapshots if stage == "全部" else snapshots[snapshots["stage"] == stage]
        st.dataframe(view, use_container_width=True, hide_index=True)

    if eval_query_id:
        st.markdown("#### Eval Judgment")
        judgments = get_eval_judgments(eval_query_id, get_engine())
        if judgments.empty:
            st.info("该 query 没有关联 judgment。")
        else:
            st.dataframe(judgments, use_container_width=True, hide_index=True)


def eval_overview() -> None:
    st.header("Eval Run 总览")
    source_scope = st.sidebar.selectbox(
        "Source scope", ["weighted_all", "gold_manual", "business_import", "llm_assisted"]
    )
    workspaces = cached_workspaces()
    workspace_id = None
    if not workspaces.empty:
        workspace_options = {"全部": None} | {
            f"#{row.id} · {row.name}": int(row.id) for row in workspaces.itertuples()
        }
        workspace_id = st.sidebar.selectbox("Workspace", list(workspace_options.keys()))
        workspace_id = workspace_options[workspace_id]

    datasets = cached_datasets(workspace_id)
    dataset_id = None
    if not datasets.empty:
        dataset_options = {"全部": None} | {
            f"#{row.id} · {row.name}": int(row.id) for row in datasets.itertuples()
        }
        dataset_label = st.sidebar.selectbox("Dataset", list(dataset_options.keys()))
        dataset_id = dataset_options[dataset_label]

    runs = cached_runs(workspace_id, dataset_id)
    if runs.empty:
        st.info("暂无 eval run。")
        return

    st.dataframe(runs, use_container_width=True, hide_index=True)
    run_options = {f"#{row.id} · {row.name or row.dataset_name or 'unnamed'}": int(row.id) for row in runs.itertuples()}
    eval_run_id = st.selectbox("查看 Run", list(run_options.keys()))
    selected_run_id = run_options[eval_run_id]

    metrics = cached_summary(selected_run_id, source_scope)
    metric_cards(metrics)

    query_rows = flatten_metrics(cached_query_results(selected_run_id, source_scope))
    if query_rows.empty:
        st.info("该 run 暂无 query 级结果。")
        return

    chart = query_type_bar(query_rows, "ndcg@10")
    if chart is not None:
        st.altair_chart(chart, use_container_width=True)

    st.subheader("Query 明细")
    filters = st.columns(4)
    only_failed = filters[0].checkbox("仅失败")
    only_zero_hit = filters[1].checkbox("仅 zero-hit")
    max_ndcg = filters[2].number_input("NDCG@10 低于", min_value=0.0, max_value=1.0, value=1.0)
    max_recall = filters[3].number_input("Recall@50 低于", min_value=0.0, max_value=1.0, value=1.0)

    view = query_rows.copy()
    if only_failed and "status" in view.columns:
        view = view[view["status"] != "success"]
    if only_zero_hit and "zero_hit" in view.columns:
        view = view[view["zero_hit"] == True]  # noqa: E712
    if "ndcg@10" in view.columns:
        view = view[view["ndcg@10"].fillna(0) <= max_ndcg]
    if "recall@50" in view.columns:
        view = view[view["recall@50"].fillna(0) <= max_recall]

    st.dataframe(view, use_container_width=True, hide_index=True)
    trace_candidates = view.dropna(subset=["trace_id"]) if "trace_id" in view.columns else pd.DataFrame()
    if not trace_candidates.empty:
        selected = st.selectbox(
            "下钻 Trace",
            trace_candidates.index.tolist(),
            format_func=lambda idx: f"{trace_candidates.loc[idx, 'eval_query_id']} · {trace_candidates.loc[idx, 'query_text'][:80]}",
        )
        render_trace_detail(
            str(trace_candidates.loc[selected, "trace_id"]),
            int(trace_candidates.loc[selected, "eval_query_id"]),
        )


def trace_search() -> None:
    st.header("Trace 查询")
    trace_id = st.text_input("trace_id")
    if st.button("查询 Trace", type="primary") and trace_id:
        render_trace_detail(trace_id)


def run_compare() -> None:
    st.header("Run 对比")
    runs = cached_runs(None, None)
    if len(runs) < 2:
        st.info("至少需要两个 eval run 才能对比。")
        return
    options = {f"#{row.id} · {row.name or row.dataset_name or 'unnamed'}": int(row.id) for row in runs.itertuples()}
    left, right, metric_col = st.columns(3)
    baseline_id = options[left.selectbox("Baseline", list(options.keys()))]
    candidate_id = options[right.selectbox("Candidate", list(options.keys()))]
    metric = metric_col.selectbox("指标", ["ndcg@10", "recall@50", "precision@10", "mrr@50", "map@50"])

    source_scope = st.selectbox("Source scope", ["weighted_all", "gold_manual", "business_import", "llm_assisted"])
    baseline_summary = cached_summary(baseline_id, source_scope)
    candidate_summary = cached_summary(candidate_id, source_scope)
    st.subheader("Summary Delta")
    cols = st.columns(5)
    for col, item in zip(cols, ["ndcg@10", "recall@50", "precision@10", "mrr@50", "map@50"]):
        base_value = extract_metric(baseline_summary, item)
        cand_value = extract_metric(candidate_summary, item)
        delta = None if base_value is None or cand_value is None else float(cand_value) - float(base_value)
        col.metric(item, "-" if cand_value is None else f"{float(cand_value):.4f}", None if delta is None else f"{delta:+.4f}")

    compared = compare_query_results(
        cached_query_results(baseline_id, source_scope),
        cached_query_results(candidate_id, source_scope),
        metric=metric,
        threshold=0.01,
    )
    chart = change_type_bar(compared)
    if chart is not None:
        st.altair_chart(chart, use_container_width=True)
    st.dataframe(compared, use_container_width=True, hide_index=True)


def main() -> None:
    st.title("OpenRag Trace / Eval 内部调参工具")
    st.caption("直接读取 PostgreSQL trace/eval 表，仅建议在内网或受控环境使用。")
    page = st.sidebar.radio("页面", ["Eval Run 总览", "Run 对比", "Trace 查询"])
    try:
        if page == "Eval Run 总览":
            eval_overview()
        elif page == "Run 对比":
            run_compare()
        else:
            trace_search()
    except Exception as exc:
        st.error(f"读取 trace/eval 数据失败：{exc}")
        st.info("请确认 POSTGRES_* 环境变量、数据库连通性，以及 trace/eval migration 已执行。")


if __name__ == "__main__":
    main()
