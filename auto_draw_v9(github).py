# -*- coding: utf-8 -*-
"""
飞机大厨官网转盘自动抽奖脚本 v9
（无头后台运行版 + Telegram 通知 + 记录唯一领取链接）

跟之前版本最大的不同：
- 不再弹出任何可见浏览器窗口（headless=True），可以放后台/开机自启跑
- 不再靠"留着浏览器标签页"来保存中奖结果，而是直接把"接受奖励"按钮的
  完整链接（带 wheel-of-fortune-reward / wheel-of-fortune-event 唯一标识）
  提取出来，追加写入本地文件 winning_links.txt，同时推送 Telegram 通知
- 每一轮结束（不管中不中）都会正常关闭这个上下文，不再需要保留任何东西

⚠️ 重要提醒：请先手动实测这个链接过几小时后是否还能正常登录领取，
   确认可靠后再放心长期挂着跑，否则抽到再多也可能白抽。

运行前需要：
    pip install playwright keyboard requests
    python -m playwright install chromium

配置好 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID 后运行：
    python auto_draw_v9.py

想设置成开机自动后台运行，用Windows"任务计划程序"：
    新建任务 -> 触发器选"用户登录时" -> 操作选启动程序 ->
    程序填 python.exe 完整路径，参数填这个脚本的完整路径。
"""

import os
import time
import json
import requests
from playwright.sync_api import sync_playwright

# ================= 配置 =================

CONFIG = {
    "SITE_URL": "https://store.nordcurrent.com/zh-hans/games/airplane-chefs",
    "WELCOME_BUTTON_SELECTOR": "#wheel-of-fortune-welcome-button",
    "SPIN_BUTTON_SELECTOR": ".WheelOfFortune_wheelSpinButton__5SmSu",
    "POPUP_READY_SELECTOR": "#wheel-of-fortune-reward-accept-not-logged-in",
    "REWARD_SCOPE_SELECTOR": "#reward",
    # 已通过实际抽中验证过的关键词（高置信度）
    "CONFIRMED_KEYWORDS": {
        "main-wheel-boost50": "+50% 加量价值",
    },
    # 从转盘固定分区推测、尚未在真实开奖弹窗里验证过的关键词（标注"疑似"，仍会通知你去核实）
    "GUESSED_KEYWORDS": {
        "main-wheel-boost300": "x3 加量价值（疑似，未验证，请核实截图）",
        "main-wheel-boost200": "x2 加量价值（疑似，未验证，请核实截图）",
    },
    "MAX_ATTEMPTS": 500,
    "POPUP_WAIT_MS": 15000,
    "RESULTS_FILE": "winning_links.txt",
}

# TODO: 填入你从 BotFather / userinfobot 拿到的信息
# 优先从环境变量读取（云端用GitHub Secrets注入），本地没设置的话用下面这两行兜底
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8790942459:AAE2Ocblajjstka1SP9Jn0wJgOm9osuCq1M")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8159794503")

COOKIE_ACCEPT_TEXTS = [
    "全部允许", "接受所有", "全部接受", "同意", "我同意", "接受",
    "Accept All", "Accept all", "Accept", "I Agree", "Agree",
]


# ================= 工具函数 =================

def send_telegram(text: str):
    if "在这里填" in TELEGRAM_BOT_TOKEN:
        print("  [提示] 还没配置Telegram Token，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        if r.status_code == 200:
            print("  已发送Telegram通知")
        else:
            print("  Telegram发送失败：", r.text)
    except Exception as e:
        print("  Telegram发送出错：", e)


def send_telegram_photo(photo_path: str, caption: str):
    """把弹窗截图直接发到Telegram，方便你登录领取前肉眼核实奖励内容"""
    if "在这里填" in TELEGRAM_BOT_TOKEN:
        print("  [提示] 还没配置Telegram Token，跳过截图通知")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            r = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=20,
            )
        if r.status_code == 200:
            print("  已发送Telegram截图")
        else:
            print("  Telegram截图发送失败：", r.text)
    except Exception as e:
        print("  Telegram截图发送出错：", e)


def accept_cookies_if_present(page, timeout_ms=10000):
    def try_click_accept_all(wait_ms):
        try:
            page.wait_for_selector("#c-consent-accept-all", state="visible", timeout=wait_ms)
            page.click("#c-consent-accept-all")
            return True
        except Exception:
            return False

    if try_click_accept_all(timeout_ms):
        return True
    try:
        page.reload(wait_until="domcontentloaded")
    except Exception:
        pass
    if try_click_accept_all(timeout_ms):
        return True

    for text in COOKIE_ACCEPT_TEXTS:
        try:
            btn = page.get_by_text(text, exact=False).first
            btn.wait_for(state="visible", timeout=1500)
            btn.click()
            return True
        except Exception:
            pass
    return False


