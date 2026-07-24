# Milvus RBAC 切换门禁

当前仓库已完成 runtime、index-admin、cleanup 三套凭据的应用隔离，但**不会在现有生产编排中直接打开 Milvus authorization**。原因是当前环境尚未提供已验证的用户/角色初始化 Job；先开认证再创建角色会同时中断 API、Worker 和管理面。

生产切换必须按顺序执行：

1. 在隔离环境启用 `common.security.authorizationEnabled=true`，使用一次性 root 凭据创建三类用户和角色。
2. runtime 仅授予现有 versioned collections 的 Search、Query、Insert、Delete、Load；验证 CreateCollection、AlterAlias、DropCollection 均被拒绝。
3. index-admin 授予 CreateCollection、CreateIndex、Load、CreateAlias、AlterAlias，但拒绝 DropCollection。
4. cleanup 仅由受控 Job 注入，授予 ReleaseCollection、DropCollection；API/Worker Pod 和 Compose service 不得出现 cleanup 环境变量。
5. 将三类非 root 凭据写入 Secret，滚动 API/Worker，完成读写、provision、alias 与拒绝 drop 测试。
6. 回收 root 凭据并启用审计。任一拒绝测试不符合预期，禁止进入首个生产 generation 迁移。

本步骤必须使用目标生产同版本 Milvus 复验；本地当前未配置任何三类账号，不能把无认证 Milvus 的成功测试当作 RBAC 通过。
