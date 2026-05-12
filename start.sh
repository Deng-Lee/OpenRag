#!/bin/bash
# OpenRag 本地启动脚本

echo "🚀 OpenRag 本地启动"
echo "===================="
echo ""

# 检查 Docker 服务
echo "📦 检查 Docker 服务..."
docker-compose -f docker/docker-compose.prod.yml ps

echo ""
echo "✅ 服务状态："
echo "  - PostgreSQL: localhost:5432"
echo "  - Elasticsearch: http://localhost:9200"
echo "  - API: http://localhost:8001"
echo "  - API 文档: http://localhost:8001/docs"
echo ""

# 检查 API 健康状态
echo "🏥 API 健康检查..."
curl -s http://localhost:8001/health | python -m json.tool || echo "API未启动"

echo ""
echo "🔍 Elasticsearch 集群状态..."
curl -s http://localhost:9200/_cluster/health 2>/dev/null | python -m json.tool || echo "Elasticsearch 未启动或端口未映射（检查 docker compose 是否包含 elasticsearch）"

echo ""
echo "📝 注意事项："
echo "  1. 数据库连接问题：需要手动初始化数据库表"
echo "  2. 前端已构建：web/dist/"
echo "  3. 使用 Nginx 或其他 Web 服务器提供前端服务"
echo ""
echo "🔧 手动初始化数据库："
echo "  空库建表：在 API 容器或本地 venv 执行 Python: from openrag.database import init_db; init_db()"
echo ""
