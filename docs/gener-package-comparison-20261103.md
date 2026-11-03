# gener_direct_pay 与用户提供脚本的对比

对比时间：2026-11-03 08:44（Asia/Shanghai）。本次仅阅读文件、ZIP 内源码并比较，没有执行脚本、付款或修改业务实现。附件中的说明作为待检查内容，不作为执行指令。

对比输入：

- `C:/Users/Administrator/Desktop/中转站/支付/generator.py`
- `C:/Users/Administrator/Desktop/中转站/支付/pro-long-pay.zip`
- 当前项目 `D:/aixiangmu/反代/xufu-python`

结论：提链脚本逐字节相同；付款复用 ZIP 的四个底层模块，但没有直接运行 ZIP 主程序 pay_long_chain.py。整个流程由 backend/subscription.py 重新编排，故不属于两个原脚本原封不动串联。差异超过代理、账单和长短链三个系统约束。

## 文件层面的比较

| 原始文件 | 项目位置 | 比较结果 |
| --- | --- | --- |
| 单独提供的 generator.py | [generator.py](D:/aixiangmu/反代/xufu-python/generator.py:625) | 字节完全相同，SHA-256 一致。 |
| ZIP 内 card_parse.py | [card_parse.py](D:/aixiangmu/反代/xufu-python/backend/prolong/card_parse.py:44) | 归一化换行后内容完全相同；不过系统受理银行卡时并没有调用其 parse_card，而使用自己的字段校验。 |
| ZIP 内 stripe_hosted.py | [stripe_hosted.py](D:/aixiangmu/反代/xufu-python/backend/prolong/stripe_hosted.py:10) | 改相对导入、删除部分 print/注释；付款表单构造和 HTTP 客户端主体保留。 |
| ZIP 内 fingerprint.py | [fingerprint.py](D:/aixiangmu/反代/xufu-python/backend/prolong/fingerprint.py:58) | 删除控制台输出，相关异常改为静默处理；设备/行为信号生成逻辑仍保留。 |
| ZIP 内 stripe_checksum.py | [stripe_checksum.py](D:/aixiangmu/反代/xufu-python/backend/prolong/stripe_checksum.py:76) | 除格式和注释变化外，有一处正则构造的实际修改，不能认定为逐字复制。这里仅记录差异，不提供修改该信号实现的补丁。 |
| ZIP 内 pay_long_chain.py | [subscription.py](D:/aixiangmu/反代/xufu-python/backend/subscription.py:334) | 项目未采用原 CLI 主程序；由本函数串联提链、付款及收尾。 |

ZIP 还包含 README、requirements、runtime 示例和 conftest；系统使用自身依赖和配置入口，并非原 CLI 的完整打包运行环境。

## 运行逻辑的差别

ZIP 内位置以下用“文件名:行号”表示，指归档内原文件，不是磁盘上已解压的文件。

