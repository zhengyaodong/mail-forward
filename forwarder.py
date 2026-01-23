import json
import logging
import os
import ssl
import time
import re
import mimetypes
import imaplib
import smtplib
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage
from email.policy import default
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# 状态记录文件路径
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

# --- 辅助工具函数 ---

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None: return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}

def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not v.strip(): return default
    try:
        return int(v)
    except ValueError:
        return default

def decode_str(s):
    if not s: return ""
    parts = decode_header(s)
    decoded = []
    for value, charset in parts:
        if isinstance(value, bytes):
            value = value.decode(charset if charset else 'utf-8', errors='ignore')
        decoded.append(str(value))
    return re.sub(r'\s+', ' ', "".join(decoded)).strip()

# --- 配置与状态管理 ---

def load_config() -> Config:
    if load_dotenv:
        load_dotenv(override=False)
    return Config(
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

def load_state() -> dict:
    if not STATE_PATH.exists(): return {}
    try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception: return {}

def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# --- 网络连接函数 ---

def imap_connect(cfg: Config):
    use_ssl = cfg.imap_ssl or cfg.imap_port == 993
    if use_ssl:
        return imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=cfg.imap_timeout)
    return imaplib.IMAP4(cfg.imap_host, cfg.imap_port, timeout=cfg.imap_timeout)

def smtp_connect(cfg: Config) -> smtplib.SMTP:
    if cfg.smtp_ssl:
        context = ssl.create_default_context()
        return smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=120)
    server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
    server.starttls(context=ssl.create_default_context())
    return server

# --- 邮件抓取逻辑 ---

def _imap_fetch_full_message(imap: imaplib.IMAP4, uid: int, chunk_size=512*1024) -> bytes:
    """分片下载，解决大 PDF 导致的连接重置问题"""
    typ, data = imap.uid("fetch", str(uid), "(RFC822.SIZE)")
    if typ != "OK" or not data:
        raise RuntimeError(f"Failed to get size for UID {uid}")
    
    raw_resp = data[0] if not isinstance(data[0], tuple) else data[0][0]
    size_match = re.search(rb'RFC822\.SIZE\s+(\d+)', raw_resp)
    
    if not size_match:
        logging.warning("Size match failed, fallback to direct fetch")
        typ, data = imap.uid("fetch", str(uid), "(RFC822)")
        return data[0][1]

    total_size = int(size_match.group(1))
    logging.info(f"Downloading UID {uid}: {total_size} bytes in {chunk_size//1024}KB chunks")

    full_content = bytearray()
    offset = 0
    while offset < total_size:
        read_len = min(chunk_size, total_size - offset)
        fetch_cmd = f"(BODY.PEEK[]<{offset}.{read_len}>)"
        
        typ, chunk_data = imap.uid("fetch", str(uid), fetch_cmd)
        if typ == "OK":
            for item in chunk_data:
                if isinstance(item, tuple):
                    full_content.extend(item[1])
                    break
        else:
            raise RuntimeError(f"Chunk fetch failed at {offset}")
        offset += read_len
    return bytes(full_content)

def _imap_fetch_header_and_text(imap: imaplib.IMAP4, uid: int) -> tuple[bytes, bytes]:
    typ, data = imap.uid("fetch", str(uid), "(BODY.PEEK[HEADER] BODY.PEEK[TEXT])")
    raw_header, raw_body = b"", b""
    for part in data:
        if isinstance(part, tuple):
            if b'HEADER' in part[0]: raw_header = part[1]
            elif b'TEXT' in part[0]: raw_body = part[1]
    return raw_header, raw_body

# --- 邮件构造逻辑 ---

