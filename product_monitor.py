import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# 切到腳本所在目錄，讓 .env / last_product_ids.json 用相對路徑
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)


# ============================================================
# ⚙️ 設定區
# ============================================================
def _load_env_file():
    """簡易 .env 讀取（不依賴 python-dotenv）"""
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# 監控清單：每筆要有顯示名稱、網站類型（決定用哪個 parser）、URL
URLS_TO_MONITOR = [
    {
        "name": "9局職棒25",
        "site": "8591",
        "url": "https://www.8591.com.tw/v3/mall/list/53011?searchGame=53011&searchType=2&post_time_sort=1",
    },
    {
        "name": "9局職棒26",  # TODO: 確認 68598 對應的遊戲名稱
        "site": "8591",
        "url": "https://www.8591.com.tw/v3/mall/list/68598?searchGame=68598&searchType=2&post_time_sort=1",
    },
    {
        "name": "台北租屋(中正/大同/中山/松山/萬華)",
        "site": "591rent",
        # sort=posttime_desc：依「最新刊登」排序，第一頁就是最新上架，避免漏抓
        "url": "https://rent.591.com.tw/list?region=1&section=1,2,6,5,3&price=16666$_33333$&acreage=8$_22$&shType=host&sort=posttime_desc",
    },
]

LAST_ID_FILE = "last_product_ids.json"
DISCORD_INTERVAL = 1.0  # 兩則 Discord 訊息之間的間隔秒數，避開 rate limit
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


# ============================================================
# 🌐 抓取商品（各網站專屬 parser，統一回傳格式）
#    每個 fetcher 回傳 [{id:int, title:str, price:str, detail_url:str}, ...]
#    依 ID 由大到小排序。失敗回傳 None。
# ============================================================
def _http_get(url):
    """共用 HTTP GET，失敗印錯誤訊息並回傳 None。"""
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp
    except requests.RequestException as e:
        print(f"    ❌ 取得網頁失敗: {e}")
        return None


def fetch_8591(url):
    """抓取 8591 列表頁。"""
    resp = _http_get(url)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.select('a[href*="/v3/mall/detail/"]')

    products = []
    seen = set()
    for a in anchors:
        href = a.get("href", "")
        m = re.search(r"/v3/mall/detail/(\d+)", href)
        if not m:
            continue
        product_id = int(m.group(1))
        if product_id in seen:
            continue
        seen.add(product_id)

        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))

        card = a.find_parent("div", class_="list-item")
        price = ""
        if card is not None:
            price_match = re.search(r"\$\s*[\d,]+", card.get_text(" ", strip=True))
            if price_match:
                price = price_match.group()

        detail_url = href if href.startswith("http") else f"https://www.8591.com.tw{href}"
        products.append(
            {"id": product_id, "title": title, "price": price, "detail_url": detail_url}
        )

    products.sort(key=lambda p: p["id"], reverse=True)
    return products


def fetch_591_rent(url):
    """抓取 591 租屋列表頁。

    重要：要抓 .list-wrapper .item（真正的搜尋結果，會依使用者的 sort 排序），
    不要抓 .recommend-ware（那是頁面下方的演算法推薦區塊，跟搜尋條件無關）。
    """
    resp = _http_get(url)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select(".list-wrapper .item")
    products = []
    seen = set()

    for item in items:
        a = item.select_one('a[href*="rent.591.com.tw/"]')
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"rent\.591\.com\.tw/(\d+)", href)
        if not m:
            continue
        post_id = int(m.group(1))
        if post_id in seen:
            continue
        seen.add(post_id)

        # 標題：a tag 的純文字（不含「優選好屋」等贅字）
        title = re.sub(r"\s+", " ", a.get_text(strip=True))

        # 「置頂」= 付費廣告位，加前綴標示讓使用者分辨
        labels_text = item.get_text(" | ", strip=True)
        if "置頂" in labels_text.split(" | ")[:5]:
            title = "[置頂] " + title

        # 價格：.item-info-price 元素，例如「20,000元/月」
        price = ""
        price_el = item.select_one(".item-info-price")
        if price_el is not None:
            price_text = price_el.get_text(" ", strip=True)
            price_match = re.search(r"\d{1,3}(?:,\d{3})+", price_text)
            if price_match:
                price = f"${price_match.group()}/月"

        products.append({
            "id": post_id,
            "title": title,
            "price": price,
            "detail_url": f"https://rent.591.com.tw/{post_id}",
        })

    products.sort(key=lambda p: p["id"], reverse=True)
    return products


# site → fetcher 派發表
FETCHERS = {
    "8591": fetch_8591,
    "591rent": fetch_591_rent,
}


def fetch_products(cfg):
    """依 cfg["site"] 派發到對應的 fetcher。未知 site 回 None。"""
    site = cfg.get("site", "8591")
    fetcher = FETCHERS.get(site)
    if fetcher is None:
        print(f"    ❌ 未知的 site 類型: {site}")
        return None
    return fetcher(cfg["url"])