def check_desired_reward(page):
    scope = page.locator(CONFIG["REWARD_SCOPE_SELECTOR"])
    try:
        src = scope.locator("img").first.get_attribute("src", timeout=3000) or ""
    except Exception:
        src = ""
    try:
        text_preview = scope.inner_text(timeout=3000).strip()
    except Exception:
        text_preview = ""

    # 第一层：已验证过的关键词，高置信度
    for kw, label in CONFIG["CONFIRMED_KEYWORDS"].items():
        if kw in src:
            return True, src, text_preview, label

    # 第二层：推测的关键词（x2/x3），标注"疑似"
    for kw, label in CONFIG["GUESSED_KEYWORDS"].items():
        if kw in src:
            return True, src, text_preview, label

    # 第三层保底：图片文件名含"boost"但数字对不上任何已知关键词，
    # 说明大概率还是加量类奖励，只是具体数值/文件名跟猜测的不完全一致
    if "boost" in src.lower():
        return True, src, text_preview, "疑似加量类奖励（图片关键词未匹配到具体数值，请核实截图）"

    return False, src, text_preview, None


def get_claim_link(page) -> str:
    """提取"接受奖励"按钮的完整链接（带唯一标识），无论中不中奖都可以拿到"""
    try:
        href = page.locator(CONFIG["POPUP_READY_SELECTOR"]).first.get_attribute("href", timeout=3000)
        if href and href.startswith("/"):
            return "https://store.nordcurrent.com" + href
        return href or ""
    except Exception:
        return ""


def save_winning_result(attempt, reward_label, text_preview, src, claim_link):
    record = {
        "attempt": attempt,
        "reward_type": reward_label,
        "reward_text": text_preview,
        "reward_image": src,
        "claim_link": claim_link,
    }
    with open(CONFIG["RESULTS_FILE"], "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ================= 主流程 =================

def run():
    win_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for attempt in range(1, CONFIG["MAX_ATTEMPTS"] + 1):
            context = browser.new_context()
            page = context.new_page()

            try:
                print(f"\n========== 第 {attempt} 次 ==========")
                page.goto(CONFIG["SITE_URL"], wait_until="domcontentloaded")

                accept_cookies_if_present(page)

                try:
                    page.wait_for_selector(CONFIG["WELCOME_BUTTON_SELECTOR"], timeout=5000)
                    page.click(CONFIG["WELCOME_BUTTON_SELECTOR"])
                except Exception:
                    pass

                page.wait_for_selector(CONFIG["SPIN_BUTTON_SELECTOR"], timeout=10000)
                page.click(CONFIG["SPIN_BUTTON_SELECTOR"])

                page.wait_for_selector(CONFIG["POPUP_READY_SELECTOR"], timeout=CONFIG["POPUP_WAIT_MS"])

                hit, src, text_preview, reward_label = check_desired_reward(page)
                print(f"  奖励: {text_preview}  图片: {src}")

                if hit:
                    win_count += 1
                    claim_link = get_claim_link(page)
                    print(f"  🎉 命中理想奖励（{reward_label}）！链接: {claim_link}")
                    save_winning_result(attempt, reward_label, text_preview, src, claim_link)

                    # 登录前先截一张弹窗的图，让你能亲眼核实奖励内容再决定要不要登录领取
                    screenshot_path = f"win_{attempt}.png"
                    try:
                        page.locator(CONFIG["REWARD_SCOPE_SELECTOR"]).screenshot(path=screenshot_path)
                    except Exception:
                        page.screenshot(path=screenshot_path)  # 兜底：截整个页面

                    caption = (
                        f"🎉 飞机大厨转盘中奖！第 {attempt} 次\n"
                        f"奖励类型: {reward_label}\n"
                        f"页面显示文字: {text_preview}\n"
                        f"请先看这张截图核实奖励内容，确认无误再点下面的链接登录领取:\n{claim_link}"
                    )
                    send_telegram_photo(screenshot_path, caption)
                else:
                    print("  未中理想奖励。")

            except Exception as e:
                print("  本轮出错：", e)

            context.close()
            time.sleep(0.3)

        browser.close()

    print(f"\n脚本结束。共命中 {win_count} 次，详情见 {CONFIG['RESULTS_FILE']}")


if __name__ == "__main__":
    run()
