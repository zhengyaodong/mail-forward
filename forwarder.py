import json
import logging
import os
import ssl
import time
import re
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage
from email.policy import default
from pathlib import Path
from typing import Iterable, Optional

import imaplib
import smtplib

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


STATE_PATH = Path("state.json")


@dataclass(frozen=True)
class Config:
    src_email: str
    src_password: str
    imap_host: str
    imap_port: int
    imap_ssl: bool
    imap_folder: str
    imap_timeout: int

    smtp_user: str
    smtp_password: str
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool

    dest_email: str
    poll_interval_seconds: int


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return int(v)


def decode_str(s):
    """解码邮件中的编码字符串"""
    if not s: return ""
    parts = decode_header(s)
    decoded = []
    for value, charset in parts:
        if isinstance(value, bytes):
            value = value.decode(charset if charset else 'utf-8', errors='ignore')
        decoded.append(str(value))
    return re.sub(r'\s+', ' ', "".join(decoded)).strip()


def load_config() -> Config:
    if load_dotenv:
        # Optional; user can create .env locally
        load_dotenv(override=False)

    cfg = Config(
        src_email=os.environ["SRC_EMAIL"],
        src_password=os.environ["SRC_PASSWORD"],
        imap_host=os.environ["IMAP_HOST"],
        imap_port=_env_int("IMAP_PORT", 993),
        imap_ssl=_env_bool("IMAP_SSL", True),
        imap_folder=os.getenv("IMAP_FOLDER", "INBOX"),
        imap_timeout=_env_int("IMAP_TIMEOUT", 120),
        smtp_user=os.environ["SMTP_USER"],
        smtp_password=os.environ["SMTP_PASSWORD"],
        smtp_host=os.environ["SMTP_HOST"],
        smtp_port=_env_int("SMTP_PORT", 465),
        smtp_ssl=_env_bool("SMTP_SSL", True),
        dest_email=os.environ["DEST_EMAIL"],
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 3600),
    )
    return cfg


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# def imap_connect(cfg: Config) -> imaplib.IMAP4:
#     if cfg.imap_ssl:
#         return imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=cfg.imap_timeout)
#     return imaplib.IMAP4(cfg.imap_host, cfg.imap_port, timeout=cfg.imap_timeout)
def imap_connect(cfg: Config):
    # 逻辑修正：如果配置开启了SSL，或者端口是默认的SSL端口(993)，都强制使用SSL连接
    use_ssl = cfg.imap_ssl or cfg.imap_port == 993
    
    if use_ssl:
        print(f"Using IMAP SSL connection (Host: {cfg.imap_host}:{cfg.imap_port})")
        return imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=cfg.imap_timeout)
    else:
        print(f"Using IMAP plain connection (Host: {cfg.imap_host}:{cfg.imap_port})")
        return imaplib.IMAP4(cfg.imap_host, cfg.imap_port, timeout=cfg.imap_timeout)


def smtp_connect(cfg: Config) -> smtplib.SMTP:
    if cfg.smtp_ssl:
        context = ssl.create_default_context()
        return smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=120)
    server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
    server.starttls(context=ssl.create_default_context())
    return server


def _imap_ok(resp) -> bool:
    if not resp or len(resp) < 1:
        return False
    return resp[0] == "OK"


def _ensure_selected(imap: imaplib.IMAP4, folder: str) -> None:
    sel = imap.select(folder, readonly=False)
    if not _imap_ok(sel):
        raise RuntimeError(f"IMAP select failed: {sel}")


def _parse_uid_fetch(data) -> bytes:
    # data is like [(b'123 (RFC822 {..}', b'raw bytes'), b')']
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    raise RuntimeError("IMAP FETCH returned no message bytes")


# def _build_forward_message(
#     cfg: Config,
#     raw_header: bytes,
#     raw_body: bytes,
#     original_uid: str,
# ) -> EmailMessage:
#     """构造转发邮件，只包含头部和正文文本，不包含附件"""
#     # 解析头部
#     header_msg = message_from_bytes(raw_header, policy=default)
#     subject = decode_str(header_msg.get('Subject'))
#     from_info = decode_str(header_msg.get('From'))

#     # 解析正文
#     temp_msg = message_from_bytes(raw_body, policy=default)
#     body_part = temp_msg.get_body(preferencelist=('html', 'plain'))
    
#     body_content = ""
#     is_html = False
#     if body_part:
#         body_content = body_part.get_content()
#         is_html = (body_part.get_content_type() == 'text/html')
#     else:
#         body_content = "（无法解析正文内容，请登录网页版查看）"

#     # 构造新邮件
#     forward_msg = EmailMessage()
#     forward_msg['Subject'] = f"[通知正文] {subject}"
#     forward_msg['From'] = cfg.smtp_user
#     forward_msg['To'] = cfg.dest_email
    
