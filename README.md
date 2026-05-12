# OpenRag

企业向 RAG 平台：多工作区文档、向量 + 可选全文检索、JWT 与角色权限、以及服务令牌机读 API（`/service/v1`）。

## 说明文档

| 文档 | 内容 |
|------|------|
| [docs/项目方案与技术概述.md](docs/项目方案与技术概述.md) | **方案书**：动机、总体架构、关键技术、技术栈与亮点 |
| [docs/01-项目说明.md](docs/01-项目说明.md) | 功能、亮点、主流程、技术架构（与上篇互补，偏全览） |
| [docs/02-Kubernetes部署.md](docs/02-Kubernetes部署.md) | K8s 部署要点与清单 |
| [docs/03-使用说明.md](docs/03-使用说明.md) | Web 操作、权限、前后端配置 |
| [docs/04-外部系统接入与API.md](docs/04-外部系统接入与API.md) | 接入流程、用户 JWT API 摘要、**`/service/v1` 完整 API** |

开发与 Docker Compose：`LOCAL_DEV_GUIDE.md`、`docker/README.md`、`docker/QUICKSTART.md`。

Kubernetes 基线清单：`k8s/README.md` 与 `k8s/*.yaml`（部署前复制并编辑 `k8s/01-secret.example.yaml` → `k8s/01-secret.yaml`）。

仓库目录：`openrag/`（后端）、`web/`（前端）、`docker/`（镜像与编排）、`k8s/`（K8s 清单）。
