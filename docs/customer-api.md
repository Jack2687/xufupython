# 客户 API 文档维护记录

2026-11-05 API 1.4.0：新增 Plus（`plus`）和 Pro 50x／Pro Max（`promax`）套餐标识、目录示例、订阅及仅提链调用示例。可用套餐由 `GET /api/v1/catalog` 返回，报价、创建与查询订单使用一致的 `product_id`。实现范围与兼容说明见 `plus-promax-products.md`。

2026-11-04 后续本地版本为 1.3.0：补齐属性说明、套餐价格字典和 price_source；Pro 20x 使用 pro20x；统一删除记录可见性。用户随后授权提交推送和正式容器更新，发布结果以正式运维记录为准，完整变更和验证见 `admin-orders-pricing.md`。下文 1.2.0 为此前已发布基线。

2026-11-04：按用户授权补全面向客户的 API 文档及对应接口；用户随后授权提交推送 GitHub，并由正式原目录拉取、构建和更新应用。发布结果以项目正式运维记录为准。

## 客户入口

- `/docs`：客户接入文档，无需登录即可阅读；`/#/docs` 入口继续可用。
- `/api/openapi.json`：OpenAPI 3.1，文档版本 1.2.0，共 18 个操作。
- 文档按账户与套餐、订阅与结账链接、关闭自动续费、资金与充值、账号检查分类。提供订阅、仅提链、独立关闭续费三套步骤，以及 cURL、Node.js、Python 示例、嵌套参数表、响应字段、错误与状态说明。

## 接口范围

| 功能 | 接口 |
| --- | --- |
| 套餐、报价与会员 | GET `/api/v1/catalog`、`/quote`、`/membership` |
| 当前账户与概览 | GET `/api/v1/me`、`/overview` |
| 创建与查询订单 | POST/GET `/api/v1/orders`、GET `/api/v1/orders/{id}` |
| 关闭自动续费 | POST/GET `/api/v1/renewals`、GET `/api/v1/renewals/{id}` |
| 余额、兑换与流水 | GET `/api/v1/wallet`、POST `/api/v1/wallet/redeem`、GET `/api/v1/wallet/ledger` |
| 充值申请 | POST/GET `/api/v1/topups`、POST `/api/v1/topups/{id}/cancel` |
| Session 检查 | POST `/api/v1/session/inspect` |

以上均使用 `Authorization: Bearer YOUR_API_KEY`，只能访问所属序付账户的数据。管理员接口、业务配置和系统运维信息不进入客户文档。

创建订单使用 `workflow=subscription` 或 `link`，按后台当前配置的已有办理流程受理；是否开放统一遵守后台付款开关。付款执行代码和数据库结构未修改。创建订单、续费任务和充值申请要求固定的 `Idempotency-Key`，相同内容重试返回原记录，不同内容返回 409。账户余额与银行卡实际订阅费用分别说明，受理成功、付款成功、订阅生效和关闭续费分别判断。

## 维护与验证

- `backend/openapi.json` 是客户文档数据源，`frontend/src/components/ApiDocs.vue` 与 `frontend/src/apiDocs.ts` 负责展示和示例生成。接口变动时同步检查方法、路径、参数、响应、鉴权、错误及示例，避免出现仅写入文档的空接口。
- `tests/test_customer_api.py` 由 `tests/test_backend.py` 加载，使用一次性数据库及模拟调度。覆盖三个已配置流程的受理、并发幂等、余额冻结次数、账户隔离、后台开关、分页和鉴权；49 项后端测试通过。
- `frontend/tests/apiDocs.test.mjs` 的 3 项测试通过，检查嵌套字段、示例请求头/超时及完整指引；Vue 类型检查和生产构建通过。
- 浏览器检查桌面与手机布局、语言与请求场景切换、响应展开、菜单和两种文档入口。
- 本地 18970 健康检查通过；启动前无待领取或执行中的订单/续费，订单、流程、账本、用户、设置及续费六张表全部字段指纹前后一致。未执行真实付款或关闭续费验收。

正式发布按项目运维流程执行，保留正式数据库、配置及密钥；上述本地验证不代表真实扣款或关闭续费已验收。

2026-11-04 补充：文档侧栏新增 `product_id 套餐标识`，地址 `/docs#product-ids`，直接列出四种常见套餐映射；报价、下单和查询套餐章节提供跳转。实际开放与新增套餐仍以鉴权目录接口返回的 `items[].id` 为准，不限制为固定枚举。

2026-11-04 按用户补充要求：所有请求参数和 JSON 输入字段明确标注来源。依赖其他接口的字段写出 HTTP 方法、路径和响应字段路径，并链接到来源章节；区分原始 Session、序付账户编号、订单/续费/充值编号，注明密钥的网页入口及调用方生成的幂等标识。新增 `/docs#input-sources` 对照与完整取值顺序。


Team团队订阅：当前 Go 提链 + HTTP 付款流程统一使用 `product_id=team`；`seats` 是普通＋高级总席位（2～100），`premium_seats` 是其中高级数量（0～seats）。例如普通1＋高级1传2、1，全高级2传2、2。无需传团队空间名称；可选 `promo_code` 为用户自带优惠码，留空不传上游，平台服务费不变。`team_premium` 历史记录保留，当前流程不再作为新订单独立套餐。
