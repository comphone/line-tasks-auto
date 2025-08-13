import hmac
import hashlib
import base64

# --- กรุณากรอก Channel Secret ของคุณในบรรทัดนี้ ---
channel_secret = 'a7ecc4dfbe87894dad36542c5d91c135'

# --- ข้อมูลที่ได้มาจาก Webhook.site ---
request_body = '{"destination":"U6d3be9943bf8d1d8a98006c10d79fa35","events":[]}'
signature_from_line = 'omUc+b9Bw4CCOtFt+gGTgdvbBnaUaI3BTH9C/Tq+waI='

# --- โค้ดสำหรับตรวจสอบลายเซ็นด้วยตัวเอง ---
hash_obj = hmac.new(
    channel_secret.encode('utf-8'),
    request_body.encode('utf-8'),
    hashlib.sha256
).digest()

my_signature = base64.b64encode(hash_obj).decode('utf-8')

# --- เปรียบเทียบผลลัพธ์ ---
print(f"Signature from LINE:      {signature_from_line}")
print(f"My Calculated Signature:  {my_signature}")
print("---------------------------------------------------------")

if my_signature == signature_from_line:
    print("✅ SUCCESS: Signature Matched! The Channel Secret is CORRECT.")
else:
    print("❌ FAILED: Signature Mismatch. The Channel Secret is INCORRECT.")