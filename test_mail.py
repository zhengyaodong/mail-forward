import imaplib
import smtplib
import email
import time
import re
from email.header import decode_header
from email.message import EmailMessage
from email.policy import default

# --- 配置信息 ---
IMAP_SERVER = 'imap.gzus.edu.cn'
IMAP_USER = 'zyd@mail.gzus.edu.cn'
IMAP_PASS = '' 
SMTP_SERVER = 'smtp.qq.com'
SMTP_USER = '283406@qq.com'
SMTP_PASS = '' 

def decode_str(s):
    if not s: return ""
    parts = decode_header(s)
    decoded = []
    for value, charset in parts:
        if isinstance(value, bytes):
            value = value.decode(charset if charset else 'utf-8', errors='ignore')
        decoded.append(str(value))
    return re.sub(r'\s+', ' ', "".join(decoded)).strip()

def get_one_mail_and_forward(mail_id):
    mail = None
    smtp_server = None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, 993)
        mail.login(IMAP_USER, IMAP_PASS)
        mail.select("inbox")
        
        # --- 核心改进：只抓取头部和正文文本部分，不抓取附件二进制流 ---
        # 使用 PEEK 不会将邮件标记为已读，直到我们转发成功手动标记
        res, data = mail.fetch(mail_id, '(BODY.PEEK[HEADER] BODY.PEEK[TEXT])')
        if res != 'OK' or not data: return False
        
        raw_header = ""
        raw_body = ""
        for part in data:
            if isinstance(part, tuple):
                if b'HEADER' in part[0]:
                    raw_header = part[1]
                elif b'TEXT' in part[0]:
                    raw_body = part[1]

        # 解析头部
        header_msg = email.message_from_bytes(raw_header, policy=default)
        subject = decode_str(header_msg.get('Subject'))
        from_info = decode_str(header_msg.get('From'))

        # 解析正文（解决 Base64 乱码和 HTML 显示问题）
        # 即使 TEXT 部分很大，也比带附件的 RFC822 小得多，网关通常会放行
        temp_msg = email.message_from_bytes(raw_body, policy=default)
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
        forward_msg['From'] = SMTP_USER
        forward_msg['To'] = SMTP_USER
        
        notice = f"--- 学校邮箱转发 (已自动忽略附件以防断连) ---\n发件人: {from_info}\n主题: {subject}\n\n"
        
        if is_html:
            header_html = f"<div style='background:#f9f9f9;padding:10px;border:1px solid #eee'><b>发件人:</b> {from_info}<br><b>主题:</b> {subject}<br><i>[提示] 附件已忽略，请至学校邮箱下载</i></div><br>"
            forward_msg.add_alternative(header_html + body_content, subtype='html')
        else:
            forward_msg.set_content(notice + body_content)

        # 发送
        smtp_server = smtplib.SMTP_SSL(SMTP_SERVER, 465)
        smtp_server.login(SMTP_USER, SMTP_PASS)
        smtp_server.send_message(forward_msg)
        
        # 成功后标记为已读
        mail.store(mail_id, '+FLAGS', '\\Seen')
        print(f"成功转发正文: {subject}")
        return True

    except Exception as e:
        print(f"邮件 ID {mail_id} 转发失败: {e}")
        return False
    finally:
        if smtp_server: 
            try: smtp_server.quit()
            except: pass
        if mail: 
            try: mail.logout()
            except: pass

def main():
    try:
        temp_mail = imaplib.IMAP4_SSL(IMAP_SERVER, 993)
        temp_mail.login(IMAP_USER, IMAP_PASS)
        temp_mail.select("inbox")
        
        # 搜索未读
        status, response = temp_mail.search(None, 'UNSEEN')
        if not response or not response[0]:
            print("没有未读新邮件。")
            temp_mail.logout()
            return

        mail_ids = response[0].split()
        temp_mail.logout()

        # 处理最新的 5 封
        for m_id in mail_ids[-5:]:
            get_one_mail_and_forward(m_id)
            time.sleep(3) # 增加延迟，降低频率
            
    except Exception as e:
        print(f"全局连接错误: {e}")

if __name__ == "__main__":
    main()