#     notice = f"--- 学校邮箱转发 (已自动忽略附件以防断连) ---\n发件人: {from_info}\n主题: {subject}\n\n"
    
#     if is_html:
#         header_html = f"<div style='background:#f9f9f9;padding:10px;border:1px solid #eee'><b>发件人:</b> {from_info}<br><b>主题:</b> {subject}<br><i>[提示] 附件已忽略，请至学校邮箱下载</i></div><br>"
#         forward_msg.add_alternative(header_html + body_content, subtype='html')
#     else:
#         forward_msg.set_content(notice + body_content)
    
#     return forward_msg
# 需要引入 mimetypes 库来判断附件类型（在文件开头添加 import mimetypes）
import mimetypes 

def _build_forward_message(
    cfg: Config,
    raw_bytes: bytes,  # 这里接收完整的原始字节
    original_uid: str,
) -> EmailMessage:
    """构造转发邮件，包含附件"""
    # 1. 解析原始邮件
    original_msg = message_from_bytes(raw_bytes, policy=default)
    
    subject = decode_str(original_msg.get('Subject'))
    from_info = decode_str(original_msg.get('From'))
    
    # 2. 创建新邮件对象
    forward_msg = EmailMessage()
    forward_msg['Subject'] = f"[转发] {subject}"
    forward_msg['From'] = cfg.smtp_user
    forward_msg['To'] = cfg.dest_email
    
    # 3. 提取正文并添加到新邮件
    # 尝试优先获取 HTML，其次是纯文本
    body_part = original_msg.get_body(preferencelist=('html', 'plain'))
    
    notice_text = f"<p style='color:gray;font-size:12px;'>--- 原始发件人: {from_info} ---</p><hr>"
    
    if body_part:
        content = body_part.get_content()
        ctype = body_part.get_content_type()
        if ctype == 'text/html':
            forward_msg.add_alternative(notice_text + content, subtype='html')
        else:
            # 纯文本处理
            forward_msg.set_content(f"--- 原始发件人: {from_info} ---\n\n" + content)
    else:
        forward_msg.set_content(f"--- 原始发件人: {from_info} ---\n(无正文内容)")

    # 4. 遍历并处理附件
    for part in original_msg.walk():
        # 跳过 multipart 容器本身
        if part.get_content_maintype() == 'multipart':
            continue
        # 跳过正文部分（因为上面已经处理过了）
        if part == body_part:
            continue
            
        filename = part.get_filename()
        if filename:
            # 解码文件名
            filename = decode_str(filename)
            
            # 获取附件内容
            payload = part.get_payload(decode=True)
            if payload:
                # 猜测 MIME 类型
                ctype = part.get_content_type()
                maintype, subtype = ctype.split('/', 1)
                
                # 如果猜测失败，给默认值
                if not maintype: maintype = 'application'
                if not subtype: subtype = 'octet-stream'
                
                # 添加附件到新邮件
                forward_msg.add_attachment(
                    payload,
                    maintype=maintype,
                    subtype=subtype,
                    filename=filename
                )
                logging.info(f"Attached file: {filename}")

    return forward_msg


def _uids_to_process(imap: imaplib.IMAP4, folder: str, last_uid: Optional[int]) -> list[int]:
    """搜索未读邮件，不管 last_uid 是否存在"""
    _ensure_selected(imap, folder)

    # 始终搜索未读邮件，与测试文件逻辑保持一致
    typ, data = imap.uid("search", None, "UNSEEN")

    if typ != "OK":
        raise RuntimeError(f"IMAP search failed: {(typ, data)}")

    if not data or not data[0]:
        return []
    raw = data[0].decode("utf-8", errors="ignore").strip()
    if not raw:
        return []
    return [int(x) for x in raw.split() if x.isdigit()]


# def _imap_fetch_header_and_text(imap: imaplib.IMAP4, uid: int) -> tuple[bytes, bytes]:
#     """只获取邮件头部和正文文本，不获取附件"""
#     typ, data = imap.uid("fetch", str(uid), "(BODY.PEEK[HEADER] BODY.PEEK[TEXT])")
#     if typ != "OK":
#         raise RuntimeError(f"IMAP fetch failed for uid={uid}: {(typ, data)}")
    
#     raw_header = b""
#     raw_body = b""
#     for part in data:
#         if isinstance(part, tuple):
#             if b'HEADER' in part[0]:
#                 raw_header = part[1]
#             elif b'TEXT' in part[0]:
#                 raw_body = part[1]
    
