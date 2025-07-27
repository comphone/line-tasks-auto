import os
import datetime
import json
import pytz
from flask import current_app, url_for
from dateutil.parser import parse as date_parse

from linebot.models import (
    TextSendMessage, FlexSendMessage, CarouselContainer, BubbleContainer,
    BoxComponent, TextComponent, SeparatorComponent, ButtonComponent,
    URIAction, PostbackAction, QuickReply, QuickReplyButton
)

import google_services as gs
import utils
from settings_manager import get_app_settings

# --- Helper functions for creating LINE messages ---

def create_task_list_message(title, tasks, limit=5):
    """สร้างข้อความสรุปรายการงานแบบสั้นๆ"""
    if not tasks:
        return TextSendMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")
    
    message = f"📋 {title}\n\n"
    tasks.sort(key=lambda x: date_parse(x['due']) if x.get('due') else datetime.datetime.max.replace(tzinfo=pytz.utc))
    
    for i, task in enumerate(tasks[:limit]):
        customer = utils.parse_customer_info_from_notes(task.get('notes', ''))
        due = utils.parse_google_task_dates(task).get('due_formatted', 'ไม่มีกำหนด')
        message += f"{i+1}. {task.get('title')}\n   - ลูกค้า: {customer.get('name', 'N/A')}\n   - นัดหมาย: {due}\n\n"
    
    if len(tasks) > limit:
        message += f"... และอีก {len(tasks) - limit} รายการ"
        
    return TextSendMessage(text=message)

def create_full_summary_message(title, tasks):
    """สร้างข้อความสรุปรายการงานแบบเต็ม (สำหรับงานค้าง)"""
    if not tasks:
        return TextSendMessage(text=f"ไม่พบรายการ{title}ในขณะนี้")

    tasks.sort(key=lambda x: date_parse(x.get('due')) if x.get('due') else date_parse(x.get('created', '9999-12-31T23:59:59Z')))
    
    lines = [f"📋 {title} (ทั้งหมด {len(tasks)} งาน)\n"]
    for i, task in enumerate(tasks):
        customer = utils.parse_customer_info_from_notes(task.get('notes', ''))
        due = utils.parse_google_task_dates(task).get('due_formatted', 'ยังไม่ระบุ')
        line = f"{i+1}. {task.get('title', 'N/A')}"
        if customer.get('name'):
            line += f"\n   - 👤 {customer.get('name')}"
        line += f"\n   - 🗓️ {due}"
        lines.append(line)
        
    message = "\n\n".join(lines)
    if len(message) > 4900:
        message = message[:4900] + "\n\n... (ข้อความยาวเกินไป)"
        
    return TextSendMessage(text=message)

def create_task_flex_message(task):
    """สร้าง Flex Message สำหรับแสดงข้อมูลงาน 1 ชิ้น"""
    customer = utils.parse_customer_info_from_notes(task.get('notes', ''))
    dates = utils.parse_google_task_dates(task)
    
    return BubbleContainer(
        body=BoxComponent(
            layout='vertical',
            spacing='md',
            contents=[
                TextComponent(text=task.get('title', '...'), weight='bold', size='lg', wrap=True),
                SeparatorComponent(margin='md'),
                BoxComponent(
                    layout='vertical',
                    margin='lg',
                    spacing='sm',
                    contents=[
                        BoxComponent(
                            layout='baseline',
                            spacing='sm',
                            contents=[
                                TextComponent(text='ลูกค้า:', color='#AAAAAA', size='sm', flex=2),
                                TextComponent(text=customer.get('name', '-'), wrap=True, color='#666666', size='sm', flex=5)
                            ]
                        ),
                        BoxComponent(
                            layout='baseline',
                            spacing='sm',
                            contents=[
                                TextComponent(text='นัดหมาย:', color='#AAAAAA', size='sm', flex=2),
                                TextComponent(text=dates.get('due_formatted', '-'), wrap=True, color='#666666', size='sm', flex=5)
                            ]
                        )
                    ]
                ),
            ]
        ),
        footer=BoxComponent(
            layout='vertical',
            spacing='sm',
            contents=[
                ButtonComponent(
                    style='primary',
                    height='sm',
                    action=URIAction(label='📝 เปิดในเว็บ', uri=url_for('main.task_details', task_id=task['id'], _external=True))
                )
            ]
        )
    )

# --- Main Handler Functions ---

