# gener 流程的 Go 付款后续

2026-11-03 用户授权：仅将 `gener_direct_pay` 的付款成功后续拆成独立 Go 程序，显示名改为 `gener提链+http直连付款流程 +Go付款成功后续`。不处理旧订单迁移，不修改 `cslive_direct_pay`，本次不发布 GitHub 或正式环境。

## 范围和步骤

gener 原有提链、HTTP 付款、套餐长短链、后台账单随机抽取、服务费结算均保留。只有确认付款成功、写入 `paid_at` 后，`backend/subscription.py` 调用 `backend/go_post_payment.py`。

Go 从本地参考 `xupay/internal/cslive` 中提取付款后函数和必要依赖，来源、选择范围及哈希见 `go-post-payment/SOURCE.json`。`finishPayment`、`confirmActivation`、`disableRenewal` 保留参考实现：

1. 访问经过本单校验的商户回跳地址。
2. 查询订阅是否生效，明确返回 401 时刷新 Session 后重查。生效轮询最多 5 轮，轮间 2 秒。
3. 用户保留自动续费则完成；否则主动刷新 Session，再查询续费状态。
4. 已关闭则直接完成；明确开启才提交取消，随后复查是否关闭。状态未知不提交。
5. 取消响应丢失时先查状态，仅在仍开启且属于可重试故障时再提交；累计最多 2 次取消写请求，计数随本单保存，恢复也不清零。

GET 网络故障及 408/429/500/502/503/504 最多 3 次，等待 0.5 秒、1 秒。403 不重试，POST 不在传输层重放。回跳页 HTTP 状态不代替订阅查询结论。参考实现的非 JSON 403 会返回 `invalid_json_response`，原 HTTP 403 和脱敏响应证据仍记入技术日志。

## 数据和流程隔离

- 子进程通过 stdin/stdout 的 NDJSON 与 Python 交互。Session、AT、Cookie、代理凭据不进入命令行或日志，只进入内存管道及原有加密 checkpoint。
- 每个阶段先保存加密进度，Python 数据库提交成功后 ACK，Go 才继续；提交失败时不越过取消边界、不报告已完成。
- Go 检查账号、结账标识、套餐、取消意愿及状态。Python 入口要求本单属于 `gener_direct_pay`，且已有成功付款记录。
- 付款成功后的错误只标记收尾待核实，不回滚付款、不释放已结算服务费、不再次发起付款，也不静默回退 Python 执行。
- `cslive_direct_pay` 及独立“取消自动续费”菜单保留原 Python 实现。恢复入口按订单保存的流程标识选择执行器，不按当前后台默认配置改派。
- 不迁移、不补跑历史订单，没有新增数据库字段或修改配置值。

## 网络和监控

沿用订单已保存的代理 endpoint、供应商 sticky session 和有效期。Go 新建自己的连接客户端，但不会重新分配订单代理；不查询出口 IP，不比较出口一致性。保留实际 ChatGPT Cookie，参考 Go 实现使用自己的默认 HTTP/TLS 请求配置，不宣称复用原 Python 的 TCP/TLS 连接。

本地设置 `LOCAL_EGRESS_PROXY` 时，通过回环转发连接同一供应商代理。HTTP/SOCKS 保留原协议；HTTPS 供应商代理在转发器中以原主机名进行证书验证和 TLS 握手，Go 至回环这一段使用 HTTP CONNECT。正式环境没有本机转发配置时直接使用订单代理。禁止关闭 TLS 证书校验或失败后直连。

Go 单请求最长 45 秒，单次后续执行最长 240 秒，并受订单代理剩余有效期约束。程序只允许 Session 刷新、订阅查询、验证后的商户回跳及符合状态条件的取消续费接口；没有付款入口。挑战求解、提链和 Stripe 付款执行未链接进此程序。

技术日志接入现有支付全监控，包含步骤、HTTP 状态、请求尝试次数、重试等待、连接/发送/响应耗时、脱敏错误证据。缺失的底层耗时不补造。

## 构建与验证

`scripts/build_post_payment.py` 使用 Go 1.25+ 编译本机 `bin/post-payment.exe`（Linux 为 `bin/post-payment`），按源码和二进制哈希复用缓存；`scripts/local.py start` 在服务未运行时自动构建。Dockerfile 增加 Go 构建阶段，只复制独立二进制到应用镜像。

- Go 离线流程测试 12 项通过。
- Python 子进程、进度 ACK、代理转发和日志测试 8 项通过。
- 原 Python 付款后测试 23 项通过，临时数据库后端集成测试 45 项通过。
- 前端类型检查及构建通过；Windows 和 Linux amd64 二进制编译通过。
- 2026-11-03 18:52（北京时间）通过新 Python → Go 入口，仅刷新用户提供的 Session：HTTP 200、约 6.7 秒、新 AT、账号标识一致。使用后台配置的新诊断代理会话；没有查询出口、修改订单、付款或取消续费。结果 `.local/diagnostics/packaged-go-refresh-20261103-185157.json`，凭据结果仅加密保存。
- 真实付款后整单与真实取消没有作为测试执行。当前机器未提供 Docker CLI，本次没有实际构建/启动 Docker 镜像；Linux 交叉编译不等同镜像运行验证。

修改前备份：`D:/aixiangmu/反代/_codex-backups/xufupython/before-go-post-payment-20261103-183236`，含源码配置 ZIP、文件哈希、已校验 PostgreSQL dump。

本地完成空闲检查、再次备份数据库及应用重启，当前入口 `http://127.0.0.1:18970`，首页/live/ready 均 200。10 张业务/配置表全部字段指纹与重启前一致，静态资源匹配，启动无错误；付款开关保持原值，没有旧单重跑。验收记录 `.local/go-post-payment-restart.json`，受保护文件对比 `.local/go-post-payment-scope.json`。未推送 GitHub、未连接或更新正式环境。
