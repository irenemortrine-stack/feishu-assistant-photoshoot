import os
import json
import time
import threading
import traceback
import requests
from datetime import date
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from notion_client import Client
import anthropic

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(dotenv_path=env_path)

_debug_notion_token = os.getenv('NOTION_TOKEN')
print(f"--- DEBUG INFO ---")
print(f"Looking for .env at: {env_path}")
print(f"NOTION_TOKEN found: {'Yes' if _debug_notion_token else 'No'}")
if _debug_notion_token:
    print(f"NOTION_TOKEN starts with: {_debug_notion_token[:12]}")
print(f"--- END DEBUG ---")

app = Flask(__name__)
processed_msg_ids = set()
pending_departure = {}  # chat_id -> (theme, msg_id)，等待用户发位置
pending_xhs = {}       # chat_id -> (page_id, location)，等待用户发小红书链接
pending_refresh = {}   # chat_id -> (theme, origin_coord, weather_info, origin_name)

APP_ID = os.getenv('FEISHU_APP_ID', '')
APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')
NOTION_TOKEN = os.environ.get('NOTION_TOKEN', '').strip()
DATABASE_ID = os.environ.get('NOTION_DATABASE_ID', '31de210ae52080d6a02aeecfe7df5a4a').strip()
PREF_DATABASE_ID = '31de210ae52080628e72e69c703b52b8'
AMAP_KEY = 'aeef7bcd2426d11af7c37ef7f7000c59'
assert DATABASE_ID, 'DATABASE_ID 未配置，请检查 .env'

notion = Client(auth=NOTION_TOKEN)
from openai import OpenAI
ai = OpenAI(api_key=os.getenv('DEEPSEEK_API_KEY', ''), base_url='https://api.deepseek.com')


# ─── 飞书基础工具 ────────────────────────────────────────────

def get_tenant_access_token():
    url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
    resp = requests.post(url, json={'app_id': APP_ID, 'app_secret': APP_SECRET})
    return resp.json().get('tenant_access_token', '')


def send_reply(msg_id, text):
    token = get_tenant_access_token()
    url = f'https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/reply'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {'msg_type': 'text', 'content': json.dumps({'text': text})}
    resp = requests.post(url, headers=headers, json=payload)
    print(f'[reply response] {resp.json()}')


def send_card(chat_id, card_content):
    """发送卡片消息到会话"""
    token = get_tenant_access_token()
    url = 'https://open.feishu.cn/open-apis/im/v1/messages'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {
        'receive_id': chat_id,
        'msg_type': 'interactive',
        'content': json.dumps(card_content)
    }
    params = {'receive_id_type': 'chat_id'}
    resp = requests.post(url, headers=headers, json=payload, params=params)
    print(f'[send_card response] {resp.json()}')
    return resp.json().get('data', {}).get('message_id', '')


def update_card(message_id, card_content):
    """更新已有卡片"""
    token = get_tenant_access_token()
    url = f'https://open.feishu.cn/open-apis/im/v1/messages/{message_id}'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload = {'msg_type': 'interactive', 'content': json.dumps(card_content)}
    resp = requests.patch(url, headers=headers, json=payload)
    print(f'[update_card response] {resp.json()}')


# ─── 归档逻辑 ────────────────────────────────────────────────