# ============================================================
# 📨 Discord 通知
# ============================================================
def send_discord_notification(message):
    """送出單則 Discord 通知，回傳 True/False。遇到 429 自動重試一次。"""
    if not DISCORD_WEBHOOK_URL:
        # 只印一次警告，避免每件商品都重複洗版
        return False

    payload = {"username": "8591新品監控", "content": message}
    for attempt in range(2):
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        except requests.RequestException as e:
            print(f"    ❌ Discord 連線失敗: {e}")
            return False

        if resp.status_code == 204:
            return True
        if resp.status_code == 429 and attempt == 0:
            retry_after = 5.0
            try:
                retry_after = float(resp.json().get("retry_after", 5.0))
            except Exception:
                pass
            print(f"    ⏳ Discord 限流，等 {retry_after:.1f}s 後重試")
            time.sleep(retry_after + 0.5)
            continue
        print(f"    ❌ Discord HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    return False


# ============================================================
# 💾 狀態檔
#   v2 格式：{url_key: [id, id, ...]}（已見過的 ID 集合）
#   v1 格式：{url_key: int}（單一 max_id）→ 讀到時轉成 None 觸發重新 baseline
# ============================================================
def load_last_ids():
    """回傳 {url_key: set[int] 或 None}。None 表示「需要重新建立 baseline」。"""
    if not os.path.exists(LAST_ID_FILE):
        return {}
    try:
        with open(LAST_ID_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ⚠️  讀取 {LAST_ID_FILE} 失敗，視為空字典: {e}")
        return {}

    result = {}
    for k, v in raw.items():
        if isinstance(v, list):
            result[k] = set(v)
        elif isinstance(v, int):
            # 舊格式：單一 max_id，不夠資訊重建 set，標記為待重新 baseline
            print(f"  🔄 偵測到舊格式狀態（{k}），將重新建立 baseline")
            result[k] = None
        else:
            result[k] = None
    return result


def save_last_ids(last_ids):
    """原子寫入：先寫 .tmp 再 rename，避免中斷時檔案損毀"""
    tmp_path = LAST_ID_FILE + ".tmp"
    serializable = {
        k: sorted(v) for k, v in last_ids.items() if isinstance(v, set)
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, LAST_ID_FILE)


# ============================================================
# 🧾 訊息格式
# ============================================================
def format_message(display_name, product, list_url):
    price = product["price"] or "(未顯示)"
    return (
        f"🎉 **新上架！**\n"
        f"**來源：** {display_name}\n"
        f"**標題：** {product['title']}\n"
        f"**價格：** {price}\n"
        f"**連結：** {product['detail_url']}\n"
        f"**列表頁：** {list_url}"
    )


# ============================================================
# 🔍 主檢查邏輯
# ============================================================
def check_new_products():
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 開始檢查...")
    if not DISCORD_WEBHOOK_URL:
        print("  ⚠️  DISCORD_WEBHOOK_URL 未設定，本次只偵測不發 Discord 通知")
    last_ids = load_last_ids()
    new_last_ids = dict(last_ids)

    for cfg in URLS_TO_MONITOR:
        url = cfg["url"]
        display_name = cfg["name"]
        url_key = url.split("?")[0]

        print(f"  ▶ {display_name}")
        products = fetch_products(cfg)
        if not products:
            print(f"    跳過（取得失敗或頁面無商品）")
            continue

        current_ids = {p["id"] for p in products}
        prev_set = last_ids.get(url_key)

        # 首次紀錄 / 舊格式遷移：建立 baseline，不通知
        if prev_set is None:
            print(f"    📝 首次紀錄 {len(current_ids)} 筆 baseline，不發通知")
            new_last_ids[url_key] = current_ids
            continue

        new_items = [p for p in products if p["id"] not in prev_set]
        if not new_items:
            print(f"    沒有新品（當前 {len(current_ids)} 筆，全在 baseline 內）")
            # 即使沒新品，也用「prev_set ∪ current_ids」更新（防止之後重複通知淡出又回來的物件）
            new_last_ids[url_key] = prev_set | current_ids
            continue

        print(f"    🎯 發現 {len(new_items)} 件新品（含重新刊登）")
        sent_ids = set()
        # 由舊 ID 到新 ID 發送，Discord 上由上往下越來越新
        for p in sorted(new_items, key=lambda x: x["id"]):
            ok = send_discord_notification(format_message(display_name, p, url))
            print(f"      [{'✓' if ok else '✗'}] id={p['id']} {p['title'][:40]}")
            if ok:
                sent_ids.add(p["id"])
            time.sleep(DISCORD_INTERVAL)

        # baseline 更新策略：保留「上次 baseline + 這次成功通知」的 ID
        # 失敗的不加進 baseline，下次還會重試
        new_last_ids[url_key] = prev_set | sent_ids

    save_last_ids(new_last_ids)
    print("檢查完成。")


# ============================================================
# 🚀 主程式
# ============================================================
def main():
    print("=" * 50)
    print("8591 新品監控啟動")
    print(f"監控網址數: {len(URLS_TO_MONITOR)}")
    print(f"Discord webhook: {'已設定' if DISCORD_WEBHOOK_URL else '⚠️  未設定（請建立 .env 檔）'}")
    print("排程: 每日 09:00–23:59，每 3 分鐘")
    print("=" * 50)

    scheduler = BlockingScheduler()
    scheduler.add_job(
        check_new_products,
        CronTrigger(hour="9-23", minute="*/3"),
        id="check_products",
        misfire_grace_time=60,  # 系統忙碌時延後 60s 內仍補跑，避免漏觸發
    )

    print("\n[初始檢查]")
    check_new_products()

    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\n程式停止")


if __name__ == "__main__":
    main()