def _build_forward_message(cfg: Config, raw_bytes: bytes, original_uid: str) -> EmailMessage:
    original_msg = message_from_bytes(raw_bytes, policy=default)
    forward_msg = EmailMessage()
    forward_msg['Subject'] = f"[转发] {decode_str(original_msg.get('Subject'))}"
    forward_msg['From'] = cfg.smtp_user
    forward_msg['To'] = cfg.dest_email

    body_part = original_msg.get_body(preferencelist=('html', 'plain'))
    if body_part:
        content = body_part.get_content()
        if body_part.get_content_type() == 'text/html':
            forward_msg.add_alternative(content, subtype='html')
        else:
            forward_msg.set_content(content)
    
    for part in original_msg.walk():
        if part.get_content_maintype() == 'multipart' or part == body_part:
            continue
        filename = part.get_filename()
        if filename:
            filename = decode_str(filename)
            payload = part.get_payload(decode=True)
            if payload:
                ctype, _ = mimetypes.guess_type(filename)
                if not ctype: ctype = part.get_content_type()
                main, sub = ctype.split('/', 1)
                forward_msg.add_attachment(payload, maintype=main, subtype=sub, filename=filename)
    return forward_msg

def _build_forward_message_no_attachment(cfg: Config, raw_header: bytes, raw_body: bytes, original_uid: str) -> EmailMessage:
    header_msg = message_from_bytes(raw_header, policy=default)
    temp_msg = message_from_bytes(raw_body, policy=default)
    forward_msg = EmailMessage()
    forward_msg['Subject'] = f"[通知正文] {decode_str(header_msg.get('Subject'))}"
    forward_msg['From'] = cfg.smtp_user
    forward_msg['To'] = cfg.dest_email
    
    body_part = temp_msg.get_body(preferencelist=('html', 'plain'))
    content = body_part.get_content() if body_part else "(无正文)"
    forward_msg.set_content(content + "\n\n(提示：附件由于网络限制已忽略)")
    return forward_msg

# --- 任务处理流程 ---

def process_once(cfg: Config) -> int:
    state = load_state()
    state_key = f"{cfg.src_email}:{cfg.imap_host}:{cfg.imap_folder}"
    last_uid_int = int(state.get(state_key, 0)) if state.get(state_key) else None
    forwarded = 0
    imap, smtp = None, None

    try:
        imap = imap_connect(cfg)
        imap.login(cfg.src_email, cfg.src_password)
        imap.select(cfg.imap_folder)
        
        typ, data = imap.uid("search", None, "UNSEEN")
        if not data[0]: return 0
        uids = [int(x) for x in data[0].split()]

        smtp = smtp_connect(cfg)
        smtp.login(cfg.smtp_user, cfg.smtp_password)

        for uid in uids:
            success = False
            for attempt in range(1, 4):
                try:
                    # 检查连接
                    try: 
                        imap.noop()
                    except:
                        imap = imap_connect(cfg)
                        imap.login(cfg.src_email, cfg.src_password)
                        imap.select(cfg.imap_folder)

                    if attempt < 3:
                        raw_bytes = _imap_fetch_full_message(imap, uid)
                        fwd = _build_forward_message(cfg, raw_bytes, str(uid))
                    else:
                        logging.warning(f"Final attempt for UID {uid}: Text only mode")
                        h, b = _imap_fetch_header_and_text(imap, uid)
                        fwd = _build_forward_message_no_attachment(cfg, h, b, str(uid))

                    smtp.send_message(fwd)
                    imap.uid("store", str(uid), "+FLAGS", r"(\Seen)")
                    forwarded += 1
                    state[state_key] = max(last_uid_int or 0, uid)
                    save_state(state)
                    logging.info(f"UID {uid} Forwarded.")
                    success = True
                    break
                except Exception as e:
                    logging.warning(f"Error on UID {uid}, attempt {attempt}: {e}")
                    time.sleep(2)

            if not success:
                logging.error(f"Skipping UID {uid} after 3 attempts.")
        return forwarded
    finally:
        if smtp:
            try:
                smtp.quit()
            except:
                pass
        if imap:
            try:
                imap.logout()
            except:
                pass

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    if args.once:
        n = process_once(cfg)
        logging.info(f"Done. {n} message(s).")
    else:
        while True:
            try:
                process_once(cfg)
            except Exception as e:
                logging.error(f"Loop error: {e}")
            time.sleep(cfg.poll_interval_seconds)

if __name__ == "__main__":
    main()