def process_with_ai(user_text):
    """提取地点并生成胶片感摄影日记，返回 (location, location_type, diary, tip)"""
    prompt = f"""用户发来一段摄影记录："{user_text}"

请完成以下四项任务，以 JSON 格式返回，不要有任何多余文字：
1. "location": 从文字中提取最核心的拍摄地点关键词（2-6个字，如"武康路"）
2. "location_type": 判断拍摄地点是室内还是室外，只能返回"室内"或"室外"
3. "diary": 基于用户原话，扩写润色成一段约50字、充满胶片感的摄影日记（第一人称，有画面感）
4. "tip": 一条关于今日光影的简短摄影小贴士（20字以内）

返回格式示例：
{{"location": "武康路", "location_type": "室外", "diary": "日记内容...", "tip": "小贴士内容"}}"""

    try:
        message = ai.chat.completions.create(
            model='deepseek-chat',
            max_tokens=512,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = message.choices[0].message.content
        print(f'[AI raw response] {raw}')
        raw = raw.replace('```json', '').replace('```', '').strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as je:
            print(f'[AI JSONDecodeError] {je} | raw: {repr(raw)}')
            return '未知地点', '室外', user_text, '光线是摄影师最好的朋友。'
        return result['location'], result.get('location_type', '室外'), result['diary'], result['tip']
    except Exception as e:
        print(f'[AI error] {type(e).__name__}: {e}')
        traceback.print_exc()
        return '未知地点', '室外', user_text, '光线是摄影师最好的朋友。'


def write_to_notion(location, location_type, diary, user_text):
    print(f"DEBUG: NOTION_TOKEN starts with: {os.environ.get('NOTION_TOKEN', '')[:15]}")
    print(f"DEBUG: DATABASE_ID = {DATABASE_ID}")
    page = notion.pages.create(
        parent={'database_id': DATABASE_ID},
        properties={
            '地点': {'title': [{'text': {'content': location}}]},
            '地点类型': {'select': {'name': location_type}},
            'Capture Date': {'date': {'start': date.today().isoformat()}},
            '描述': {'rich_text': [{'text': {'content': diary}}]},
        }
    )
    return page['id']

# ─── 出发逻辑 ────────────────────────────────────────────────

def build_location_select_card(destination):
    """构建选点卡片——让用户输入起点地址"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📸 准备出发：{destination}"},
            "template": "blue"
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"目的地：**{destination}**\n\n请直接回复你的**起点地址**（如：徐家汇、人民广场），我将为你规划交通路线并生成拍摄建议。"}
            }
        ]
    }


def build_result_card(destination, transport, guide, xhs_url):
    """构建结果卡片"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📸 出发指南：{destination}"},
            "template": "green"
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"🏠 **实时交通情报**\n{transport}"}
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"📷 **独家拍摄指南**\n{guide}"}
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✨ 小红书灵感参考"},
                        "type": "primary",
                        "url": xhs_url
                    }
                ]
            }
        ]
    }


def amap_geocode(address):
    """高德地理编码，返回 '经度,纬度' 字符串"""
    url = 'https://restapi.amap.com/v3/geocode/geo'
    resp = requests.get(url, params={'key': AMAP_KEY, 'address': address})
    data = resp.json()
    if data.get('status') == '1' and data.get('geocodes'):
        return data['geocodes'][0]['location']  # 格式: "经度,纬度"
    return None


def amap_transit(origin, destination_coord):
    """高德公交路径规划，返回 (耗时描述, 线路描述)"""
    url = 'https://restapi.amap.com/v3/direction/transit/integrated'
    resp = requests.get(url, params={
        'key': AMAP_KEY,
        'origin': origin,
        'destination': destination_coord,
        'city': '上海',
        'cityd': '上海'
    })
    data = resp.json()
    if data.get('status') == '1' and data.get('route', {}).get('transits'):
        transit = data['route']['transits'][0]
        duration = int(transit.get('duration', 0)) // 60
        # 提取线路名
        segments = transit.get('segments', [])
        lines = []
        for seg in segments:
            bus = seg.get('bus', {})
            for busline in bus.get('buslines', []):
                lines.append(busline.get('name', ''))
        walking_distance = data['route'].get('taxi_cost', '')
        line_str = ' → '.join([l for l in lines if l]) or '步行'
        return f"预计 {duration} 分钟", line_str
    return "暂无数据", "建议打车或步行"


def query_notion_prefs(destination):
    """检索偏好库，返回拍摄建议文本"""
    try:
        results = notion.databases.query(
            database_id=PREF_DATABASE_ID,
            filter={
                "or": [
                    {"property": "地点", "title": {"contains": destination}},
                    {"property": "场景", "rich_text": {"contains": destination}}
                ]
            }
        )
        pages = results.get('results', [])
        if not pages:
            return None
        page = pages[0]['properties']
        advice = ''
        for key in ['拍摄建议', '焦段', '器材']:
            prop = page.get(key, {})
            ptype = prop.get('type', '')
            if ptype == 'rich_text':
                val = ''.join([t['plain_text'] for t in prop.get('rich_text', [])])
            elif ptype == 'title':
                val = ''.join([t['plain_text'] for t in prop.get('title', [])])
            else:
                val = ''
            if val:
                advice += f"【{key}】{val}\n"
        return advice.strip() or None
    except Exception as e:
        print(f'[notion pref error] {e}')
        return None


