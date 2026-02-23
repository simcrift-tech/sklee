import os
import sys
import time
import socket
import json
import hashlib
import mimetypes
import urllib.request
import xml.etree.ElementTree as ET
import tinytuya  # [필수] pip install tinytuya
import threading
import cv2       # [필수] pip install opencv-python
import mss       # [필수] pip install mss
import numpy as np # [필수] pip install numpy
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)

# --- 기본 설정 ---
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
DATA_FILE = os.path.join(BASE_DIR, 'data.json')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

ALLOWED_EXTENSIONS = {'mp4', 'webm', 'ogg', 'mov', 'jpg', 'jpeg', 'png', 'pdf'}
DEFAULT_PASSWORD_HASH = "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4"

# [중요] Tinytuya 연결 객체를 저장할 전역 메모리 변수
iot_devices_map = {}

# [IOT] 기본 장비 데이터 템플릿
# ★★★ 주의: 여기에 실제 TV/플러그의 ID와 Key를 입력해야 작동합니다! ★★★
DEFAULT_IOT_DEVICES = [
    {
        'name': 'Lobby_TV', 
        'id': 'bf0xxxxxxxxxxxxxxx',  # [수정필요] 실제 Device ID
        'key': 'a1b2xxxxxxxxxxxx',   # [수정필요] 실제 Local Key
        'ip': '192.168.0.',        # [수정필요] 실제 IP 주소
        'version': 3.3,              # 프로토콜 버전 (3.1, 3.3, 3.4 중 하나)
        'icon': 'tv',
        'group': '1층 로비',
        'status': False
    }
]

def load_data_from_file():
    if not os.path.exists(DATA_FILE):
        default_data = {
            'videos': [], 'history': [], 'library': [],
            'marquee': {'active': False, 'text': '긴급 공지사항입니다.', 'color': '#ffffff', 'bg': '#ef4444', 'size': '3rem', 'speed': '30s'},
            'iot_devices': DEFAULT_IOT_DEVICES,
            'storage': {'total': 300 * 1024, 'used': 0}
        }
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_data, f, indent=4)
        return default_data
    else:
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if 'iot_devices' not in data: data['iot_devices'] = DEFAULT_IOT_DEVICES
                return data
        except: return {}

if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump({'password_hash': DEFAULT_PASSWORD_HASH}, f)

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
current_broadcast = { 'is_live': False, 'type': None, 'src': None, 'title': '', 'timestamp': 0 }

# --- [기능 1] 화면 캡처 제너레이터 (화면 송출용) ---
def generate_screen_stream():
    with mss.mss() as sct:
        # 모니터 1번 캡처
        if not sct.monitors: return b''
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        
        while True:
            try:
                screenshot = sct.grab(monitor)
                img_np = np.array(screenshot)
                frame = cv2.cvtColor(img_np, cv2.COLOR_BGRA2BGR)
                # 전송 속도를 위해 720p로 리사이즈
                frame = cv2.resize(frame, (1280, 720)) 
                ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
                frame_bytes = buffer.tobytes()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                time.sleep(0.05) # 약 20fps
            except Exception as e:
                print(f"Screen Capture Error: {e}")
                time.sleep(1)

# --- 유틸리티 함수 ---
def get_password_hash(pwd): return hashlib.sha256(pwd.encode()).hexdigest()
def check_password(pwd):
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f).get('password_hash') == get_password_hash(pwd)
    except: return False
def update_password(new_pwd):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump({'password_hash': get_password_hash(new_pwd)}, f)
        return True
    except: return False
def allowed_file(filename): return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
def get_ip_address():
    try: s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except: return "127.0.0.1"

# --- [기능 2] IoT 장비 초기화 함수 (강화됨) ---
def init_iot_devices():
    global iot_devices_map
    print("\n" + "="*50)
    print("🔌 [System] IoT 장비 연결 초기화 시작...")
    
    data = load_data_from_file()
    device_list = data.get('iot_devices', [])
    iot_devices_map = {}
    
    if not device_list:
        print("   ⚠️  등록된 장비가 없습니다. (data.json 확인)")
    
    for config in device_list:
        dev_id = config.get('id')
        dev_ip = config.get('ip')
        dev_key = config.get('key')
        
        # ID나 IP가 없거나 dummy 데이터인 경우 스킵
        if not dev_id or 'dummy' in dev_id or not dev_ip: 
            print(f"   ⚠️  스킵됨: {config.get('name')} (유효하지 않은 정보)")
            continue
        
        print(f"   ⏳ 연결 시도: {config['name']} ({dev_ip})...")
        try:
            # tinytuya 객체 생성
            d = tinytuya.OutletDevice(
                dev_id=dev_id,
                address=dev_ip, 
                local_key=dev_key, 
                connection_timeout=2
            )
            
            version = float(config.get('version', 3.4))
            d.set_version(version)
            d.set_socketPersistent(True) 

            # 간단한 상태 조회로 연결 테스트
            try:
                status = d.status()
                if status and 'dps' in status:
                    print(f"      ✅ 연결 성공! (상태: {status['dps']})")
                else:
                    print(f"      ⚠️  연결은 됐으나 상태 응답 없음 (프로토콜 버전 확인 필요)")
            except:
                print(f"      ⚠️  연결 테스트 실패 (장비가 오프라인일 수 있음)")

            iot_devices_map[dev_id] = { 
                'device': d, 
                'name': config['name'],
                'ip': dev_ip
            }
            
        except Exception as e:
            print(f"      ❌ 객체 생성 실패: {e}")
            
    print("🔌 [System] 초기화 완료.")
    print("="*50 + "\n")

