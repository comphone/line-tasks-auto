import requests

# 🔷 Organization ของคุณ
organization = "comphone"  # เปลี่ยนตามจริงถ้าไม่ใช่

# 🔷 Token ของคุณ
token = "3549607a6460e1aafec7d63ae6b493c98c53b537"

# 🔷 URL สำหรับ API
url = f"https://sonarcloud.io/api/projects/search?organization={organization}"

# 🔷 ส่ง HTTP Basic Auth ด้วย token
auth = (token, "")

# 🔷 เรียก API
response = requests.get(url, auth=auth)

if response.status_code == 200:
    data = response.json()
    projects = data.get("components", [])

    if projects:
        print(f"✅ Found {len(projects)} projects in organization '{organization}':\n")
        for project in projects:
            name = project.get("name")
            key = project.get("key")
            print(f"- Name: {name}")
            print(f"  Key: {key}\n")
    else:
        print("⚠️ No projects found in this organization.")
else:
    print(f"❌ Failed to fetch projects. Status code: {response.status_code}")
    print(response.text)