def generate_shooting_guide_by_theme(theme, notion_prefs, origin_name='', weather_info='', nearby_pois=None):
    """根据拍摄主题和起点生成3个附近地点建议"""
    from datetime import datetime
    now = datetime.now()
    hour = now.hour
    if 5 <= hour < 8:
        light_tip = "现在是清晨，光线柔和，是拍摄黄金时段。"
    elif 8 <= hour < 10:
        light_tip = "现在是上午，光线角度低，适合拍摄长影子和侧光。"
    elif 10 <= hour < 16:
        light_tip = "现在是正午前后，光线较硬，建议寻找阴影区域或室内拍摄。"
    elif 16 <= hour < 19:
        light_tip = "现在是下午黄金时段，暖调侧光，是户外拍摄最佳时机。"
    elif 19 <= hour < 21:
        light_tip = "现在是蓝调时刻，天空呈深蓝色，适合城市夜景和人像。"
    else:
        light_tip = "现在是夜间，适合长曝光、光绘或霓虹灯街拍。"

    weather_context = f"当前天气：{weather_info}，请根据天气条件调整推荐地点（如雨天优先推荐有顶棚/室内场景，晴天优先户外）。" if weather_info else ""
    pref_context = f"偏好库参考：\n{notion_prefs}" if notion_prefs else ""
    location_context = f"用户当前位置：{origin_name}。" if origin_name else ""

    if nearby_pois:
        poi_list = '\n'.join([f"- {p['name']}（{p['address']}）" for p in nearby_pois[:20]])
        poi_context = f"以下是用户20km内的真实地点列表，请从中挑选3个最适合「{theme}」主题拍摄的地点，优先选择有故事感、光影独特、不过度商业化的地方：\n{poi_list}"
    else:
        poi_context = f"请推荐3个适合「{theme}」主题的真实拍摄地点，不要过度商业化。"

    prompt = f"""你是一位资深摄影导师。用户想进行「{theme}」主题拍摄。
当前时间：{now.strftime('%H:%M')}，{light_tip}
{weather_context}
{location_context}
{pref_context}

{poi_context}

以 JSON 数组返回，不要有任何多余文字：
[
  {{
    "place": "地点名称",
    "address": "详细地址",
    "lng": "经度",
    "lat": "纬度",
    "best_time": "最佳拍摄时段，结合当前时间说明是否适合现在前往（10字以内）",
    "suggestion": "针对该地点的拍摄建议（50字以内）",
    "guide": "具体拍摄指南，包含光线、构图、器材建议（80字以内）"
  }}
]"""

    try:
        message = ai.chat.completions.create(
            model='deepseek-chat',
            max_tokens=600,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = message.choices[0].message.content.strip()
        raw = raw.replace('```json', '').replace('```', '').strip()
        return json.loads(raw)
    except Exception as e:
        print(f'[AI guide error] {e}')
        return [{"place": theme, "address": theme, "suggestion": "注意光线变化，寻找有趣构图。", "guide": "祝拍摄顺利！"}]


def fetch_nearby_pois(origin_coord, theme, radius=20000):
    """用高德周边搜索获取真实 POI 列表"""
    # 根据主题映射搜索关键词
    keyword_map = {
        '人像': '公园|老街|咖啡馆|艺术区|花园',
        '建筑': '历史建筑|老街|文化园|艺术馆|工业遗址',
        '街拍': '老街|步行街|市集|文创园|胡同',
        '风光': '公园|湖泊|山|植物园|湿地',
        '夜景': '天桥|观景台|商业街|滨水',
    }
    keywords = '公园|老街|艺术区|文创园|历史建筑'
    for k, v in keyword_map.items():
        if k in theme:
            keywords = v
            break

    pois = []
    try:
        resp = requests.get('https://restapi.amap.com/v3/place/around', params={
            'key': AMAP_KEY,
            'location': origin_coord,
            'keywords': keywords,
            'radius': radius,
            'offset': 20,
            'page': 1,
            'extensions': 'base'
        })
        data = resp.json()
        for poi in data.get('pois', []):
            pois.append({
                'name': poi.get('name', ''),
                'address': poi.get('address', ''),
                'location': poi.get('location', ''),
                'type': poi.get('type', '')
            })
        print(f'[POI around] 获取到 {len(pois)} 个地点')
    except Exception as e:
        print(f'[POI around error] {e}')
    return pois


def verify_places_with_amap(places, city='上海'):
    """用高德 POI 搜索校验地点真实性，返回校验通过的地点列表（同时更新坐标）"""
    verified = []
    for p in places:
        place = p.get('place', '')
        address = p.get('address', place)
        try:
            resp = requests.get('https://restapi.amap.com/v3/place/text', params={
                'key': AMAP_KEY,
                'keywords': place,
                'city': city,
                'citylimit': 'false',
                'offset': 1
            })
            data = resp.json()
            pois = data.get('pois', [])
            if pois:
                poi = pois[0]
                loc = poi.get('location', '')
                if loc:
                    lng, lat = loc.split(',')
                    p['lng'] = lng
                    p['lat'] = lat
                p['address'] = poi.get('address', address) or address
                verified.append(p)
                print(f'[POI verify] ✅ {place} -> {poi.get("address", "")}')
            else:
                print(f'[POI verify] ❌ {place} 未找到，跳过')
        except Exception as e:
            print(f'[POI verify error] {place}: {e}')
            verified.append(p)  # 校验失败时保留原数据
    return verified


def handle_departure(theme, chat_id, msg_id):
    """处理出发指令，提示用户发送位置"""
    pending_departure[chat_id] = (theme, msg_id)
    send_reply(msg_id, f'📸 收到！主题：{theme}\n\n请在飞书里发送你的📍当前位置，我将基于你的位置推荐附近拍摄地点。')


def process_departure(theme, origin_coord, msg_id):
    """根据拍摄主题和起点坐标，推荐附近地点"""
    import urllib.parse

    # 高德逆地理编码
    origin_name = '你的位置'
    adcode = '310000'
    city_name = '上海'
    try:
        resp = requests.get('https://restapi.amap.com/v3/geocode/regeo', params={
            'key': AMAP_KEY, 'location': origin_coord, 'radius': 1000, 'extensions': 'base'
        })
        regeo = resp.json()
        if regeo.get('status') == '1':
            origin_name = regeo['regeocode']['formatted_address']
            adcode = regeo['regeocode']['addressComponent'].get('adcode', '310000')
            city_name = regeo['regeocode']['addressComponent'].get('city', '') or regeo['regeocode']['addressComponent'].get('province', '上海')
            print(f'[regeo] {origin_name}, adcode={adcode}, city={city_name}')
    except Exception as e:
        print(f'[regeo error] {e}')

    import random
    notion_prefs = query_notion_prefs(theme)
    nearby_pois = fetch_nearby_pois(origin_coord, theme)
    random.shuffle(nearby_pois)

    # 高德天气
    weather_info = ''
    try:
        w_resp = requests.get('https://restapi.amap.com/v3/weather/weatherInfo', params={
            'key': AMAP_KEY, 'city': adcode, 'extensions': 'base'
        })
        w_data = w_resp.json()
        if w_data.get('status') == '1' and w_data.get('lives'):
            live = w_data['lives'][0]
            weather_info = f"{live.get('weather', '')}，{live.get('temperature', '')}°C，{live.get('windpower', '')}级风"
            print(f'[weather] {weather_info}')
    except Exception as e:
        print(f'[weather error] {e}')

    places = generate_shooting_guide_by_theme(theme, notion_prefs, origin_name, weather_info, nearby_pois)
    places = verify_places_with_amap(places, city=city_name)

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"📍 出发地：{origin_name}\n🌤 当前天气：{weather_info if weather_info else '获取失败'}\n📸 为你推荐附近 **{theme}** 小众拍摄地点"}}
    ]

    icons = ['①', '②', '③']
    for i, p in enumerate(places[:3]):
        place = p.get('place', '')
        address = p.get('address', place)
        suggestion = p.get('suggestion', '')
        guide = p.get('guide', '')
        dest_lng = p.get('lng', '')
        dest_lat = p.get('lat', '')
        if dest_lng and dest_lat:
            amap_url = f"https://uri.amap.com/navigation?from={origin_coord}&to={dest_lng},{dest_lat}&toname={urllib.parse.quote(place)}&src=kiro&coordinate=gaode&callnative=1"
        else:
            amap_url = f"https://uri.amap.com/search?keyword={urllib.parse.quote(address)}&src=kiro"
        xhs_place_url = f"xhsdiscover://search/result?keyword={urllib.parse.quote(place + ' 拍照')}"

        elements.append({"tag": "hr"})
        best_time = p.get('best_time', '')
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"{icons[i]} **{place}**\n📍 {address}\n⏰ {best_time}\n💡 {suggestion}\n📷 {guide}"}
        })
        elements.append({
            "tag": "action",
            "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "🗺 高德地图"}, "type": "default", "url": amap_url},
                {"tag": "button", "text": {"tag": "plain_text", "content": "✨ 小红书"}, "type": "primary", "url": xhs_place_url}
            ]
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": "发送「换一换」获取新的推荐地点"}
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"📸 出发指南：{theme}"}, "template": "green"},
        "elements": elements
    }

    token = get_tenant_access_token()
    url = f'https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/reply'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    resp = requests.post(url, headers=headers, json={'msg_type': 'interactive', 'content': json.dumps(card)})
    print(f'[departure card] {resp.json()}')
    # 存储换一换上下文
    chat_id_from_msg = resp.json().get('data', {}).get('chat_id', '')
    if chat_id_from_msg:
        pending_refresh[chat_id_from_msg] = (theme, origin_coord, weather_info, origin_name)