# --- API ---

# 1. 화면 송출 API
@app.route('/stream_screen')
def stream_screen():
    return Response(generate_screen_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/data', methods=['GET'])
def get_data():
    data = load_data_from_file()
    total_size = 0
    if os.path.exists(UPLOAD_FOLDER):
        for f in os.listdir(UPLOAD_FOLDER):
            fp = os.path.join(UPLOAD_FOLDER, f)
            if os.path.isfile(fp): total_size += os.path.getsize(fp)
    data['storage']['used'] = round(total_size / (1024 * 1024), 2)
    return jsonify(data)

@app.route('/api/data', methods=['POST'])
def save_data():
    try:
        current = load_data_from_file()
        current.update(request.json)
        with open(DATA_FILE, 'w', encoding='utf-8') as f: json.dump(current, f, ensure_ascii=False, indent=4)
        return jsonify({'success': True})
    except: return jsonify({'success': False}), 500

# 2. IoT 상태 확인 API
@app.route('/api/iot/status', methods=['GET'])
def get_iot_status():
    data = load_data_from_file()
    devices_config = data.get('iot_devices', [])
    status_list = []

    for conf in devices_config:
        dev_id = conf.get('id')
        is_on = False
        
        if dev_id in iot_devices_map:
            try:
                device = iot_devices_map[dev_id]['device']
                # status() 호출 시 에러가 나면 except로 빠짐
                status_data = device.status() 
                
                if status_data and 'dps' in status_data:
                    dps = status_data['dps']
                    # Tuya 전원 키 찾기 (1, 20, 9 등)
                    found_key = False
                    for k in ['1', '20', '9']:
                        if k in dps:
                            is_on = bool(dps[k])
                            found_key = True
                            break
                    if not found_key: is_on = True # 상태는 왔는데 키를 모르면 켜짐으로 간주
                else:
                    # 응답이 비어있으면 오프라인
                    is_on = False
            except Exception as e:
                # 타임아웃 등 연결 실패
                # print(f"Status check error: {e}") 
                is_on = False
        
        status_list.append({
            'id': dev_id,
            'name': conf.get('name'),
            'icon': conf.get('icon', 'tv'),
            'isOn': is_on,
            'schedule': conf.get('schedule', {'enabled': False})
        })
        
    return jsonify(status_list)

# 3. IoT 제어 API
@app.route('/api/iot/control', methods=['POST'])
def control_iot_device():
    req = request.json
    dev_id = req.get('id')
    action = req.get('action')
    
    print(f"📡 [Control] 요청 수신: ID={dev_id}, Action={action}")

    success = False
    error_msg = ""

    if dev_id in iot_devices_map:
        try:
            device = iot_devices_map[dev_id]['device']
            print(f"   ↳ 제어 명령 전송 중...")
            
            if action == 'on':
                res = device.turn_on()
            else:
                res = device.turn_off()
            
            print(f"   ↳ 장비 응답: {res}")
            
            # 응답 확인
            if res and 'Error' not in str(res): 
                success = True
                time.sleep(0.5) # 상태 반영 대기
            else:
                error_msg = f"장비 응답 에러: {res}"
                print(f"   ❌ {error_msg}")

        except Exception as e:
            error_msg = f"통신 예외 발생: {str(e)}"
            print(f"   ❌ {error_msg}")
    else:
        error_msg = "초기화된 장비 객체를 찾을 수 없습니다. (ID 불일치)"
        print(f"   ❌ {error_msg}")

    # 성공 시 파일 상태 업데이트
    if success:
        data = load_data_from_file()
        devices = data.get('iot_devices', [])
        for d in devices:
            if d['id'] == dev_id:
                d['status'] = (action == 'on')
                break
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        
    return jsonify({'success': success, 'message': error_msg})

@app.route('/api/iot/schedule', methods=['POST'])
def save_iot_schedule():
    req = request.json
    dev_id = req.get('id')
    data = load_data_from_file()
    devices = data.get('iot_devices', [])
    for d in devices:
        if d['id'] == dev_id:
            d['schedule'] = {
                'on_time': req.get('on_time'),
                'off_time': req.get('off_time'),
                'enabled': req.get('enabled')
            }
            break
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    return jsonify({'success': True})

@app.route('/api/settings/iot', methods=['POST'])
def save_iot_settings():
    if not check_password(request.json.get('password')): return jsonify({'success': False}), 403
    try:
        data = load_data_from_file()
        data['iot_devices'] = request.json.get('devices', [])
        with open(DATA_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=4)
        
        # 설정 변경 즉시 재연결 시도
        init_iot_devices()
        return jsonify({'success': True})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    if not check_password(request.form.get('password')): return jsonify({'success': False}), 403
    f = request.files.get('file')
    if not f or f.filename == '' or not allowed_file(f.filename): return jsonify({'success': False}), 400
    fn = secure_filename(f.filename); sn = f"{int(time.time())}_{fn}"
    path = os.path.join(app.config['UPLOAD_FOLDER'], sn); f.save(path)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    ext = fn.rsplit('.', 1)[1].lower()
    ftype = 'video' if ext in ['mp4','webm','mov'] else ('image' if ext in ['jpg','png','jpeg'] else 'doc')
    return jsonify({'success': True, 'url': f'/uploads/{sn}', 'filename': fn, 'real_filename': sn, 'size': f"{size_mb:.2f} MB", 'file_type': ftype})

@app.route('/delete_file', methods=['POST'])
def delete_single_file():
    if not check_password(request.json.get('password')): return jsonify({'success': False}), 403
    try:
        p = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(request.json.get('filename')))
        if os.path.exists(p): os.remove(p)
        return jsonify({'success': True})
    except: return jsonify({'success': False}), 500

