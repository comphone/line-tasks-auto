@echo off
echo 🔷 กำลังลบโฟลเดอร์ __pycache__ ทั้งหมด...

for /d /r %%i in (__pycache__) do (
    echo 🗑️ กำลังลบ %%i
    rmdir /s /q "%%i"
)

echo ✅ เสร็จสิ้น!
pause