#     return raw_header, raw_body
def _build_forward_message_no_attachment(
    cfg: Config,
    raw_header: bytes,
    raw_body: bytes,
    original_uid: str,
) -> EmailMessage:
    """构造转发邮件，只包含头部和正文文本，不包含附件"""
    # 解析头部
    header_msg = message_from_bytes(raw_header, policy=default)
    subject = decode_str(header_msg.get('Subject'))
    from_info = decode_str(header_msg.get('From'))

    # 解析正文
    temp_msg = message_from_bytes(raw_body, policy=default)
    body_part = temp_msg.get_body(preferencelist=('html', 'plain'))
    
    body_content = ""
    is_html = False
    if body_part:
        body_content = body_part.get_content()
        is_html = (body_part.get_content_type() == 'text/html')
    else:
        body_content = "（无法解析正文内容，请登录网页版查看）"

    # 构造新邮件
    forward_msg = EmailMessage()
    forward_msg['Subject'] = f"[通知正文] {subject}"
    forward_msg['From'] = cfg.smtp_user
    forward_msg['To'] = cfg.dest_email
    
    notice = "--- 学校邮箱转发 (已自动忽略附件以防断连) ---\n" + "发件人: " + from_info + "\n" + "主题: " + subject + "\n\n"
    
    if is_html:
        header_html = "<div style='background:#f9f9f9;padding:10px;border:1px solid #eee'><b>发件人:</b> " + from_info + "<br><b>主题:</b> " + subject + "<br><i>[提示] 附件已忽略，请至学校邮箱下载</i></div><br>"
        forward_msg.add_alternative(header_html + body_content, subtype='html')
    else:
        forward_msg.set_content(notice + body_content)
    
    return forward_msg


def _imap_fetch_header_and_text(imap: imaplib.IMAP4, uid: int) -> tuple[bytes, bytes]:
    """只获取邮件头部和正文文本，不获取附件"""
    typ, data = imap.uid("fetch", str(uid), "(BODY.PEEK[HEADER] BODY.PEEK[TEXT])")
    if typ != "OK":
        raise RuntimeError(f"IMAP fetch failed for uid={uid}: {(typ, data)}")
    
    raw_header = b""
    raw_body = b""
    for part in data:
        if isinstance(part, tuple):
            if b'HEADER' in part[0]:
                raw_header = part[1]
            elif b'TEXT' in part[0]:
                raw_body = part[1]
    
    return raw_header, raw_body


def _imap_fetch_full_message(imap: imaplib.IMAP4, uid: int) -> bytes:
    """获取完整邮件内容（包含附件）"""
    # RFC822 代表获取邮件的原始完整数据
    typ, data = imap.uid("fetch", str(uid), "(RFC822)")
    if typ != "OK":
        raise RuntimeError(f"IMAP fetch failed for uid={uid}: {(typ, data)}")
    
    # 解析返回的数据结构，通常 data[0] 是 (b'uid (RFC822 {size}', b'raw content')
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[1] # 返回原始字节流
    raise RuntimeError("IMAP FETCH returned no message bytes")


def _imap_mark_forwarded(imap: imaplib.IMAP4, uid: int) -> None:
    """标记邮件为已读，不使用自定义标记以避免服务器错误"""
    imap.uid("store", str(uid), "+FLAGS", r"(\Seen)")


