import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, jsonify
import psutil
import datetime
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

GITHUB_NOTICE_URL = "https://raw.githubusercontent.com/HeXA-UNIST/heXA_dashboard_notice/main/notice.md"

SERVICES = [
    {"name": "Heartbeat", "url": "https://www.google.com"},
    {"name": "Service2", "url": "https://www.google.com"},
    {"name": "Service3", "url": "https://www.google.com"},
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


# 간단한 인메모리 TTL 캐시: 동일 데이터의 반복 외부 호출을 줄임
CACHE = {}
CACHE_LOCK = threading.Lock()


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
        res = HTTP.get(GITHUB_NOTICE_URL, timeout=3)
        return res.text if res.status_code == 200 else "공지사항 파일을 찾을 수 없습니다."
    except Exception:
        return "GitHub 연결에 실패했습니다."

def get_cpu_temp():
    res = os.popen('vcgencmd measure_temp').readline()
    return res.replace("temp=","").replace("'C\n","")


def check_service_status(service):
    try:
        status = "Online" if HTTP.get(service['url'], timeout=1).status_code == 200 else "Offline"
    except Exception:
        status = "Offline"
    return {"name": service['name'], "status": status}


def get_service_statuses():
    # 병렬 체크: 서비스 개수가 늘어도 응답 지연을 최소화
    max_workers = min(len(SERVICES), 8) if SERVICES else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(check_service_status, SERVICES))


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