def handle_card_action(action_data):
    """处理卡片回调"""
    action = action_data.get('action', {})
    raw_value = action.get('value', {})
    if isinstance(raw_value, str):
        try:
            value = json.loads(raw_value)
        except Exception:
            value = {}
    else:
        value = raw_value
    message_id = action_data.get('open_message_id', '')
    chat_id = action_data.get('open_chat_id', '')

    # 换一换
    if value.get('action') == 'refresh':
        theme = value.get('theme', '')
        origin_coord = value.get('origin_coord', '')
        weather_info = value.get('weather_info', '')
        origin_name = value.get('origin_name', '')
        print(f'[换一换] theme={theme}, origin={origin_coord}')

        import urllib.parse
        import random
        notion_prefs = query_notion_prefs(theme)
        nearby_pois = fetch_nearby_pois(origin_coord, theme)
        random.shuffle(nearby_pois)
        places = generate_shooting_guide_by_theme(theme, notion_prefs, origin_name, weather_info, nearby_pois)
        places = verify_places_with_amap(places)

        icons = ['①', '②', '③']
        elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"📍 出发地：{origin_name}\n🌤 当前天气：{weather_info if weather_info else '获取失败'}\n📸 为你推荐附近 **{theme}** 小众拍摄地点"}}
        ]
        for i, p in enumerate(places[:3]):
            place = p.get('place', '')
            address = p.get('address', place)
            suggestion = p.get('suggestion', '')
            guide = p.get('guide', '')
            best_time = p.get('best_time', '')
            dest_lng = p.get('lng', '')
            dest_lat = p.get('lat', '')
            if dest_lng and dest_lat:
                amap_url = f"https://uri.amap.com/navigation?from={origin_coord}&to={dest_lng},{dest_lat}&toname={urllib.parse.quote(place)}&src=kiro&coordinate=gaode&callnative=1"
            else:
                amap_url = f"https://uri.amap.com/search?keyword={urllib.parse.quote(address)}&src=kiro"
            xhs_place_url = f"xhsdiscover://search/result?keyword={urllib.parse.quote(place + ' 拍照')}"
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"{icons[i]} **{place}**\n📍 {address}\n⏰ {best_time}\n💡 {suggestion}\n📷 {guide}"}})
            elements.append({"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "🗺 高德地图"}, "type": "default", "url": amap_url},
                {"tag": "button", "text": {"tag": "plain_text", "content": "✨ 小红书"}, "type": "primary", "url": xhs_place_url}
            ]})

        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "🔄 换一换"}, "type": "default",
             "value": json.dumps({"action": "refresh", "theme": theme, "origin_coord": origin_coord, "weather_info": weather_info, "origin_name": origin_name}, ensure_ascii=False)}
        ]})

        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": f"📸 出发指南：{theme}"}, "template": "green"},
            "elements": elements
        }
        if chat_id:
            send_card(chat_id, card)
        return

    # 提取用户选择的坐标（飞书格式：{longitude, latitude}）
    location = action.get('option', '') or action.get('location', {})
    if isinstance(location, dict):
        lng = location.get('longitude', '')
        lat = location.get('latitude', '')
    else:
        print(f'[card action] 未获取到坐标，location={location}')
        return

    if not lng or not lat:
        print('[card action] 坐标为空')
        return

    origin_coord = f"{lng},{lat}"
    print(f'[出发] destination={destination}, origin={origin_coord}')

    # 高德地理编码目的地
    dest_coord = amap_geocode(destination)
    if dest_coord:
        duration_str, line_str = amap_transit(origin_coord, dest_coord)
    else:
        duration_str, line_str = "暂无数据", "建议打车或步行"

    # Notion 偏好检索
    notion_prefs = query_notion_prefs(destination)

    # AI 生成拍摄指南
    guide = generate_shooting_guide(destination, duration_str, line_str, notion_prefs)

    # 交通描述
    transport = f"预计 {duration_str}，建议乘坐 {line_str}"

    # 小红书链接
    import urllib.parse
    xhs_url = f"https://www.xiaohongshu.com/search_result?keyword={urllib.parse.quote(destination + '摄影攻略')}"

    # 更新卡片
    result_card = build_result_card(destination, transport, guide, xhs_url)
    if message_id:
        update_card(message_id, result_card)


