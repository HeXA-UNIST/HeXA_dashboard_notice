import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, jsonify
import psutil
import datetime
import os

app = Flask(__name__)

GITHUB_NOTICE_URL = "https://raw.githubusercontent.com/HeXA-UNIST/heXA_dashboard_notice/main/notice.md"

SERVICES = [
    {"name": "Heartbeat", "url": "https://www.google.com"},
    {"name": "Service2", "url": "https://www.google.com"},
    {"name": "Service3", "url": "https://www.google.com"},
    {"name": "Service4", "url": "https://www.google.com"}
]


def get_naver_weather():
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        url = "https://search.naver.com/search.naver?query=울산+언양읍+날씨"
        res = requests.get(url, headers=headers)
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
    except:
        return {"temp": "N/A", "desc": "Error", "hourly": []}


def get_github_notice():
    try:
        res = requests.get(GITHUB_NOTICE_URL, timeout=3)
        return res.text if res.status_code == 200 else "공지사항 파일을 찾을 수 없습니다."
    except:
        return "GitHub 연결에 실패했습니다."

def get_cpu_temp():
    res = os.popen('vcgencmd measure_temp').readline()
    return res.replace("temp=","").replace("'C\n","")

@app.route('/api/data')
def get_data():
    # 서비스 상태 체크
    service_statuses = []
    for svc in SERVICES:
        try:
            status = "Online" if requests.get(svc['url'], timeout=1).status_code == 200 else "Offline"
        except:
            status = "Offline"
        service_statuses.append({"name": svc['name'], "status": status})

    # 지연시간 체크
    try:
        ping = f"{int(requests.get('http://1.1.1.1', timeout=1).elapsed.total_seconds() * 1000)}ms"
    except:
        ping = "Timeout"

    return jsonify({
        "weather": get_naver_weather(),
        "notice": get_github_notice(),
        "services": service_statuses,
        "system": {
            "cpu": psutil.cpu_percent(),
            "cpu_temp":get_cpu_temp(),
            "ram": psutil.virtual_memory().percent,
            "ping": ping,
            "time": datetime.datetime.now().strftime("%H:%M:%S")
        }
    })


@app.route('/')
def index(): return render_template('index.html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)