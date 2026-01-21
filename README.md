# mail-forward

本项目用于**每隔 1 小时读取学校邮箱的新邮件，并自动转发到另一个邮箱**。

## 你需要准备什么

- **学校邮箱的 IMAP 登录信息**（一般就是邮箱账号 + 邮箱密码；若学校启用了“客户端授权码”，就用授权码）
- **用于发送转发邮件的 SMTP 账号**
  - 可以用学校邮箱本身（前提是学校邮箱提供 SMTP）
  - 也可以用 QQ 邮箱（推荐），但需要在 QQ 邮箱里开启 SMTP 并生成“授权码”

## 快速开始（Windows）

1) 安装 Python 3.10+（建议 3.11/3.12）

2) 安装依赖

```bash
python -m pip install -r requirements.txt
```

3) 创建配置文件

- 把 `config.example.env` 复制一份并重命名为 `.env`
- 按你的真实信息填写：
  - `SRC_EMAIL` / `SRC_PASSWORD`：学校邮箱账号与密码（或授权码）
  - `IMAP_HOST`：学校邮箱 IMAP 服务器地址（不确定就问学校/查学校邮箱帮助文档）
  - `SMTP_*`：用于“发出转发邮件”的 SMTP（如果用 QQ 发信，这里就填 QQ 的 SMTP）
  - `DEST_EMAIL=283406@qq.com`

4) 运行一次（测试）

```bash
python forwarder.py --once
```

5) 持续运行（每 1 小时自动轮询）

```bash
python forwarder.py
```

## 去重/避免重复转发的机制

- 脚本会在本地生成 `state.json`，记录某个邮箱/文件夹最后处理到的 UID
- 第一次运行：只转发 **UNSEEN（未读）的5封** 邮件，避免把历史全部转发
- 后续运行：转发 UID 大于上次记录的新邮件

## 常见问题

### 学校邮箱的 IMAP/SMTP 地址不知道怎么办？

- 先试试常见的：`imap.<域名>` / `smtp.<域名>` / `mail.<域名>`
- 更稳妥：问学校信息中心/查学校邮箱官方说明（通常会写 IMAP/SMTP 服务器和端口）

### QQ 邮箱怎么发 SMTP？

- 需要在 QQ 邮箱设置里开启 SMTP，并生成**授权码**
- `SMTP_USER` 填你的 QQ 邮箱
- `SMTP_PASSWORD` 填授权码（不是 QQ 登录密码）

