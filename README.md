# 序付 XUFU Python

FastAPI + PostgreSQL 的订阅服务管理系统，包含账户、会员、钱包、账单资料、代理管理、订阅办理、结账链接、续费处理和订单记录。Vue 前端由应用直接提供。

对外 API 文档位于 `/docs`，OpenAPI 文件位于 `/api/openapi.json`。文档版本 1.2.0，API 密钥开放 18 个接口，覆盖账户与套餐、订阅办理与结账链接、关闭自动续费、订单、余额流水、充值申请、兑换及 Session 检查。提供 cURL、Node.js、Python 示例与完整调用步骤；所有接口仅访问密钥所属账户的数据。创建订单、续费任务及充值申请必须携带 `Idempotency-Key`。维护与验证说明见 [客户 API 文档](docs/customer-api.md)。

## Docker 部署

需要 Docker Engine 与 Docker Compose。应用对外端口固定为 **5496**；PostgreSQL 18 使用独立容器和持久卷，不映射主机数据库端口。

```sh
git clone https://github.com/yigerende/xufupython.git
cd xufupython
cp .env.example .env
# 填写数据库密码、32 字节 Base64 主密钥和初始化管理员密码。
docker compose build
docker compose up -d
```

访问 `http://服务器地址:5496`。健康检查为 `/health/live` 和 `/health/ready`。初始管理员只在空库创建；已有用户和配置不会被重置。

生产环境不要配置本机前置代理地址。应用会直接使用系统中保存的远端代理。办理功能统一由后台“开放站内付款”开关控制，保存后生效，无需重启。关闭时停止受理和领取新办理任务，已执行中的任务正常收尾；代理检查与系统维护继续运行。

## 数据迁移与备份

迁移时先停止来源应用并确认没有在途任务，再用 `pg_dump -Fc --no-owner --no-privileges` 导出数据库。将来源环境的 `MASTER_KEY` 保留到新环境，否则已加密的数据无法解密。先启动目标 PostgreSQL，将备份恢复至空库并核对数据，再启动应用；同一份数据不要同时运行两个后台队列。

```sh
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U xufupython -d xufupython --no-owner --no-privileges --exit-on-error < backup.dump
docker compose up -d app
```

数据库备份、`.env`、主密钥、日志和本地数据不得提交 Git。后续升级仅更新代码与应用镜像，保留 `.env` 和数据库卷；不要使用 `docker compose down -v`。

## 本地开发

本地脚本适配 Windows、Python 3.11+、PostgreSQL 18 和 Node.js 22+。本地应用使用 18970，专用数据库使用 18972。

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
./start-local.ps1
```

`restart-local.ps1` 重启本项目应用；`stop-local.ps1` 停止应用。数据库位于 `.local/postgres`，运行配置位于 `.env`。

## 验证

```powershell
./.venv/Scripts/python.exe tests/test_backend.py
./.venv/Scripts/python.exe tests/test_post_payment.py
node --experimental-strip-types --test frontend/tests/paymentJourney.test.mjs
node --experimental-strip-types --test frontend/tests/apiDocs.test.mjs
npm --prefix frontend run build
```

后端集成测试只使用本机 18972 上新建的临时数据库；网络请求使用模拟响应，不执行真实付款或取消续费。付款后异常与付款成功分别记录，恢复入口 `scripts/recover_post_payment.py ORDER_ID` 只继续已有付款后检查点，并核对原订单代理会话与出口。
