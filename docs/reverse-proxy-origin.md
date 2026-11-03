# HTTPS 反向代理登录

TLS 在 Nginx/1Panel 等反向代理终止时，应用接到 HTTP 请求，但浏览器 Origin 是 HTTPS。部署时在 `.env` 设置 `PUBLIC_ORIGIN=https://实际域名`，Compose 会传给应用；代理须保留原 Host。

只接受该配置对应的域名、协议和端口，不因任意 `X-Forwarded-Proto` 或 `X-Forwarded-Host` 放行。`Sec-Fetch-Site: cross-site` 仍拒绝，其他域名、错误端口、空或非法 Origin 仍拒绝。未配置时保留原同源规则，直接 IP/本地开发访问仍按当前地址同源校验。纯 HTTPS 部署可同时设置 `COOKIE_SECURE=true`。

修改只影响浏览器写请求的来源校验，不修改付款、订单、代理资源、账单资料或后台任务。保存配置并重建应用容器后，使用真实 HTTPS 域名测试登录、读取当前用户、访问管理接口和注销；仅测试 localhost 登录不能验证 HTTPS 反向代理链。