# ─── Flask 路由 ──────────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    print(f'[webhook] received, type={data.get("type") if data else None}')

    # 飞书 URL 验证
    if data and 'challenge' in data:
        return jsonify({'challenge': data['challenge']})

    # 卡片回调
    if data and data.get('type') == 'card.action.trigger':
        threading.Thread(target=handle_card_action, args=(data,), daemon=True).start()
        return jsonify({'code': 0})

    # 消息事件
    def safe_process(d):
        try:
            process_event(d)
        except Exception as e:
            print(f'[process_event error] {e}')
            traceback.print_exc()

    threading.Thread(target=safe_process, args=(data,), daemon=True).start()
    return jsonify({'status': 'ok'})


def process_event(data):
    print(f'[raw] {data}')

    event_type = data.get('type') or data.get('header', {}).get('event_type', '')
    event = data.get('event', {})
    print(f'[event_type] {event_type}')

    if 'im.message.receive_v1' in event_type or data.get('type') == 'event_callback':
        msg = event.get('message', {})
        msg_type = msg.get('message_type', '')
        msg_id = msg.get('message_id', '')
        chat_id = msg.get('chat_id', '')
        print(f'[msg] type={msg_type}, id={msg_id}, chat_id={chat_id}')

        # 去重
        if msg_id in processed_msg_ids:
            print(f'[skip] 重复消息: {msg_id}')
            return
        processed_msg_ids.add(msg_id)

        # 过滤5分钟前的旧消息
        create_time = int(msg.get('create_time', 0)) // 1000
        now = time.time()
        age = int(now - create_time) if create_time else -1
        print(f'[time] create_time={create_time}, now={int(now)}, age={age}s')
        if create_time and age > 300:
            print(f'[skip] 消息过旧: {age}s')
            return

        if msg_type == 'image':
            print('[skip] 收到图片，暂不处理')
            return

        # 处理位置消息（用户发送位置后触发出发流程）
        if msg_type == 'location':
            if chat_id in pending_departure:
                theme, orig_msg_id = pending_departure.pop(chat_id)
                raw_content = msg.get('content', '{}')
                loc = json.loads(raw_content)
                lng = loc.get('longitude', '')
                lat = loc.get('latitude', '')
                print(f'[location] theme={theme}, lng={lng}, lat={lat}')
                if lng and lat:
                    threading.Thread(
                        target=process_departure,
                        args=(theme, f"{lng},{lat}", msg_id),
                        daemon=True
                    ).start()
                else:
                    send_reply(msg_id, '位置解析失败，请重新发送位置。')
            return

        if msg_type in ('text', 'post'):
            raw_content = msg.get('content', '{}')
            content = json.loads(raw_content)
            user_text = content.get('text', '').strip()

            if not user_text:
                return

            print(f'收到消息: {user_text}')

            # 等待小红书链接
            if chat_id in pending_xhs:
                if 'xiaohongshu.com' in user_text or 'xhslink.com' in user_text:
                    page_id, location = pending_xhs.pop(chat_id)
                    try:
                        notion.pages.update(page_id=page_id, properties={
                            '小红书链接': {'url': user_text.strip()}
                        })
                        notion_db_url = 'https://www.notion.so/31de210ae52080d6a02aeecfe7df5a4a?v=31de210ae520805e9d52000c6dcef84b'
                        card = {
                            "config": {"wide_screen_mode": True},
                            "header": {"title": {"tag": "plain_text", "content": "✅ 链接已记录"}, "template": "green"},
                            "elements": [
                                {"tag": "div", "text": {"tag": "lark_md", "content": "小红书链接已记录到**拍摄足迹与归档**"}},
                                {"tag": "action", "actions": [
                                    {"tag": "button", "text": {"tag": "plain_text", "content": "📖 查看归档"}, "type": "primary", "url": notion_db_url}
                                ]}
                            ]
                        }
                        token = get_tenant_access_token()
                        url = f'https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/reply'
                        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
                        requests.post(url, headers=headers, json={'msg_type': 'interactive', 'content': json.dumps(card)})
                    except Exception as e:
                        print(f'[xhs link error] {e}')
                        send_reply(msg_id, f'链接记录失败：{str(e)}')
                else:
                    send_reply(msg_id, '请发送小红书帖子链接（包含 xiaohongshu.com 或 xhslink.com）')
                return

            # 归档指令
            if user_text.startswith('归档'):
                try:
                    location, location_type, diary, tip = process_with_ai(user_text)
                    print(f'[AI] location={location}, type={location_type}')
                    page_id = write_to_notion(location, location_type, diary, user_text)
                    print('[notion] 写入成功')
                    pending_xhs[chat_id] = (page_id, location)
                    card = {
                        "config": {"wide_screen_mode": True},
                        "header": {"title": {"tag": "plain_text", "content": "✅ 归档成功"}, "template": "green"},
                        "elements": [
                            {"tag": "div", "text": {"tag": "lark_md", "content": f"📍 地点：{location}\n📝 {diary}"}},
                            {"tag": "hr"},
                            {"tag": "div", "text": {"tag": "lark_md", "content": "发布小红书后，直接把帖子链接发给我，自动记录到归档。"}},
                        ]
                    }
                    token = get_tenant_access_token()
                    url = f'https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/reply'
                    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
                    requests.post(url, headers=headers, json={'msg_type': 'interactive', 'content': json.dumps(card)})
                except Exception as e:
                    print(f'[error] {e}')
                    if msg_id:
                        send_reply(msg_id, f'记录失败，请稍后重试。错误：{str(e)}')

            # 出发指令
            elif user_text.startswith('出发'):
                theme = user_text[2:].lstrip('，,').strip()
                if theme:
                    handle_departure(theme, chat_id, msg_id)
                else:
                    send_reply(msg_id, '请告诉我拍摄主题，例如：出发，我要拍人像')

            # 换一换
            elif user_text.strip() == '换一换':
                if chat_id in pending_refresh:
                    theme, origin_coord, weather_info, origin_name = pending_refresh[chat_id]
                    threading.Thread(
                        target=process_departure,
                        args=(theme, origin_coord, msg_id),
                        daemon=True
                    ).start()
                else:
                    send_reply(msg_id, '请先发送「出发」指令获取推荐，再使用换一换。')

            else:
                print('[skip] 非指令消息，忽略')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
