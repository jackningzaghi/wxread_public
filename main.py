# main.py 主逻辑：包括字段拼接、模拟请求
import json
import subprocess
import sys
import time
import random
import logging
import hashlib
import requests
import urllib.parse
from datetime import date, datetime, timezone, timedelta
from push import push
from log_utils import setup_logging
from config import (data, headers, cookies, READ_NUM, PUSH_METHOD, book, chapter,
                    WEEKLY_TARGET, DAILY_MIN, DAILY_MAX)


# 加密盐及其它默认值
KEY = "3c5c8717f3daf09iop3423zafeqoi"
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"
COOKIE_DATA_VARIANTS = [{"rq": "%2Fweb%2Fbook%2Fread", "ql": False},{"rq": "%2Fweb%2Fbook%2Fread", "ql": True},{"rq": "%2Fweb%2Fbook%2Fread"},]


def encode_data(data):
    """数据编码"""
    return '&'.join(f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys()))


def cal_hash(input_string):
    """计算哈希值"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1

    while _19094e > 0:
        _7032f5 = 0x7fffffff & (_7032f5 ^ ord(input_string[_19094e]) << (length - _19094e) % 30)
        _cc1055 = 0x7fffffff & (_cc1055 ^ ord(input_string[_19094e - 1]) << _19094e % 30)
        _19094e -= 2

    return hex(_7032f5 + _cc1055)[2:].lower()

def get_wr_skey():
    """刷新cookie密钥"""
    for cookie_data in COOKIE_DATA_VARIANTS:
        try:
            response = requests.post(RENEW_URL,headers=headers,cookies=cookies,data=json.dumps(cookie_data, separators=(',', ':')),timeout=10)
            
            if 'wr_skey' in response.cookies:
                return response.cookies['wr_skey'][:8]
            else:
                continue
        except requests.RequestException as exc:
            logging.warning(f"refresh_cookie 请求失败，payload={cookie_data}，原因：{exc}")
            continue
        
        
    return None

def fix_no_synckey():
    requests.post(FIX_SYNCKEY_URL, headers=headers, cookies=cookies,data=json.dumps({"bookIds":["3300060341"]}, separators=(',', ':')))

refresh_print = setup_logging()

def refresh_cookie():
    logging.info("刷新 cookie")
    new_skey = get_wr_skey()
    if new_skey:
        cookies['wr_skey'] = new_skey
        logging.info(f"密钥刷新成功，新密钥：{new_skey[:2]}***")
        logging.info("重新本次阅读。")
    else:
        ERROR_CODE = "无法获取新密钥或者 WXREAD_CURL_BASH 配置有误，终止运行。"
        logging.error(ERROR_CODE)
        raise Exception(ERROR_CODE)


# ── 周目标模式辅助函数 ──

def get_today_beijing():
    """返回北京时间（UTC+8）今天的 date 对象"""
    return datetime.now(timezone(timedelta(hours=8))).date()


def get_week_monday(today):
    """返回 today 所在周的周一 date 对象"""
    return today - timedelta(days=today.weekday())


def load_weekly_tracking():
    """读取 weekly_reading.json，不存在或损坏则返回 None"""
    try:
        with open('weekly_reading.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_weekly_tracking(tracking):
    """写入 weekly_reading.json"""
    with open('weekly_reading.json', 'w', encoding='utf-8') as f:
        json.dump(tracking, f, ensure_ascii=False, indent=2)


def calculate_daily_target(tracking, today, weekly_target, min_daily, max_daily):
    """计算今日需要阅读的目标分钟数和对应的 read_num。

    返回 (read_num, target_minutes) 或 (None, None) 表示今日已完成。
    """
    # 今日已完成？
    if today.isoformat() in tracking.get('days', {}):
        return None, None

    cumulative = sum(d['minutes'] for d in tracking.get('days', {}).values())

    # 本周目标已达标 → 降为最低每日打卡量，保持阅读记录不断
    if cumulative >= weekly_target:
        logging.info(f"本周目标 {weekly_target} 分钟已达标（累计 {cumulative} 分钟），"
                     f"今日仅读 {min_daily} 分钟保持打卡。")
        return int(min_daily * 2), min_daily

    week_monday = date.fromisoformat(tracking['week_start_monday'])
    day_index = (today - week_monday).days          # 0=周一 … 6=周日
    remaining_days = 7 - day_index                   # 含今天

    if day_index == 6:
        # 周日：兜底，确保达标
        target = min(weekly_target - cumulative, max_daily)
    else:
        # 周一到周六：随机目标，但检查后面的日子能不能补回来
        random_target = random.randint(min_daily, max_daily)
        days_after_today = remaining_days - 1
        remaining_needed = weekly_target - cumulative - random_target
        if days_after_today > 0 and remaining_needed > days_after_today * max_daily:
            # 后面补不回来，今天多读
            target = min(weekly_target - cumulative, max_daily)
        else:
            target = random_target

    target = max(target, 0)
    read_num = int(target * 2)  # 每次迭代 0.5 分钟
    return read_num, target


# ── Git 持久化辅助函数 ──

def git_persist():
    """将跟踪文件提交并推送到远程仓库"""
    import os as _os
    subprocess.run(['git', 'config', 'user.name', 'github-actions[bot]'], check=False)
    subprocess.run(['git', 'config', 'user.email', 'github-actions[bot]@users.noreply.github.com'], check=False)
    # 只添加实际存在的文件，避免 git add 因文件不存在而报错
    for f in ('last_read.txt', 'weekly_reading.json'):
        if _os.path.exists(f):
            subprocess.run(['git', 'add', f], check=False)
    result = subprocess.run(['git', 'diff', '--staged', '--quiet'])
    if result.returncode != 0:
        subprocess.run(['git', 'commit', '-m', 'Update reading progress [skip ci]'], check=False)
        subprocess.run(['git', 'push'], check=False)

# ── 主流程 ──

try:
    beijing_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(beijing_tz).strftime('%Y-%m-%d')

    # 1. 确定今日阅读目标
    if WEEKLY_TARGET > 0:
        # ── 周目标模式 ──
        tracking = load_weekly_tracking()
        today_date = get_today_beijing()
        week_monday = get_week_monday(today_date)

        if tracking is None or date.fromisoformat(tracking['week_start_monday']) != week_monday:
            tracking = {
                'week_start_monday': week_monday.isoformat(),
                'target_minutes': WEEKLY_TARGET,
                'days': {},
            }
            logging.info(f"新的一周开始，周一日期：{week_monday.isoformat()}，目标：{WEEKLY_TARGET} 分钟")

        read_num, target_minutes = calculate_daily_target(
            tracking, today_date, WEEKLY_TARGET, DAILY_MIN, DAILY_MAX
        )

        if read_num is None:
            logging.info(f"今日（{today_str}）已完成阅读，无需重复执行，退出。")
            sys.exit(0)

        cumulative_before = sum(d['minutes'] for d in tracking.get('days', {}).values())
        logging.info(f"周目标模式：今日目标 {target_minutes} 分钟（{read_num} 次），"
                     f"本周累计 {cumulative_before}/{WEEKLY_TARGET} 分钟")
        skip_reading = False
    else:
        # ── 传统模式：last_read.txt 幂等 ──
        skip_reading = False
        try:
            with open('last_read.txt', 'r') as f:
                if f.read().strip() == today_str:
                    skip_reading = True
        except FileNotFoundError:
            pass

        if skip_reading:
            logging.info(f"今日（{today_str}）已完成阅读，无需重复执行，退出。")
            sys.exit(0)

        read_num = READ_NUM
        logging.info(f"固定模式：一共需要阅读 {read_num} 次。")

    # 2. 执行阅读
    refresh_cookie()
    index = 1
    lastTime = int(time.time()) - 30
    consecutive_failures = 0

    while index <= read_num:
        data.pop('s')
        data['b'] = random.choice(book)
        data['c'] = random.choice(chapter)
        thisTime = int(time.time())
        data['ct'] = thisTime
        data['rt'] = thisTime - lastTime
        data['ts'] = int(thisTime * 1000) + random.randint(0, 1000)
        data['rn'] = random.randint(0, 1000)
        data['sg'] = hashlib.sha256(f"{data['ts']}{data['rn']}{KEY}".encode()).hexdigest()
        data['s'] = cal_hash(encode_data(data))

        refresh_print(f"阅读进度: 第 {index}/{read_num} 次，已完成 {(index - 1) * 0.5:.1f} 分钟")
        logging.debug("data: %s", data)
        response = requests.post(READ_URL, headers=headers, cookies=cookies,
                                 data=json.dumps(data, separators=(',', ':')))
        resData = response.json()
        logging.debug("response: %s", resData)

        if 'succ' in resData:
            consecutive_failures = 0
            if 'synckey' in resData:
                lastTime = thisTime
                index += 1
                time.sleep(30)
                refresh_print(f"阅读进度: 第 {min(index, read_num + 1) - 1}/{read_num} 次，"
                             f"已完成 {(index - 1) * 0.5:.1f} 分钟")
            else:
                logging.warning("无 synckey，尝试修复...")
                fix_no_synckey()
        else:
            consecutive_failures += 1
            if consecutive_failures > 10:
                raise Exception(f"连续 {consecutive_failures} 次阅读请求失败，"
                                f"请检查 book ID 是否正确或 cookie 是否已过期")
            logging.warning(f"cookie 已过期，尝试刷新...（连续失败 {consecutive_failures}/10）")
            refresh_cookie()

    logging.info("阅读脚本已完成。")
    actual_minutes = (index - 1) * 0.5

    # 3. 持久化状态
    if WEEKLY_TARGET > 0:
        tracking['days'][today_date.isoformat()] = {
            'minutes': actual_minutes,
            'read_num': index - 1,
            'completed_at': datetime.now(beijing_tz).isoformat(),
        }
        save_weekly_tracking(tracking)
        cumulative_after = sum(d['minutes'] for d in tracking['days'].values())
        logging.info(f"本周累计：{cumulative_after}/{WEEKLY_TARGET} 分钟")

    # 写入 last_read.txt（两种模式都需要，供候补 cron 幂等）
    with open('last_read.txt', 'w') as f:
        f.write(today_str)
    git_persist()

    # 4. 等到北京时间 7:30 (UTC 23:30) 再推送
    now = time.time()
    today_start = now - (now % 86400)
    target = today_start + 23 * 3600 + 30 * 60
    wait = target - now
    if wait > 0:
        logging.info(f"等待 {wait:.0f} 秒至北京时间 7:30 推送...")
        time.sleep(wait)

    if PUSH_METHOD not in (None, ''):
        logging.info("开始推送...")
        if WEEKLY_TARGET > 0:
            push_title = f"微信读书 {int(cumulative_after)}/{WEEKLY_TARGET}min"
            push_msg = (f"微信读书自动阅读完成。\n"
                        f"今日阅读：{actual_minutes} 分钟\n"
                        f"本周累计：{cumulative_after}/{WEEKLY_TARGET} 分钟")
        else:
            push_title = None
            push_msg = f"微信读书自动阅读完成。\n阅读时长：{actual_minutes} 分钟。"
        push(push_msg, PUSH_METHOD, is_success=True, title=push_title)
    else:
        logging.info("未配置推送渠道，跳过推送。")
except Exception as e:
    error_msg = f"微信读书自动阅读失败。\n错误信息：{e}"
    logging.error(error_msg)
    if PUSH_METHOD not in (None, ''):
        push(error_msg, PUSH_METHOD, is_success=False)
    raise