def process_once(cfg: Config) -> int:
    """处理一次邮件转发任务"""
    state = load_state()
    state_key = f"{cfg.src_email}:{cfg.imap_host}:{cfg.imap_folder}"
    last_uid = state.get(state_key)
    last_uid_int = int(last_uid) if isinstance(last_uid, int) or (isinstance(last_uid, str) and last_uid.isdigit()) else None

    forwarded = 0

    imap = None
    smtp = None
    try:
        imap = imap_connect(cfg)
        imap.login(cfg.src_email, cfg.src_password)

        uids = _uids_to_process(imap, cfg.imap_folder, last_uid_int)
        if not uids:
            logging.info("No new messages to forward.")
            return 0

        smtp = smtp_connect(cfg)
        smtp.login(cfg.smtp_user, cfg.smtp_password)

        # 处理最新的邮件
        for uid in uids:
            attempts = 0
            success = False
            while attempts < 3 and not success:
                attempts += 1
                try:
                    # # 使用新的获取方式，只获取头部和正文文本
                    # # raw_header, raw_body = _imap_fetch_header_and_text(imap, uid)
                    # [修改] 获取完整邮件数据
                    raw_bytes = _imap_fetch_full_message(imap, uid)
                    
                    # 构造转发邮件
                    # fwd = _build_forward_message(cfg, raw_header, raw_body, original_uid=str(uid))
                    # [修改] 构造包含附件的转发邮件
                    fwd = _build_forward_message(cfg, raw_bytes, original_uid=str(uid))
                    
                    # 发送邮件
                    smtp.send_message(fwd)
                    
                    # 成功后标记为已读
                    _imap_mark_forwarded(imap, uid)
                    forwarded += 1

                    if last_uid_int is None or uid > last_uid_int:
                        last_uid_int = uid
                        state[state_key] = last_uid_int
                        save_state(state)

                    logging.info("Forwarded uid=%s (attempt %d)", uid, attempts)
                    success = True
                except imaplib.IMAP4.abort as e:
                    logging.warning("IMAP aborted on uid=%s with attachments attempt=%d: %s", uid, attempts, e)
                    
                    # 如果是第一次失败，尝试降级处理：只转发标题和正文
                    if attempts == 1:
                        logging.info("Attempting to forward without attachments for uid=%s", uid)
                        try:
                            # 重新连接 IMAP
                            try:
                                imap.logout()
                            except Exception:
                                pass
                            imap = imap_connect(cfg)
                            imap.login(cfg.src_email, cfg.src_password)
                            _ensure_selected(imap, cfg.imap_folder)
                            
                            # 获取邮件头部和正文文本
                            raw_header, raw_body = _imap_fetch_header_and_text(imap, uid)
                            
                            # 重新连接 SMTP
                            try:
                                smtp.quit()
                            except Exception:
                                pass
                            smtp = smtp_connect(cfg)
                            smtp.login(cfg.smtp_user, cfg.smtp_password)
                            
                            # 构造无附件的转发邮件
                            fwd = _build_forward_message_no_attachment(cfg, raw_header, raw_body, original_uid=str(uid))
                            
                            # 发送邮件
                            smtp.send_message(fwd)
                            
                            # 成功后标记为已读
                            _imap_mark_forwarded(imap, uid)
                            forwarded += 1

                            if last_uid_int is None or uid > last_uid_int:
                                last_uid_int = uid
                                state[state_key] = last_uid_int
                                save_state(state)

                            logging.info("Forwarded uid=%s without attachments (attempt %d)", uid, attempts)
                            success = True
                        except Exception as e2:
                            logging.exception("Failed forwarding uid=%s without attachments: %s", uid, e2)
                            time.sleep(1)
                    else:
                        # 重新连接 IMAP
                        try:
                            imap.logout()
                        except Exception:
                            pass
                        imap = imap_connect(cfg)
                        imap.login(cfg.src_email, cfg.src_password)
                        _ensure_selected(imap, cfg.imap_folder)
                        time.sleep(1)
                except Exception as e:
                    logging.exception("Failed forwarding uid=%s with attachments attempt=%d: %s", uid, attempts, e)
                    
                    # 如果是第一次失败，尝试降级处理：只转发标题和正文
                    if attempts == 1:
                        logging.info("Attempting to forward without attachments for uid=%s", uid)
                        try:
                            # 获取邮件头部和正文文本
                            raw_header, raw_body = _imap_fetch_header_and_text(imap, uid)
                            
                            # 构造无附件的转发邮件
                            fwd = _build_forward_message_no_attachment(cfg, raw_header, raw_body, original_uid=str(uid))
                            
                            # 发送邮件
                            smtp.send_message(fwd)
                            
                            # 成功后标记为已读
                            _imap_mark_forwarded(imap, uid)
                            forwarded += 1

                            if last_uid_int is None or uid > last_uid_int:
                                last_uid_int = uid
                                state[state_key] = last_uid_int
                                save_state(state)

                            logging.info("Forwarded uid=%s without attachments (attempt %d)", uid, attempts)
                            success = True
                        except Exception as e2:
                            logging.exception("Failed forwarding uid=%s without attachments: %s", uid, e2)
                            time.sleep(1)
                    else:
                        time.sleep(1)

            if not success:
                # Skip this UID to avoid blocking subsequent messages
                last_uid_int = uid
                state[state_key] = last_uid_int
                save_state(state)
                logging.warning("Skipped uid=%s after %d failed attempt(s)", uid, attempts)
            
            # 增加延迟，降低频率
            time.sleep(3)

        return forwarded
    finally:
        try:
            if smtp is not None:
                smtp.quit()
        except Exception:
            pass
        try:
            if imap is not None:
                imap.logout()
        except Exception:
            pass


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Auto-forward mail from IMAP to another email via SMTP.")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle then exit.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config()

    if args.once:
        n = process_once(cfg)
        logging.info("Done. Forwarded %d message(s).", n)
        return

    logging.info("Starting loop. Poll interval=%ss", cfg.poll_interval_seconds)
    while True:
        try:
            n = process_once(cfg)
            logging.info("Cycle done. Forwarded %d message(s). Sleeping...", n)
        except Exception as e:
            logging.exception("Cycle failed: %s. Sleeping...", e)
        time.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    main()