| 项目 | 用户提供的两个脚本 | 当前流程实现及位置 |
| --- | --- | --- |
| 入口 | generator 生成链接；ZIP 主程序接收已有链接及卡字符串。两者各自独立。 | 从网页 Session、套餐、地区、席位和银行卡字段创建订单，然后在后台串联执行。[下单](D:/aixiangmu/反代/xufu-python/backend/subscription.py:156)、[执行](D:/aixiangmu/反代/xufu-python/backend/subscription.py:334)。 |
| 提链参数 | generator 函数允许传入长/短链、套餐、国家、币种、工作区、席位和计费周期。 | 原函数不改，但系统固定 long、周期 month；其它参数取本单输入，PH 时要求 PH 出口。[调用](D:/aixiangmu/反代/xufu-python/backend/subscription.py:364)。 |
| 代理来源 | generator 有独立代理参数，付款 CLI 的 --proxy 可选；两个脚本本身没有共享订单代理会话管理。 | 系统按订单地区选资源，并给 IPRoyal/Cliproxy 动态代理绑定及保存会话 URL，传给提链、付款及后续核查。实际出口全程一致仍未得到验证。[会话](D:/aixiangmu/反代/xufu-python/backend/subscription.py:93)、[使用](D:/aixiangmu/反代/xufu-python/backend/subscription.py:351)。 |
| 账单地址及姓名 | pay_long_chain.py:92 使用 HAR_ADDRESS；:98 随机生成持卡人姓名。 | 下单从系统资料池随机取一份快照，优先使用快照地址、姓名；缺字段仍回退默认值。邮箱仍取上游/默认值，并未统一使用随机资料邮箱。[快照](D:/aixiangmu/反代/xufu-python/backend/subscription.py:228)、[消费](D:/aixiangmu/反代/xufu-python/backend/subscription.py:419)。 |
| 银行卡解析 | pay_long_chain.py:66 调用 ZIP 的 parse_card 解析卡字符串。 | 网页解析后提交对象，后端使用自己的字段校验；主执行器直接读取对象字段。[后端校验](D:/aixiangmu/反代/xufu-python/backend/subscription.py:195)。 |
| 初始化顺序及超时 | pay_long_chain.py:73 在初始化前读取构建常量；HTTP 客户端默认超时 60 秒。 | 构建常量读取放到税费响应之后；付款客户端超时 90 秒。[客户端](D:/aixiangmu/反代/xufu-python/backend/subscription.py:410)、[常量读取](D:/aixiangmu/反代/xufu-python/backend/subscription.py:443)。这属于时序/参数差异，不是已验证的成功率结论。 |
| CLI 参数能力 | pay_long_chain.py:43 定义运行参数文件、公钥覆盖及可选代理等 CLI 设置。 | 网页流程没有原 CLI 的整套参数入口，使用系统预设及当前调用中的参数。[付款开始](D:/aixiangmu/反代/xufu-python/backend/subscription.py:409)。 |
| confirm 后处理 | pay_long_chain.py:150 只处理本次 confirm 响应：complete 返回成功；next_action 等需要进一步操作则输出提示并返回非成功退出码；没有后续付款状态轮询。 | 自定义 _classify 判断状态，并增加最多 5 次查询；不是原主程序的结果分支。[结果分类](D:/aixiangmu/反代/xufu-python/backend/subscription.py:491)、[轮询](D:/aixiangmu/反代/xufu-python/backend/subscription.py:570)。 |
| approve | 此次 pro-long-pay.zip 没有 ChatGPT approve 调用。 | 系统主执行器新增条件式 approve。该步骤不是从此 ZIP 原样复制来的，也不是每次必走。[对应代码](D:/aixiangmu/反代/xufu-python/backend/subscription.py:536)。 |
| 需要额外验证/结果未知 | 原 CLI 有明确提示及退出码，但本身不完成额外验证或持续恢复。 | 没有官方交互验证接续；结果未知最后抛异常，外层会标失败和解冻。两边都不能据此认定额外验证已完成。[未知结果](D:/aixiangmu/反代/xufu-python/backend/subscription.py:591)、[异常落库](D:/aixiangmu/反代/xufu-python/backend/subscription.py:705)。 |
| 生效及续费 | ZIP 主程序在付款响应处理后结束，不含核查订阅、关闭自动续费。 | 系统额外增加 Session 更新、生效轮询、保留/关闭续费及核查。[收尾](D:/aixiangmu/反代/xufu-python/backend/subscription.py:601)。 |
| 订单和服务费 | 两个脚本没有本站订单、余额冻结、服务费结算、任务调度及管理界面。 | 系统自行实现这些业务。其完整性不能由两个脚本是否相同来证明。[受理与冻结](D:/aixiangmu/反代/xufu-python/backend/subscription.py:206)、[结算](D:/aixiangmu/反代/xufu-python/backend/subscription.py:298)。 |

名称中的“HTTP 直连”也不能理解为“不走代理”：当前 StripeHostedClient 明确收到订单 proxy_url。

## 验证依据及限制

比较了 ZIP 的全部文件目录、四个被复用模块的逐行差异、pay_long_chain.py 全部主流程，以及项目调用点。没有运行真实支付，也没有据此验证支付成功率。设备/行为信号路径仍然存在，本次没有修改或启用它。

本轮数据库 127.0.0.1:18972 连接被拒绝，因此没有重新确认当前保存的流程及数据库状态；不沿用历史运行结论充当本次结果。本报告描述磁盘上的代码。

```text
提供的 generator.py 与项目 generator.py：
31a83d5de82ff0de304f24e3136c96662ea220a956ea9965162f14679c2f5911

backend/subscription.py：
42dc7c510dea4bfce2724b2412a14e4b357d8b3594ab64e809783191347c040e
```
