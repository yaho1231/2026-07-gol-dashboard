import os
from dotenv import load_dotenv

# Load from project root .env
dotenv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
load_dotenv(dotenv_path)

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
# DeepLOL Clash 커스텀 코드 생성용 비밀번호. (.env 에서 설정)
CLASH_PASSWORD = os.getenv("CLASH_PASSWORD", "")

# 봇 기본 상태 메시지("듣는 중" 자리). 운영 서버의 담당자 표기 등은 .env 에서 넣는다.
BOT_DEFAULT_STATUS = os.getenv("BOT_DEFAULT_STATUS", "").strip() or "플레이어 등록문의는 서버 관리자에게"

# 서버의 Administrator 권한이 없어도 관리자 전용 명령(/등록·/계정추가·/등록취소)을
# 쓸 수 있는 디스코드 유저 ID 화이트리스트.
# .env 에 ADMIN_DISCORD_IDS=아이디1,아이디2 형태로 콤마로 구분해 설정한다.
ADMIN_DISCORD_IDS = {
    uid.strip()
    for uid in os.getenv("ADMIN_DISCORD_IDS", "").split(",")
    if uid.strip()
}
