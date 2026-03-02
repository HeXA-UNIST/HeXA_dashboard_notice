import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, jsonify
import psutil
import datetime
import os
import time
import threading
import atexit
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)
app.json.ensure_ascii = False  # JSON 응답에서 한글이 깨지지 않도록 설정
app.json.sort_keys = False  # JSON 응답에서 키 순서 유지

GITHUB_NOTICE_URL = "https://raw.githubusercontent.com/HeXA-UNIST/heXA_dashboard_notice/main/notice.md"

SERVICES = [
    {"name": "Heartbeat", "url": "https://www.google.com"},
    {"name": "밥먹어U", "url": "https://meal.hexa.pro/mainpage/data", "url_type": "json"},
    {"name": "BUS HeXA", "url": "https://bus.hexa.pro"},
    {"name": "Service4", "url": "https://www.google.com"}
]


# 공용 HTTP 세션: 연결 재사용(keep-alive) + 재시도로 외부 호출 비용/실패율 감소
def create_http_session():
    retry = Retry(
        total=2,
        backoff_factor=0.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


HTTP = create_http_session()

# 서비스 상태 체크용 스레드풀 전역 재사용: 요청마다 executor 생성/해제 비용 제거
SERVICE_POOL_SIZE = min(len(SERVICES), 8) if SERVICES else 1
SERVICE_EXECUTOR = ThreadPoolExecutor(max_workers=SERVICE_POOL_SIZE)


@atexit.register
def shutdown_executors():
    SERVICE_EXECUTOR.shutdown(wait=False)


# 간단한 인메모리 TTL 캐시: 동일 데이터의 반복 외부 호출을 줄임
CACHE = {}
CACHE_LOCK = threading.Lock()

# GitHub notice 전용 메타데이터
# - etag: 마지막으로 정상 수신한 응답의 ETag 값
# - last_text: 마지막으로 정상 수신한 공지 본문
#
# 동작 개요:
# 1) etag가 있으면 다음 요청에 If-None-Match 헤더를 붙여 "변경 여부만" 확인
# 2) 서버가 304(Not Modified)를 주면 last_text를 재사용(본문 재다운로드 없음)
# 3) 서버가 200을 주면 새 본문/ETag로 갱신
#
# 효과:
# - 공지가 자주 바뀌지 않을 때 네트워크 트래픽 및 원격 처리 부담을 크게 줄임
# - 일시 네트워크 장애 시에도 last_text로 서비스 연속성 확보
NOTICE_META = {
    "etag": None,
    "last_text": ""
}
NOTICE_META_LOCK = threading.Lock()


def get_cached(key, ttl_seconds, fetcher):
    now = time.time()

    with CACHE_LOCK:
        cached = CACHE.get(key)
        if cached and now - cached["ts"] < ttl_seconds:
            return cached["value"]

    value = fetcher()

    with CACHE_LOCK:
        CACHE[key] = {"ts": now, "value": value}

    return value


def get_naver_weather():
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        url = "https://search.naver.com/search.naver?query=울산+언양읍+날씨"
        res = HTTP.get(url, headers=headers, timeout=3)
        soup = BeautifulSoup(res.text, 'html.parser')

        # 온도 및 상태
        temp = soup.select_one('.temperature_text strong').text.replace('현재 온도', '').replace('°', '').strip()
        desc = soup.select_one('.before_slash').text.strip()

        # 상세 지표 (풍속 추출 강화)
        summary_items = soup.select('.summary_list .sort')
        weather_details = {}
        wind_value = "-"
        for item in summary_items:
            term = item.select_one('.term').text.strip()
            val = item.select_one('.desc').text.strip()
            if '풍' in term:
                wind_value = val
            else:
                weather_details[term] = val

        # 오늘 차트 (미세먼지, 자외선)
        chart_items = soup.select('.today_chart_list .item_today')
        metrics = {item.select_one('.title').text.strip(): item.select_one('.txt').text.strip() for item in chart_items
                   if item.select_one('.title')}

        # 시간별 예보
        hourly_data = []
        temp_items = soup.select('._hourly_weather ._li')
        rain_items = soup.select('._hourly_rain .value')
        for i in range(min(len(temp_items), 8)):
            h_time = temp_items[i].select_one('.time em').text.strip()
            h_temp = temp_items[i].select_one('.num').text.replace('°', '').strip()
            h_rain = rain_items[i].text.strip() if i < len(rain_items) else "0%"
            hourly_data.append({"time": h_time, "temp": h_temp, "rain": h_rain})

        return {
            "temp": temp, "desc": desc, "feels": weather_details.get("체감", "--"),
            "hum": weather_details.get("습도", "--"), "wind": wind_value,
            "uv": metrics.get("자외선", "-"), "dust": metrics.get("미세먼지", "-"),
            "hourly": hourly_data
        }
    except Exception:
        return {"temp": "N/A", "desc": "Error", "hourly": []}


def get_github_notice():
    try:
        # 공유 메타데이터는 잠금 하에 읽어 스레드 간 일관성 보장
        with NOTICE_META_LOCK:
            etag = NOTICE_META["etag"]
            cached_text = NOTICE_META["last_text"]

        # 기존 ETag가 있으면 조건부 요청(If-None-Match)으로 변경 여부만 확인
        headers = {}
        if etag:
            headers["If-None-Match"] = etag

        res = HTTP.get(GITHUB_NOTICE_URL, timeout=3, headers=headers)

        # 본문 변경이 없으면 304가 내려오므로, 기존 캐시 텍스트를 즉시 반환
        # (네트워크 왕복은 있지만 본문 다운로드/파싱 비용은 없음)
        if res.status_code == 304 and cached_text:
            return cached_text

        # 본문이 변경되었거나 최초 요청이면 200 수신
        # 이때 ETag/본문을 같이 갱신해 다음 요청부터 조건부 재검증 가능
        if res.status_code == 200:
            with NOTICE_META_LOCK:
                NOTICE_META["etag"] = res.headers.get("ETag")
                NOTICE_META["last_text"] = res.text
            return res.text

        # 비정상 상태코드(예: 404/5xx)에서는 기존 성공 캐시를 우선 반환해 가용성 유지
        return cached_text if cached_text else "공지사항 파일을 찾을 수 없습니다."
    except Exception:
        # 예외 상황(타임아웃/네트워크 오류)에서도 마지막 성공 값을 반환하여 안정성 확보
        with NOTICE_META_LOCK:
            cached_text = NOTICE_META["last_text"]
        return cached_text if cached_text else "GitHub 연결에 실패했습니다."

def get_cpu_temp():
    res = os.popen('vcgencmd measure_temp').readline()
    return res.replace("temp=","").replace("'C\n","")


def is_non_empty_json_payload(payload):
    # JSON 내용물이 "비어 있는지" 판별
    # - dict/list: 길이가 1 이상이어야 유효
    # - str: 공백 제거 후 길이가 1 이상이어야 유효
    # - number/bool: null(None)만 아니면 내용이 있는 것으로 간주
    if isinstance(payload, (dict, list)):
        return len(payload) > 0
    if isinstance(payload, str):
        return len(payload.strip()) > 0
    return payload is not None


def check_service_status(service):
    try:
        url_type = service.get("url_type", "basic")

        # 1) url_type과 무관하게 먼저 HTTP 응답 성공 여부를 확인
        res = HTTP.get(service['url'], timeout=1)
        if res.status_code != 200:
            return {"name": service['name'], "status": "Offline"}

        # 2) 200이 확인된 뒤, url_type별 추가 검증 수행
        # json 전용 healthcheck:
        # 1) Content-Type에 application/json 포함
        # 2) JSON 파싱 성공
        # 3) 파싱 결과가 비어 있지 않음
        if url_type == "json":
            content_type = res.headers.get("Content-Type", "").lower()
            if "application/json" not in content_type:
                return {"name": service['name'], "status": "Offline"}

            payload = res.json()
            if not is_non_empty_json_payload(payload):
                return {"name": service['name'], "status": "Offline"}

        status = "Online"
    except Exception:
        status = "Offline"
    return {"name": service['name'], "status": status}


def get_service_statuses():
    # 병렬 체크: 전역 executor를 재사용해 생성/해제 오버헤드 제거
    return list(SERVICE_EXECUTOR.map(check_service_status, SERVICES))


def get_system_metrics():
    try:
        ping = f"{int(HTTP.get('http://1.1.1.1', timeout=1).elapsed.total_seconds() * 1000)}ms"
    except Exception:
        ping = "Timeout"

    return {
        "cpu": psutil.cpu_percent(),
        "cpu_temp": get_cpu_temp(),
        "ram": psutil.virtual_memory().percent,
        "ping": ping,
        "time": datetime.datetime.now().strftime("%H:%M:%S")
    }


@app.route('/api/weather')
def get_weather_api():
    # 외부 스크래핑 부하 절감을 위해 5분 캐시
    return jsonify(get_cached("weather", 300, get_naver_weather))


@app.route('/api/notice')
def get_notice_api():
    # 공지사항은 변경 주기가 길어 1시간 캐시
    return jsonify({"notice": get_cached("notice", 3600, get_github_notice)})


@app.route('/api/services')
def get_services_api():
    # 서비스 상태는 비교적 자주 변할 수 있어 30초 캐시
    return jsonify({"services": get_cached("services", 30, get_service_statuses)})


@app.route('/api/system')
def get_system_api():
    # 시스템 지표는 로컬 계산 위주라 짧게 5초 캐시
    return jsonify({"system": get_cached("system", 5, get_system_metrics)})

@app.route('/api/data')
def get_data():
    # 기존 클라이언트 호환을 위해 통합 API는 유지
    return jsonify({
        "weather": get_cached("weather", 300, get_naver_weather),
        "notice": get_cached("notice", 3600, get_github_notice),
        "services": get_cached("services", 30, get_service_statuses),
        "system": get_cached("system", 5, get_system_metrics)
    })


@app.route('/')
def index(): return render_template('index.html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)