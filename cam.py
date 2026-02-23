import logging
from miio import MiotDevice

# ==========================================
# ▼ 여기에 실제 정보를 입력해야 합니다!
IP = "192.168.0.17"
TOKEN = "3233784c4a4e4d6c4162777738653067" 
# 예: TOKEN = "54696d65612d323032332d..."
# ==========================================

# 로그 레벨 설정 (필요시 DEBUG로 변경)
logging.basicConfig(level=logging.INFO)

print(f"📡 {IP} 카메라에 연결 시도 중...")

try:
    # 최신 샤오미 카메라는 MiotDevice를 사용해야 합니다.
    # mapping은 비워두어도 기본 정보 조회는 가능합니다.
    cam = MiotDevice(ip=IP, token=TOKEN, mapping={})
    
    # 1. 기기 정보 조회
    info = cam.info()
    print("\n✅ 연결 성공!")
    print(f"모델명: {info.model}")
    print(f"펌웨어: {info.firmware_version}")
    print(f"MAC주소: {info.mac_address}")

except Exception as e:
    print(f"\n❌ 연결 실패: {e}")
    print("👉 토큰 값이 정확한지 다시 한 번 확인해주세요.")