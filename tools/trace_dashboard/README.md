# OpenRag Trace / Eval Dashboard

Streamlit 内部调参工具，用于查看 `trace_*` 与 `eval_*` 表中的检索链路、评估指标和 run 对比结果。

## 本地启动

```powershell
cd tools/trace_dashboard
pip install -r requirements.txt
streamlit run app.py
```

默认读取 `POSTGRES_*` 环境变量，也支持直接设置：

```powershell
$env:TRACE_DASHBOARD_DATABASE_URL="postgresql+psycopg://openrag:openrag_pass@localhost:5432/openrag"
```

## Docker Compose

开发环境：

```powershell
cd docker
docker compose -f docker-compose.dev.yml up trace-dashboard
```

访问：

```text
http://localhost:8501
```

这是内部工具，第一版不做登录鉴权，请只在内网或受控环境暴露。