def handle_text_message(event):
    """จัดการข้อความที่ผู้ใช้ส่งมาใน LINE"""
    line_bot_api = current_app.line_bot_api
    text = event.message.text.strip().lower()
    reply = None

    if text == 'งานวันนี้':
        tasks = [
            t for t in (gs.get_google_tasks_for_report(show_completed=False) or [])
            if t.get('due') and date_parse(t['due']).astimezone(utils.THAILAND_TZ).date() == datetime.datetime.now(utils.THAILAND_TZ).date()
            and t.get('status') == 'needsAction'
        ]
        if not tasks:
            return line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ไม่พบงานสำหรับวันนี้"))
        
        tasks.sort(key=lambda x: date_parse(x['due']))
        messages = []
        for task in tasks[:5]: # ส่งไม่เกิน 5 งานเพื่อไม่ให้ spam
            customer, dates = utils.parse_customer_info_from_notes(task.get('notes', '')), utils.parse_google_task_dates(task)
            loc = f"พิกัด: {customer.get('map_url')}" if customer.get('map_url') else "พิกัด: - (ไม่มีข้อมูล)"
            msg_text = (
                f"🔔 งานสำหรับวันนี้\n\nชื่องาน: {task.get('title', '-')}\n"
                f"👤 ลูกค้า: {customer.get('name', '-')}\n"
                f"📞 โทร: {customer.get('phone', '-')}\n"
                f"🗓️ นัดหมาย: {dates.get('due_formatted', '-')}\n"
                f"📍 {loc}\n\n"
                f"🔗 ดูรายละเอียด/แก้ไข:\n{url_for('main.task_details', task_id=task.get('id'), _external=True)}"
            )
            messages.append(TextSendMessage(text=msg_text))
        return line_bot_api.reply_message(event.reply_token, messages)

    elif text == 'งานค้าง':
        tasks = [t for t in (gs.get_google_tasks_for_report(show_completed=False) or []) if t.get('status') == 'needsAction']
        reply = create_full_summary_message('รายการงานค้าง', tasks)

    elif text == 'งานเสร็จ':
        tasks = sorted(
            [t for t in (gs.get_google_tasks_for_report(show_completed=True) or []) if t.get('status') == 'completed'],
            key=lambda x: date_parse(x.get('completed', '0001-01-01T00:00:00Z')),
            reverse=True
        )
        reply = create_task_list_message('รายการงานเสร็จล่าสุด', tasks)

    elif text == 'งานพรุ่งนี้':
        tomorrow = (datetime.datetime.now(utils.THAILAND_TZ) + datetime.timedelta(days=1)).date()
        tasks = [
            t for t in (gs.get_google_tasks_for_report(show_completed=False) or [])
            if t.get('due') and date_parse(t['due']).astimezone(utils.THAILAND_TZ).date() == tomorrow
            and t.get('status') == 'needsAction'
        ]
        reply = create_task_list_message('งานพรุ่งนี้', tasks)

    elif text == 'สร้างงานใหม่' and current_app.LIFF_ID_FORM:
        reply = TextSendMessage(
            text="เปิดฟอร์มเพื่อสร้างงานใหม่ครับ 👇",
            quick_reply=QuickReply(items=[
                QuickReplyButton(action=URIAction(label="เปิดฟอร์มสร้างงาน", uri=f"https://liff.line.me/{current_app.LIFF_ID_FORM}"))
            ])
        )

    elif text.startswith('ดูงาน '):
        query = event.message.text.split(maxsplit=1)[1].strip().lower()
        if not query:
            return line_bot_api.reply_message(event.reply_token, TextSendMessage(text="โปรดระบุชื่อลูกค้าที่ต้องการค้นหา"))
        
        tasks = [
            t for t in (gs.get_google_tasks_for_report(show_completed=True) or [])
            if query in utils.parse_customer_info_from_notes(t.get('notes', '')).get('name', '').lower()
        ]
        
        if not tasks:
            return line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"ไม่พบงานของลูกค้า: {query}"))
            
        tasks.sort(key=lambda x: (x.get('status') == 'completed', date_parse(x.get('due', '9999-12-31T23:59:59Z'))))
        bubbles = [create_task_flex_message(t) for t in tasks[:10]] # แสดงไม่เกิน 10 รายการใน Carousel
        return line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text=f"ผลการค้นหา: {query}", contents=CarouselContainer(contents=bubbles)))

    elif text == 'comphone':
        help_text = (
            "พิมพ์คำสั่งเพื่อดูรายงานหรือจัดการงาน:\n"
            "- *งานค้าง*: ดูรายการงานที่ยังไม่เสร็จทั้งหมด\n"
            "- *งานเสร็จ*: ดูรายการงานที่ทำเสร็จแล้ว 5 รายการล่าสุด\n"
            "- *งานวันนี้*: ดูงานที่นัดหมายสำหรับวันนี้\n"
            "- *งานพรุ่งนี้*: ดูสรุปงานที่นัดหมายสำหรับพรุ่งนี้\n"
            "- *สร้างงานใหม่*: เปิดฟอร์มสำหรับสร้างงานใหม่\n"
            "- *ดูงาน [ชื่อลูกค้า]*: ค้นหางานตามชื่อลูกค้า\n\n"
            f"ดูข้อมูลทั้งหมด: {url_for('tools.dashboard', _external=True)}"
        )
        reply = TextSendMessage(text=help_text)
    
    if reply:
        line_bot_api.reply_message(event.reply_token, reply)

def handle_postback(event):
    """จัดการข้อมูล Postback ที่ถูกส่งกลับมาจากการกดปุ่ม"""
    line_bot_api = current_app.line_bot_api
    data = dict(x.split('=') for x in event.postback.data.split('&'))
    action = data.get('action')
    task_id = data.get('task_id')

    if action == 'customer_feedback':
        task = gs.get_single_task(task_id)
        if not task: return

        notes = task.get('notes', '')
        feedback = utils.parse_customer_feedback_from_notes(notes)
        feedback.update({
            'feedback_date': datetime.datetime.now(utils.THAILAND_TZ).isoformat(),
            'feedback_type': data.get('feedback'),
            'customer_line_user_id': event.source.user_id
        })
        
        history_reports, base = utils.parse_tech_report_from_notes(notes)
        reports_text = "".join([f"\n\n--- TECH_REPORT_START ---\n{json.dumps(r, ensure_ascii=False, indent=2)}\n--- TECH_REPORT_END ---" for r in history_reports])
        
        final_notes = f"{base.strip()}"
        if reports_text:
            final_notes += reports_text
        final_notes += f"\n\n--- CUSTOMER_FEEDBACK_START ---\n{json.dumps(feedback, ensure_ascii=False, indent=2)}\n--- CUSTOMER_FEEDBACK_END ---"
        
        gs.update_google_task(task_id, notes=final_notes)
        current_app.cache.clear()
        
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ขอบคุณสำหรับคำยืนยันครับ/ค่ะ 🙏"))
        except Exception:
            pass # อาจเกิด error ถ้าผู้ใช้กดปุ่มแล้วบล็อคบอททันที