@app.route('/clear_files', methods=['POST'])
def clear_all_files():
    if not check_password(request.json.get('password')): return jsonify({'success': False}), 403
    try:
        for f in os.listdir(UPLOAD_FOLDER): os.remove(os.path.join(UPLOAD_FOLDER, f))
        return jsonify({'success': True})
    except: return jsonify({'success': False}), 500

@app.route('/api/verify_password', methods=['POST'])
def verify_password_api(): return jsonify({'success': True}) if check_password(request.json.get('password')) else (jsonify({'success': False}), 403)

@app.route('/api/change_password', methods=['POST'])
def change_password_api():
    d = request.json
    if not check_password(d.get('current_password')): return jsonify({'success': False, 'message': '비밀번호 오류'}), 403
    return jsonify({'success': True}) if update_password(d.get('new_password')) else (jsonify({'success': False}), 500)

@app.route('/api/live/update', methods=['POST'])
def update_live_status():
    current_broadcast.update(request.json); current_broadcast['timestamp'] = time.time()
    return jsonify({'success': True})

@app.route('/api/live/status', methods=['GET'])
def get_live_status(): return jsonify(current_broadcast)

@app.route('/api/news', methods=['GET'])
def get_news():
    try:
        url = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            xml_data = response.read(); root = ET.fromstring(xml_data)
            headlines = [item.find('title').text for item in root.findall('.//item')[:5]]
            return jsonify({'success': True, 'text': "   📢   ".join(headlines)})
    except: return jsonify({'success': False, 'text': '뉴스 정보를 불러오지 못했습니다.'})

@app.route('/')
def index(): return render_template('index.html', server_ip=get_ip_address())
@app.route('/m')
def mobile_index(): return render_template('mindex.html')
@app.route('/uploads/<name>')
def download_file(name): return send_from_directory(app.config['UPLOAD_FOLDER'], name)

if __name__ == '__main__':
    # 1. 서버 시작 전 IoT 장비 연결을 명시적으로 시도
    # (flask run으로 실행 시 이 부분은 실행되지 않으므로 주의!)
    try:
        init_iot_devices()
    except Exception as e:
        print(f"초기화 중 오류 발생: {e}")

    # 2. 서버 정보 출력
    ip = get_ip_address()
    print("\n" + "="*50)
    print(f"🚀 LG전자 구독&케어 CMS Server Started")
    print(f"👉 접속 주소: http://{ip}:5000")
    print(f"📺 화면 송출: http://{ip}:5000/stream_screen")
    print("="*50 + "\n")
    
    app.run(host='0.0.0.0', port=5000, debug=True)