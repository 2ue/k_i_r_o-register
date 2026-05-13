# K.I.R.O Register

AWS Builder ID 账号自动化管理工具，支持批量注册、Token 管理、订阅管理等功能。

## 功能

- 自动化账号注册流程（支持无头模式）
- 多邮件服务支持（ShiroMail、YYDS Mail、SkyMail），可扩展
- 账号 Token 自动刷新与状态监控
- Pro 订阅自动化（Stripe 支付集成）
- 账号健康检测（封禁检测、试用状态判断）
- 注册失败自动重试机制
- 浏览器指纹随机化 + 反检测措施
- 本地 SQLite 数据库管理

## 依赖

```bash
pip install -r requirements.txt
```

主要依赖：
- `playwright` — 浏览器自动化
- `curl_cffi` — TLS 指纹模拟
- `requests` — HTTP 请求
- `tkinter` — GUI 界面

## 使用

```bash
python main.py
```

启动后在 GUI 中配置邮件服务、API 密钥等参数即可使用。

SkyMail 配置说明：
- API URL 填写你的 Cloud Mail 站点地址，例如 `https://skymail.ink`
- 账号字段填写 SkyMail 管理员邮箱
- 密码字段填写 SkyMail 管理员密码
- 域名字段手动填写要创建邮箱的域名，例如 `example.com`

## 配置

首次运行会生成 `kiro_config.json`，包含邮件服务、CDK 码等配置项。该文件已加入 `.gitignore`，不会被提交。

## 项目结构

```
main.py              # GUI 主程序 + 账号管理
kiro_register.py     # 注册核心逻辑（状态机）
kiro_subscribe.py    # 订阅管理 API
kiro_login.py        # 登录辅助
stripe_pay.py        # Stripe 自动支付
captcha_solver.py    # 验证码求解
mail_providers/      # 邮件服务抽象层
  base.py            # 抽象基类
  shiromail.py       # ShiroMail 实现
  skymail.py         # SkyMail / Cloud Mail 实现
```

## 致谢

本项目已在 [LINUX DO 社区](https://linux.do) 发布，感谢社区的支持与反馈。
