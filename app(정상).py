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
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)

# --- 설정 ---
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
DATA_FILE = os.path.join(BASE_DIR, 'data.json')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

ALLOWED_EXTENSIONS = {'mp4', 'webm', 'ogg', 'mov', 'jpg', 'jpeg', 'png', 'pdf'}
DEFAULT_PASSWORD_HASH = "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4"

# Tinytuya 연결 객체를 저장할 전역 변수
iot_devices_map = {}

# [IOT] 기본 장비 데이터 (예시)
# 주의: id, key, ip, mac은 tinytuya.json wizard 실행 후 얻은 실제 값을 넣어야 작동합니다.
DEFAULT_IOT_DEVICES = [
    {
        'name': 'Lobby_Main_TV', 
        'id': 'bf0xxxxxxxxxxxxxxx',  # 실제 Device ID
        'key': '12xxxxxxxxxxxxxx',   # 실제 Local Key
        'ip': '192.168.0.10',        # 실제 IP
        'version': 3.3,              # 프로토콜 버전 (3.1, 3.3, 3.4)
        'icon': 'tv',
        'group': '1층 로비'
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

# --- Helper Functions ---
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

# --- [IOT] 초기화 함수 (요청하신 부분 반영) ---
def init_iot_devices():
    global iot_devices_map
    print("🔌 TV 제어 시스템 초기화 중...")
    
    data = load_data_from_file()
    device_list = data.get('iot_devices', [])
    iot_devices_map = {}
    
    for config in device_list:
        dev_id = config.get('id')
        if not dev_id or 'dummy' in dev_id: continue
        
        try:
            # tinytuya OutletDevice 객체 생성
            d = tinytuya.OutletDevice(
                dev_id=dev_id, 
                address=config.get('ip'), # IP 주소를 명시하면 연결 속도가 훨씬 빠릅니다.
                local_key=config.get('key'), 
                connection_timeout=2
            )
            
            version = float(config.get('version', 3.3))
            d.set_version(version)
            d.set_socketPersistent(True) # 소켓 연결 유지 (반응 속도 향상)

            iot_devices_map[dev_id] = { 
                'device': d, 
                'name': config['name'], 
                'icon': config.get('icon', 'tv')
            }
            print(f"   ✅ [{config['name']}] 연결 객체 생성 (v{version})")
        except Exception as e:
            print(f"   ❌ [{config['name']}] 객체 생성 실패: {e}")

# --- API ---
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

# [IOT] 상태 확인 API (tinytuya status 사용)
@app.route('/api/iot/status', methods=['GET'])
def get_iot_status():
    data = load_data_from_file()
    devices_config = data.get('iot_devices', [])
    status_list = []

    for conf in devices_config:
        dev_id = conf.get('id')
        is_on = False # 기본값: 꺼짐/오프라인
        
        # 메모리에 로드된 기기인지 확인
        if dev_id in iot_devices_map:
            try:
                device = iot_devices_map[dev_id]['device']
                # 실제 장비 상태 조회 (dps)
                status_data = device.status() 
                
                # status() 결과 예시: {'dps': {'1': True, '9': 0}, ...}
                if status_data and 'dps' in status_data:
                    # Tuya 스마트 플러그/스위치의 '1'번 dps가 보통 전원(True/False)
                    # 모델에 따라 '20'번일 수도 있음. 일단 '1'번 확인
                    dps = status_data['dps']
                    if '1' in dps:
                        is_on = bool(dps['1'])
                    elif '20' in dps:
                        is_on = bool(dps['20'])
                    else:
                        # dps는 왔는데 전원 키를 모르면 일단 온라인(True)으로 간주하거나, 
                        # 단순히 Alive 상태만 보려면 True로 설정
                        is_on = True 
            except Exception as e:
                print(f"Status check failed for {conf['name']}: {e}")
                is_on = False
        
        status_list.append({
            'id': dev_id,
            'name': conf.get('name'),
            'icon': conf.get('icon', 'tv'),
            'isOn': is_on, # True면 초록불(숨쉬기), False면 회색불
            'schedule': conf.get('schedule', {'enabled': False})
        })
        
    return jsonify(status_list)

# [IOT] 제어 API (tinytuya 제어 사용)
@app.route('/api/iot/control', methods=['POST'])
def control_iot_device():
    req = request.json
    dev_id = req.get('id')
    action = req.get('action') # 'on' or 'off'
    
    success = False
    
    # 1. 실제 장비 제어
    if dev_id in iot_devices_map:
        try:
            device = iot_devices_map[dev_id]['device']
            if action == 'on':
                device.turn_on()
            else:
                device.turn_off()
            success = True
            time.sleep(0.5) # 기기가 상태 반영할 시간 대기
        except Exception as e:
            print(f"Control failed: {e}")
            success = False

    # 2. 파일 상태 업데이트 (UI 동기화용, 실제로는 status API가 더 중요)
    data = load_data_from_file()
    devices = data.get('iot_devices', [])
    for d in devices:
        if d['id'] == dev_id:
            d['status'] = (action == 'on')
            break
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        
    return jsonify({'success': success})

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
        
        # 설정 변경 시 장비 재연결
        init_iot_devices()
        
        return jsonify({'success': True})
    except Exception as e: return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    if not check_password(request.form.get('password')): return jsonify({'success': False}), 403
    f = request.files.get('file')
    if not f or f.filename == '' or not allowed_file(f.filename): return jsonify({'success': False}), 400
    
    fn = secure_filename(f.filename)
    sn = f"{int(time.time())}_{fn}"
    path = os.path.join(app.config['UPLOAD_FOLDER'], sn)
    f.save(path)
    
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
            xml_data = response.read()
            root = ET.fromstring(xml_data)
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
    # 서버 시작 시 IoT 장비 연결 초기화
    init_iot_devices()
    print(f"🚀 TheLynk Server Started on http://{get_ip_address()}:5000")
    app.run(host='0.0.0.0', port=5000, debug=True)