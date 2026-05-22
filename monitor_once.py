"""
單次執行版入口（給 GitHub Actions 用）。

與 product_monitor.py 的差異：
- 不啟動 APScheduler 常駐排程，改由 GitHub Actions 的 cron 每次觸發。
- 只跑一次 check_new_products() 就結束。

所有抓取 / 解析 / Discord 通知 / 狀態檔邏輯，都直接重用 product_monitor.py，
不重複實作，避免兩份程式行為不一致。

Discord webhook 來源：
- GitHub Actions 會把 repository secret 設成環境變數 DISCORD_WEBHOOK_URL，
  product_monitor 在 import 時就會讀到（不需要 .env 檔）。
"""

import sys
import product_monitor as pm


def main():
    if not pm.DISCORD_WEBHOOK_URL:
        # 不中斷，仍會執行偵測（log 看得到抓到幾筆），只是不發通知。
        print("⚠️  DISCORD_WEBHOOK_URL 未設定：本次只偵測、不發 Discord 通知。")
        print("    （在 GitHub repo 的 Settings → Secrets 設定後即可發送）")

    print("=" * 50)
    print("單次偵測開始（GitHub Actions 模式）")
    print(f"監控網址數: {len(pm.URLS_TO_MONITOR)}")
    print("=" * 50)

    try:
        pm.check_new_products()
    except Exception as e:
        # 讓 Actions 該步驟標記為失敗，方便你在執行記錄看到問題
        print(f"❌ 偵測過程發生錯誤: {e}")
        raise

    print("單次偵測結束。")


if __name__ == "__main__":
    sys.exit(main())
