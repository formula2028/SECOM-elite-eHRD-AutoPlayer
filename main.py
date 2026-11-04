# -*- coding: utf-8 -*-
"""
=============================================================================
              eHRD 數位學習自動掛機系統 (免安裝可攜版 - GUI 版)
=============================================================================
特點：
  1. 現代化 GUI 介面：PyQt5 打造，操作直覺，支援無頭背景執行與有頭瀏覽器接手。
  2. 背景無頭模式 (Headless)：完全不彈出瀏覽器干擾工作，驗證碼直接於 GUI 顯示輸入。
  3. 雙向防失焦偽造：自動覆寫 document.visibilityState 與 CourseReader 監聽器。
  4. 多格式教材支援：完整支援影片教材 (Video.js / JWPlayer) 與投影片簡報 (PDF.js)。
  5. 智慧章節過濾：自動識別並略過測驗、問卷與作業，專注掛滿閱讀時長。
  6. 平台計時器精準監控：以 LMS 內部 CourseReader._totalReadTime 實時秒數為準。
  7. 安全時數結算：達標前絕不提前結束，以 Pause=0 安全結算入帳。
  8. 人機協作接手：影片與簡報教材全部完成後彈出系統通知，一鍵開啟瀏覽器接手測驗作答。
=============================================================================
"""

import sys
import os
import time
import json
import ctypes
import queue

# 確保 Windows 主控台與輸出編碼相容性
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt5.QtGui import QPixmap, QFont, QIcon, QColor
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QProgressBar,
    QTextEdit, QCheckBox, QGroupBox, QMessageBox, QFrame, QSplitter
)
from playwright.sync_api import sync_playwright

# 預設帳號與登入首頁
DEFAULT_USER_ID = ""
DEFAULT_PASSWORD = ""
LOGIN_URL = "https://newschool.secom.com.tw/ehrd/eHRDOrg"

def show_native_message_box(title, message):
    """彈出 Windows 原生對話框通知同仁"""
    try:
        MB_OK = 0x0
        MB_ICONINFORMATION = 0x40
        MB_SYSTEMMODAL = 0x1000
        ctypes.windll.user32.MessageBoxW(0, message, title, MB_OK | MB_ICONINFORMATION | MB_SYSTEMMODAL)
    except Exception:
        pass

def format_seconds(seconds):
    """將秒數格式化為 mm:ss"""
    if seconds is None:
        return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"

# =============================================================================
# 背景自動化工作執行緒 (Playwright Worker)
# =============================================================================
class AutomationWorker(QThread):
    # 信號定義
    sig_log = pyqtSignal(str)
    sig_status = pyqtSignal(str, str)             # text, color
    sig_captcha_loaded = pyqtSignal(bytes)        # captcha image bytes
    sig_login_result = pyqtSignal(bool, str)      # success, message
    sig_courses_loaded = pyqtSignal(list)         # list of course dicts
    sig_progress = pyqtSignal(str, int, int, str) # title, current_sec, req_sec, status_text
    sig_all_completed = pyqtSignal(str)           # target_course_title
    sig_error = pyqtSignal(str)

    def __init__(self, headless=True):
        super().__init__()
        self.headless = headless
        self.cmd_queue = queue.Queue()
        self.is_running = True
        self.stop_hanging_flag = False

        # Playwright 物件引用
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def post_command(self, cmd_name, **kwargs):
        """傳送操作命令至工作執行緒"""
        self.cmd_queue.put((cmd_name, kwargs))

    def run(self):
        """工作執行緒主迴圈"""
        edge_args = [
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--autoplay-policy=no-user-gesture-required"
        ]

        try:
            self.sig_log.emit("[系統] 正在啟動自動化引擎...")
            self.playwright = sync_playwright().start()

            # 優先使用 Microsoft Edge，備選 Chrome
            try:
                self.browser = self.playwright.chromium.launch(
                    channel="msedge", headless=self.headless, args=edge_args
                )
            except Exception:
                try:
                    self.browser = self.playwright.chromium.launch(
                        channel="chrome", headless=self.headless, args=edge_args
                    )
                except Exception as e:
                    self.sig_error.emit(f"無法啟動 Edge 或 Chrome 瀏覽器: {e}")
                    return

            self.context = self.browser.new_context(viewport={"width": 1366, "height": 768})
            self.page = self.context.new_page()

            # 自動接受彈出對話框
            self.page.on("dialog", lambda dialog: dialog.accept())

            # 初始連線至登入頁面並抓取驗證碼
            self._load_login_page_and_captcha()

            # 處理命令佇列
            while self.is_running:
                try:
                    cmd_name, kwargs = self.cmd_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if cmd_name == "refresh_captcha":
                    self._refresh_captcha()
                elif cmd_name == "login":
                    self._do_login(kwargs.get("username"), kwargs.get("password"), kwargs.get("captcha"))
                elif cmd_name == "refresh_courses":
                    self._fetch_courses()
                elif cmd_name == "start_hanging":
                    self._start_hanging_course(kwargs.get("course_title"))
                elif cmd_name == "open_browser_for_quiz":
                    self._open_headed_browser_for_quiz()
                elif cmd_name == "exit":
                    break

        except Exception as e:
            self.sig_log.emit(f"[錯誤] 背景執行緒異常: {e}")
            self.sig_error.emit(str(e))
        finally:
            self._cleanup()

    def _cleanup(self):
        try:
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.sig_log.emit("[系統] 自動化引擎已安全關閉。")

    # ---------------- 登入相關 ----------------
    def _load_login_page_and_captcha(self):
        try:
            self.sig_status.emit("正在連線至登入頁面...", "#1976D2")
            self.sig_log.emit(f"[*] 正在連線至 eHRD 登入頁: {LOGIN_URL}")
            self.page.goto(LOGIN_URL)
            self.page.wait_for_load_state("domcontentloaded")

            # 等待驗證碼圖片完成載入 (避開初始的 loading.gif)
            self.page.wait_for_selector("#CaptchaImg", timeout=12000)
            self.page.wait_for_function(
                "() => { const img = document.querySelector('#CaptchaImg'); return img && !img.src.includes('loading.gif'); }",
                timeout=10000
            )

            # 截取驗證碼真實截圖
            captcha_img_bytes = self.page.locator("#CaptchaImg").screenshot()
            self.sig_captcha_loaded.emit(captcha_img_bytes)
            self.sig_status.emit("就緒：請輸入驗證碼後點擊登入", "#2E7D32")
            self.sig_log.emit("[✓] 驗證碼圖片載入成功，已呈現於 GUI 介面。")
        except Exception as e:
            self.sig_status.emit("驗證碼載入失敗，請重試", "#C62828")
            self.sig_log.emit(f"[!] 驗證碼載入錯誤: {e}")

    def _refresh_captcha(self):
        try:
            self.sig_status.emit("正在重新整理驗證碼...", "#1976D2")
            # 點擊換一張按鈕
            reset_btn = self.page.locator("#resetCaptch")
            if reset_btn.count() > 0:
                reset_btn.click()
            else:
                self.page.locator("#CaptchaImg").click()

            self.page.wait_for_timeout(500)
            self.page.wait_for_function(
                "() => { const img = document.querySelector('#CaptchaImg'); return img && !img.src.includes('loading.gif'); }",
                timeout=8000
            )
            captcha_img_bytes = self.page.locator("#CaptchaImg").screenshot()
            self.sig_captcha_loaded.emit(captcha_img_bytes)
            self.sig_status.emit("驗證碼已更新，請輸入新驗證碼", "#2E7D32")
            self.sig_log.emit("[✓] 已刷新驗證碼圖片。")
        except Exception as e:
            self.sig_status.emit("刷新失敗，請重試", "#C62828")
            self.sig_log.emit(f"[!] 刷新驗證碼失敗: {e}")

    def _do_login(self, username, password, captcha):
        try:
            self.sig_status.emit("正在驗證登入資訊...", "#1976D2")
            self.sig_log.emit(f"[*] 正在提交帳號 [{username}] 與驗證碼...")

            self.page.wait_for_selector("#UserID", timeout=5000)
            self.page.fill("#UserID", username)
            self.page.fill("#Esrom", password)
            self.page.fill("#Captcha", captcha)

            # 點擊登入按鈕
            self.page.click("#loginButton")

            # 等待判斷結果 (最多等待 8 秒)
            start_wait = time.time()
            logged_in = False
            error_reason = ""

            while time.time() - start_wait < 8:
                self.page.wait_for_timeout(800)

                # 判斷是否成功登入 (網址包含 home、或有 passport、或有首頁元件)
                current_url = self.page.url.lower()
                if "home" in current_url:
                    logged_in = True
                    break

                try:
                    has_passport = self.page.evaluate(
                        "() => typeof sessionStorage !== 'undefined' && sessionStorage.getItem('passport') !== null"
                    )
                    if has_passport:
                        logged_in = True
                        break
                except Exception:
                    pass

                if self.page.locator("text=我的課程").count() > 0 or self.page.locator("#Desc, #LoginCenerImg, .TitleBlock").count() > 0:
                    logged_in = True
                    break

                # 判斷是否有錯誤提示文字
                try:
                    page_text = self.page.inner_text("body")
                    if "驗證碼輸入錯誤" in page_text or "驗證碼不符" in page_text:
                        error_reason = "驗證碼輸入錯誤，請重新輸入。"
                        break
                    elif "帳號或密碼錯誤" in page_text or "帳號不存在" in page_text:
                        error_reason = "帳號或密碼錯誤，請確認後重試。"
                        break
                except Exception:
                    pass

            if logged_in:
                self.sig_status.emit("登入成功！正在載入課程清單...", "#2E7D32")
                self.sig_log.emit("[✓] 恭喜！登入成功，已安全取得連線權杖 (Passport)。")
                self.sig_login_result.emit(True, "登入成功")
                # 載入課程
                self._fetch_courses()
            else:
                if not error_reason:
                    error_reason = "登入超時或驗證碼不正確，請重新整理後重試。"
                self.sig_status.emit(error_reason, "#C62828")
                self.sig_log.emit(f"[!] 登入失敗: {error_reason}")
                self.sig_login_result.emit(False, error_reason)
                # 刷新驗證碼供下次輸入
                self._refresh_captcha()

        except Exception as e:
            err_msg = f"登入發生例外: {e}"
            self.sig_status.emit("登入錯誤", "#C62828")
            self.sig_log.emit(f"[!] {err_msg}")
            self.sig_login_result.emit(False, err_msg)
            self._refresh_captcha()

    # ---------------- 課程清單 ----------------
    def _fetch_courses(self):
        try:
            self.sig_status.emit("正在讀取「我的課程」...", "#1976D2")
            self.sig_log.emit("[*] 正在點擊「我的課程」...")
            self.page.wait_for_timeout(2000)

            # 點擊我的課程
            try:
                my_course_btn = self.page.locator("text=我的課程").first
                if my_course_btn.count() > 0:
                    my_course_btn.click()
                else:
                    self.page.locator(".TableCell:has-text('我的課程'), a:has-text('我的課程')").first.click()
                self.page.wait_for_timeout(3500)
            except Exception as e:
                self.sig_log.emit(f"[!] 點擊我的課程警告: {e}")

            # 掃描課程卡片 (涵蓋各類主題與卡片結構)
            course_cards = self.page.evaluate("""() => {
                const titles = document.querySelectorAll(
                    '.InfoBlock.MoveTop .Title, .ListItem .Title, .CardBlock .Title, .CourseTitle, [class*="CourseCard"] .Title'
                );
                const res = [];
                for (let el of titles) {
                    const text = el.innerText.trim();
                    if (text && !res.some(r => r.title === text)) {
                        res.push({ title: text });
                    }
                }
                return res;
            }""")

            self.sig_courses_loaded.emit(course_cards)
            self.sig_status.emit("課程清單載入完成，請選擇課程開始掛機", "#2E7D32")
            self.sig_log.emit(f"[✓] 成功掃描到 {len(course_cards)} 門待修課程。")
        except Exception as e:
            self.sig_log.emit(f"[!] 讀取課程清單失敗: {e}")
            self.sig_status.emit("讀取課程失敗，請手動重新整理", "#C62828")

    # ---------------- 掛機核心演算法 ----------------
    def _start_hanging_course(self, target_course_title):
        self.stop_hanging_flag = False
        try:
            self.sig_log.emit(f"\n=======================================================")
            self.sig_log.emit(f"[*] 開始掛機目標課程: 【{target_course_title}】")
            self.sig_log.emit(f"=======================================================")
            self.sig_status.emit(f"正在進入課程: {target_course_title}...", "#1976D2")

            # 1. 點擊卡片開啟氣泡/進入彈窗
            keyword = target_course_title[:8].strip()
            self.sig_log.emit(f"[*] 正在點選課程卡片: 【{keyword}】...")
            card_clicked = False
            for sel in [
                f".Title:has-text('{keyword}')",
                f":text('{target_course_title}')",
                f":text('{keyword}')"
            ]:
                try:
                    c_loc = self.page.locator(sel).first
                    if c_loc.count() > 0:
                        c_loc.click(force=True)
                        card_clicked = True
                        break
                except Exception:
                    pass

            if not card_clicked:
                self.page.locator(f"text={target_course_title}").first.click(force=True)
            self.page.wait_for_timeout(3000)

            # 2. 點擊氣泡內的「教室」
            self.sig_log.emit("[*] 正在進入「教室」視窗...")
            classroom_clicked = False
            classroom_selectors = [
                ".aEnrich-ActionButton:has-text('教室')",
                ".buttonBlock:has-text('教室')",
                "span:has-text('教室')",
                "button:has-text('教室')",
                "a:has-text('教室')",
                ":has-text('教室')"
            ]
            for c_sel in classroom_selectors:
                try:
                    c_btn = self.page.locator(c_sel).first
                    if c_btn.count() > 0 and c_btn.is_visible():
                        c_btn.click(force=True)
                        classroom_clicked = True
                        self.sig_log.emit(f"    [✓] 已點選教室按鈕 ({c_sel})")
                        break
                except Exception:
                    pass

            if not classroom_clicked:
                self.page.evaluate("""() => {
                    const els = Array.from(document.querySelectorAll('span, a, button, div'));
                    for (let el of els) {
                        if (el.innerText && el.innerText.trim() === '教室' && el.offsetParent !== null) {
                            el.click();
                            return;
                        }
                    }
                }""")

            self.page.wait_for_timeout(5000)
            self.sig_log.emit("[✓] 成功進入教室！開始掃描章節目錄...")

            completed_indices = set()
            completed_titles = set()

            while not self.stop_hanging_flag:
                # 重新掃描當前章節活動項目
                activities = self.page.evaluate("""() => {
                    const list = [];
                    const rows = document.querySelectorAll('.ListItem');
                    rows.forEach((row, index) => {
                        const titleEl = row.querySelector('.TableCell:nth-child(3), [class*=\"Title\"], .TableCell:nth-child(2)');
                        const title = titleEl ? titleEl.innerText.trim() : `單元 ${index+1}`;
                        
                        // 解析規定閱讀時長
                        let reqSeconds = 0;
                        const reqMatchMin = row.innerText.match(/閱讀時間達\\s*([\\d\\.]+)\\s*分鐘/);
                        if (reqMatchMin) {
                            reqSeconds = Math.round(parseFloat(reqMatchMin[1]) * 60);
                        } else {
                            const reqMatchSec = row.innerText.match(/閱讀時間達\\s*(\\d+)\\s*秒/);
                            if (reqMatchSec) {
                                reqSeconds = parseInt(reqMatchSec[1], 10);
                            } else {
                                const reqMatchHr = row.innerText.match(/閱讀時間達\\s*([\\d\\.]+)\\s*小時/);
                                if (reqMatchHr) {
                                    reqSeconds = Math.round(parseFloat(reqMatchHr[1]) * 3600);
                                }
                            }
                        }
                        
                        // 解析目前累計時數
                        let currentSeconds = 0;
                        const timeMatch = row.innerText.match(/(\\d{2}:\\d{2}:\\d{2})/);
                        if (timeMatch) {
                            const parts = timeMatch[1].split(':');
                            currentSeconds = parseInt(parts[0], 10)*3600 + parseInt(parts[1], 10)*60 + parseInt(parts[2], 10);
                        }
                        
                        // 通過狀態判斷
                        const hasPassedIcon = row.querySelector('.Icon-Passed, [class*=\"Pass\"], .aEnrich-icon-Passed, .pass') !== null;
                        const isPassed = hasPassedIcon || (reqSeconds > 0 && currentSeconds >= reqSeconds);
                        
                        // 播放圖示 / 進入按鈕
                        const hasPlayBtn = row.querySelector('.aEnrich-icon-LPDoRead, .Img.cursor_pointer, [class*=\"LPDoRead\"]') !== null;
                        
                        // 排除測驗、問卷、無須計時項目
                        const isQuiz = title.includes('測驗') || title.includes('問卷') || title.includes('考試') || 
                                       title.includes('評量') || row.innerText.includes('未測驗') || 
                                       row.innerText.includes('限時') || (reqSeconds === 0 && !hasPlayBtn);

                        list.push({
                            index: index,
                            title: title,
                            reqSeconds: reqSeconds,
                            currentSeconds: currentSeconds,
                            isPassed: isPassed,
                            hasPlayBtn: hasPlayBtn,
                            isQuiz: isQuiz
                        });
                    });
                    return list;
                }""")

                # 篩選待掛機項目 (排除已通過、排除測驗/問卷、排除本次已結算)
                pending_items = [
                    a for a in activities 
                    if not a['isPassed'] 
                    and a['hasPlayBtn'] 
                    and not a['isQuiz'] 
                    and a['index'] not in completed_indices
                    and a['title'].split('\n')[0].strip() not in completed_titles
                ]

                if not pending_items:
                    self.sig_log.emit("\n" + "=" * 60)
                    self.sig_log.emit("[✓] 恭喜！本課程所有影片與簡報教材皆已全部掛滿完畢！")
                    self.sig_log.emit("=" * 60 + "\n")
                    self.sig_progress.emit("全部章節已完成", 100, 100, "完課")
                    self.sig_status.emit("恭喜！所有教材播放完畢", "#2E7D32")
                    self.sig_all_completed.emit(target_course_title)
                    break

                current_target = pending_items[0]
                target_idx = current_target['index']
                title_clean = current_target['title'].split('\n')[0].strip()
                req_sec = current_target['reqSeconds']
                curr_sec = current_target['currentSeconds']
                remaining_sec = max(0, req_sec - curr_sec)

                self.sig_log.emit(f"[▶] 準備掛機章節: 【{title_clean}】")
                self.sig_log.emit(f"    - 規定時長: {format_seconds(req_sec)} | 目前紀錄: {format_seconds(curr_sec)} | 預計需掛機: {format_seconds(remaining_sec)}")

                # 點擊該 ListItem 內部的播放圖示
                try:
                    target_row = self.page.locator(".ListItem").nth(target_idx)
                    row_play_btn = target_row.locator(".aEnrich-icon-LPDoRead, .Img.cursor_pointer, [class*='LPDoRead']").first
                    if row_play_btn.count() > 0:
                        row_play_btn.click(force=True)
                    else:
                        target_row.click(force=True)
                except Exception:
                    self.page.evaluate(f"""(idx) => {{
                        const rows = document.querySelectorAll('.ListItem');
                        if (rows[idx]) {{
                            const btn = rows[idx].querySelector('.aEnrich-icon-LPDoRead, .Img.cursor_pointer, [class*="LPDoRead"]');
                            if (btn) btn.click();
                            else rows[idx].click();
                        }}
                    }}""", target_idx)

                # 等待播放器視窗載入
                self.sig_log.emit("    [*] 等待播放器視窗載入中...")
                for _ in range(20):
                    if self.stop_hanging_flag:
                        break
                    self.page.wait_for_timeout(1000)
                    is_open = self.page.evaluate("""() => {
                        const overlay = document.querySelector('#ReadOverlay');
                        return overlay && overlay.style.display !== 'none' && overlay.offsetParent !== null;
                    }""")
                    if is_open:
                        break

                if self.stop_hanging_flag:
                    break

                self.page.wait_for_timeout(3000)
                self._ensure_video_and_timer_active()
                self.sig_log.emit("    [*] 正在背景安全計時播放中（已自動處理靜音、投影片翻頁與防失焦）...")

                start_epoch = time.time()
                total_wait_sec = remaining_sec + 15 if remaining_sec > 0 else 60

                # 實時以平台 CourseReader._totalReadTime 為最高準則
                last_slide_flip = time.time()
                while not self.stop_hanging_flag:
                    now = time.time()
                    if now - last_slide_flip >= 10:
                        self._ensure_video_and_timer_active()
                        last_slide_flip = now

                    reader_info = self._get_reader_info()
                    platform_seconds = reader_info.get("totalSeconds", 0)
                    status_code = reader_info.get("status", 0)
                    status_label = "計時中" if status_code == 3 else f"同步中({status_code})"

                    elapsed_wall = int(time.time() - start_epoch)

                    # 發送進度至 GUI
                    self.sig_progress.emit(title_clean, platform_seconds, req_sec, status_label)
                    self.sig_status.emit(f"掛機中: {title_clean} ({format_seconds(platform_seconds)} / {format_seconds(req_sec)})", "#1976D2")

                    # 當平台紀錄時數達到要求 (+10 秒緩衝安全結算)，或實體掛機時間達標
                    if (req_sec > 0 and platform_seconds >= req_sec + 10) or (req_sec == 0 and elapsed_wall >= total_wait_sec):
                        self.sig_log.emit(f"\n    [✓] 本章節平台時數已達標 ({format_seconds(platform_seconds)})！")
                        break

                    self.page.wait_for_timeout(2000)

                if self.stop_hanging_flag:
                    self.sig_log.emit("    [!] 同仁中斷掛機，正在結算目前時數...")
                    self._click_return_and_exit()
                    break

                # 執行回傳時數並離開 (確保以 Pause=0 回傳入帳)
                self.page.wait_for_timeout(2000)
                self._click_return_and_exit()

                # 等待播放器關閉確認
                self.sig_log.emit("    [*] 正在等待伺服器記錄時數並返回活動目錄...")
                closed_confirmed = False
                for _ in range(15):
                    self.page.wait_for_timeout(1000)
                    is_closed = self.page.evaluate("""() => {
                        const overlay = document.querySelector('#ReadOverlay');
                        return !overlay || overlay.style.display === 'none' || overlay.offsetParent === null;
                    }""")
                    if is_closed:
                        closed_confirmed = True
                        self.sig_log.emit("    [✓] 播放器已正式關閉，時數成功回傳入庫！")
                        break

                if not closed_confirmed:
                    self.page.evaluate("""() => {
                        if (window.oUIManager && window.oUIManager.CourseReader) {
                            window.oUIManager.CourseReader.close();
                        }
                        const overlay = document.querySelector('#ReadOverlay');
                        if (overlay) overlay.style.display = 'none';
                    }""")
                    self.page.wait_for_timeout(2000)

                completed_indices.add(target_idx)
                completed_titles.add(title_clean)
                self.sig_log.emit(f"    [✓] 已成功結算單元【{title_clean}】，自動推進至下一單元！\n")
                self.page.wait_for_timeout(3000)

        except Exception as e:
            self.sig_log.emit(f"[!] 掛機過程發生例外: {e}")
            self.sig_status.emit("掛機中斷或發生錯誤", "#C62828")

    def _ensure_video_and_timer_active(self):
        """穿透 iframe 啟動 Video.js 播放 / PDF.js 投影片翻頁並維持計時狀態"""
        # 1. 偽造全域焦點與 CourseReader 防休眠
        try:
            self.page.evaluate("""() => {
                try {
                    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
                    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
                    document.hasFocus = () => true;
                    window.onblur = null;
                    
                    if (window.oUIManager && window.oUIManager.CourseReader) {
                        const cr = window.oUIManager.CourseReader;
                        cr.visibleconfirm = false;
                        cr.visibilitychangeFn = function() {};
                        
                        const uid = cr._coffer.data("currentWinUID");
                        if (uid && cr.readerStatus !== 3) {
                            cr.readerStatus = 2; // ready
                            cr._WithoutToken = false;
                            cr._startCountReadTime(uid);
                            cr._startCountResponseTime(uid);
                        }
                        try { cr.postMessageToIframeForJwplayer("play", null, null); } catch(e) {}
                    }
                } catch(e) {}
            }""")
        except Exception:
            pass

        # 2. 穿透所有 Frame 處理影片 (Video.js) 或 投影片 (PDF.js)
        for frame in self.page.frames:
            try:
                frame.evaluate("""() => {
                    try {
                        // 影片控制
                        const vids = document.querySelectorAll('video');
                        vids.forEach(v => {
                            v.muted = true;
                            v.volume = 0;
                            if (v.paused) v.play().catch(() => {});
                        });
                        const playBtns = document.querySelectorAll('.vjs-big-play-button, button.vjs-play-control, .jw-display-icon-display');
                        playBtns.forEach(b => {
                            try { b.click(); } catch(e) {}
                        });
                        if (typeof videojs !== 'undefined') {
                            try {
                                const player = videojs('MediaContainer');
                                if (player) {
                                    player.muted(true);
                                    if (player.paused()) player.play();
                                }
                            } catch(e) {}
                        }

                        // PDF / 投影片控制 (PDF.js viewer)
                        const nextBtn = document.querySelector('#next, button[id*=\"next\"], .next');
                        if (nextBtn && !nextBtn.disabled) {
                            nextBtn.click();
                        } else {
                            const prevBtn = document.querySelector('#previous, button[id*=\"previous\"], .previous');
                            if (prevBtn && !prevBtn.disabled) {
                                prevBtn.click();
                            }
                        }
                        const viewerContainer = document.querySelector('#viewerContainer');
                        if (viewerContainer) {
                            viewerContainer.scrollTop += 300;
                            if (viewerContainer.scrollTop + viewerContainer.clientHeight >= viewerContainer.scrollHeight - 50) {
                                viewerContainer.scrollTop = 0;
                            }
                        }
                    } catch(e) {}
                }""")
            except Exception:
                pass

    def _get_reader_info(self):
        try:
            return self.page.evaluate("""() => {
                if (window.oUIManager && window.oUIManager.CourseReader) {
                    const cr = window.oUIManager.CourseReader;
                    return {
                        hasReader: true,
                        status: cr.readerStatus,
                        totalSeconds: cr._totalReadTime || 0,
                        isOpen: !(!document.querySelector('#ReadOverlay') || document.querySelector('#ReadOverlay').style.display === 'none')
                    };
                }
                return { hasReader: false, status: 0, totalSeconds: 0, isOpen: false };
            }""")
        except Exception:
            return { "hasReader": False, "status": 0, "totalSeconds": 0, "isOpen": False }

    def _click_return_and_exit(self):
        self.sig_log.emit("    [*] 正在向伺服器提交本單元時數...")
        try:
            self.page.evaluate("""() => {
                if (window.oUIManager && window.oUIManager.CourseReader) {
                    const cr = window.oUIManager.CourseReader;
                    cr.visibleconfirm = false;
                    cr.readerStatus = 3; // reading
                }
            }""")
        except Exception:
            pass

        clicked = False
        for frame in self.page.frames:
            try:
                for sel in [
                    "text=回傳時數並離開",
                    ":has-text('回傳時數並離開')",
                    "div.toolbar-button.exit",
                    "input[value*='回傳']",
                    "button:has-text('回傳')",
                    "a:has-text('回傳')",
                    ".btn:has-text('回傳')"
                ]:
                    b = frame.locator(sel).first
                    if b.count() > 0 and b.is_visible():
                        b.click(timeout=3000)
                        clicked = True
                        self.sig_log.emit(f"    [✓] 已在 Frame ({frame.name or 'Main'}) 點擊【回傳時數並離開】！")
                        break
                if clicked:
                    break
            except Exception:
                pass

        if not clicked:
            try:
                res = self.page.evaluate("""() => {
                    const docs = [document];
                    const iframes = document.querySelectorAll('iframe');
                    iframes.forEach(f => {
                        try {
                            if (f.contentDocument) {
                                docs.push(f.contentDocument);
                                f.contentDocument.querySelectorAll('iframe').forEach(inner => {
                                    try { if (inner.contentDocument) docs.push(inner.contentDocument); } catch(e) {}
                                });
                            }
                        } catch(e) {}
                    });
                    for (let doc of docs) {
                        const els = Array.from(doc.querySelectorAll('button, input, a, div, span'));
                        for (let el of els) {
                            const t = (el.innerText || el.value || '').trim();
                            if (t === '回傳時數並離開' || (t.includes('回傳') && t.includes('離開'))) {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }""")
                if res:
                    clicked = True
                    self.sig_log.emit("    [✓] 已透過 DOM 樹觸發【回傳時數並離開】！")
            except Exception:
                pass

        try:
            self.page.wait_for_timeout(2500)
            is_still_open = self.page.evaluate("""() => {
                const overlay = document.querySelector('#ReadOverlay');
                return overlay && overlay.style.display !== 'none' && overlay.offsetParent !== null;
            }""")
            if is_still_open:
                self.page.evaluate("""() => {
                    if (window.oUIManager && window.oUIManager.CourseReader) {
                        window.oUIManager.CourseReader.close();
                    }
                }""")
                self.sig_log.emit("    [✓] 已透過 CourseReader.close() 提交時數結算！")
        except Exception:
            pass

        return clicked

    def _open_headed_browser_for_quiz(self):
        """開啟有頭瀏覽器並繼承登入狀態，方便同仁進行測驗作答"""
        try:
            self.sig_log.emit("[*] 正在開啟可視化瀏覽器視窗供您接手作答...")
            cookies = self.context.cookies()
            session_storage = self.page.evaluate("() => Object.assign({}, window.sessionStorage)")
            target_url = self.page.url

            # 啟動有頭瀏覽器
            headed_browser = self.playwright.chromium.launch(channel="msedge", headless=False)
            headed_context = headed_browser.new_context(viewport={"width": 1366, "height": 768})
            headed_context.add_cookies(cookies)
            headed_page = headed_context.new_page()

            # 注入 sessionStorage
            headed_page.add_init_script(f"""
                const ss = {json.dumps(session_storage)};
                for (const [k, v] of Object.entries(ss)) {{
                    sessionStorage.setItem(k, v);
                }}
            """)
            headed_page.goto(target_url)
            self.sig_log.emit("[✓] 瀏覽器視窗已成功開啟並還原置頂，請直接於瀏覽器進行測驗或問卷！")
        except Exception as e:
            self.sig_log.emit(f"[!] 開啟瀏覽器失敗: {e}")

# =============================================================================
# 主視窗 GUI 介面 (PyQt5)
# =============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("eHRD 數位學習自動掛機系統 (免安裝可攜版)")
        self.resize(780, 720)
        self.setMinimumSize(700, 650)

        # 啟動背景 Worker
        self.worker = AutomationWorker(headless=True)
        self.worker.sig_log.connect(self.append_log)
        self.worker.sig_status.connect(self.update_status)
        self.worker.sig_captcha_loaded.connect(self.display_captcha)
        self.worker.sig_login_result.connect(self.on_login_result)
        self.worker.sig_courses_loaded.connect(self.populate_courses)
        self.worker.sig_progress.connect(self.update_progress)
        self.worker.sig_all_completed.connect(self.on_all_completed)
        self.worker.sig_error.connect(self.on_worker_error)
        self.worker.start()

        self._init_ui()

    def _init_ui(self):
        # 樣式表設計
        self.setStyleSheet("""
            QMainWindow {
                background-color: #F8F9FA;
            }
            QGroupBox {
                font-weight: bold;
                font-size: 13px;
                border: 1px solid #D0D7DE;
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 15px;
                background-color: #FFFFFF;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 4px;
                color: #24292F;
            }
            QLabel {
                font-size: 12px;
                color: #24292F;
            }
            QLineEdit {
                border: 1px solid #D0D7DE;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 13px;
                background-color: #FFFFFF;
            }
            QLineEdit:focus {
                border: 2px solid #0969DA;
            }
            QPushButton {
                background-color: #0969DA;
                color: white;
                font-weight: bold;
                border-radius: 6px;
                padding: 7px 14px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #0858B9;
            }
            QPushButton:pressed {
                background-color: #064593;
            }
            QPushButton:disabled {
                background-color: #8C959F;
                color: #EAEEF2;
            }
            QProgressBar {
                border: 1px solid #D0D7DE;
                border-radius: 6px;
                text-align: center;
                height: 22px;
                font-size: 12px;
                background-color: #F6F8FA;
            }
            QProgressBar::chunk {
                background-color: #2DA44E;
                border-radius: 5px;
            }
            QComboBox {
                border: 1px solid #D0D7DE;
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 13px;
                background-color: #FFFFFF;
            }
            QTextEdit {
                border: 1px solid #D0D7DE;
                border-radius: 6px;
                background-color: #1E1E1E;
                color: #D4D4D4;
                font-family: Consolas, "Courier New", monospace;
                font-size: 11px;
            }
        """)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(16, 12, 16, 16)
        main_layout.setSpacing(10)

        # 頂部標題區
        title_box = QHBoxLayout()
        lbl_app_title = QLabel("🎓 eHRD 數位學習自動掛機系統")
        lbl_app_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #0969DA;")
        title_box.addWidget(lbl_app_title)

        lbl_sub_title = QLabel("(免安裝可攜版 ‧ 背景無頭執行 ‧ 自動時數回傳)")
        lbl_sub_title.setStyleSheet("font-size: 12px; color: #57606A; margin-top: 4px;")
        title_box.addWidget(lbl_sub_title)
        title_box.addStretch()
        main_layout.addLayout(title_box)

        # ================= 區塊 1: 登入設定 =================
        self.group_login = QGroupBox("1. 系統登入與驗證碼")
        login_layout = QVBoxLayout(self.group_login)
        login_layout.setContentsMargins(16, 14, 16, 14)
        login_layout.setSpacing(10)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("帳號:"))
        self.edit_user = QLineEdit(DEFAULT_USER_ID)
        self.edit_user.setPlaceholderText("請輸入員工編號")
        self.edit_user.setFixedWidth(140)
        row1.addWidget(self.edit_user)

        row1.addSpacing(15)
        row1.addWidget(QLabel("密碼:"))
        self.edit_pwd = QLineEdit(DEFAULT_PASSWORD)
        self.edit_pwd.setPlaceholderText("請輸入登入密碼")
        self.edit_pwd.setEchoMode(QLineEdit.Password)
        self.edit_pwd.setFixedWidth(140)
        row1.addWidget(self.edit_pwd)

        self.chk_show_pwd = QCheckBox("顯示密碼")
        self.chk_show_pwd.stateChanged.connect(
            lambda s: self.edit_pwd.setEchoMode(QLineEdit.Normal if s else QLineEdit.Password)
        )
        row1.addWidget(self.chk_show_pwd)
        row1.addStretch()
        login_layout.addLayout(row1)

        # 驗證碼區
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("驗證碼圖片:"))
        
        # 驗證碼圖片 Label
        self.lbl_captcha = QLabel("[ 正在載入驗證碼... ]")
        self.lbl_captcha.setFixedSize(140, 48)
        self.lbl_captcha.setAlignment(Qt.AlignCenter)
        self.lbl_captcha.setStyleSheet("border: 1px dashed #0969DA; border-radius: 6px; background-color: #F6F8FA;")
        row2.addWidget(self.lbl_captcha)

        self.btn_refresh_captcha = QPushButton("🔄 換一張")
        self.btn_refresh_captcha.setStyleSheet("background-color: #6E7781;")
        self.btn_refresh_captcha.clicked.connect(self.on_refresh_captcha_clicked)
        row2.addWidget(self.btn_refresh_captcha)

        row2.addSpacing(15)
        row2.addWidget(QLabel("輸入驗證碼:"))
        self.edit_captcha = QLineEdit()
        self.edit_captcha.setFixedWidth(110)
        self.edit_captcha.setMaxLength(6)
        self.edit_captcha.setPlaceholderText("請輸入 4 碼")
        self.edit_captcha.returnPressed.connect(self.on_login_clicked)
        row2.addWidget(self.edit_captcha)

        self.btn_login = QPushButton("登入系統")
        self.btn_login.setFixedWidth(110)
        self.btn_login.clicked.connect(self.on_login_clicked)
        row2.addWidget(self.btn_login)

        row2.addStretch()
        login_layout.addLayout(row2)

        main_layout.addWidget(self.group_login)

        # ================= 區塊 2: 課程與掛機控制 =================
        self.group_course = QGroupBox("2. 課程管理與自動掛機")
        self.group_course.setEnabled(False)  # 登入後才啟用
        course_layout = QVBoxLayout(self.group_course)
        course_layout.setContentsMargins(16, 14, 16, 14)
        course_layout.setSpacing(10)

        crow1 = QHBoxLayout()
        crow1.addWidget(QLabel("選擇掛機課程:"))
        self.combo_courses = QComboBox()
        self.combo_courses.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        crow1.addWidget(self.combo_courses)

        self.btn_refresh_courses = QPushButton("🔄 刷新課程")
        self.btn_refresh_courses.setStyleSheet("background-color: #6E7781;")
        self.btn_refresh_courses.clicked.connect(self.on_refresh_courses_clicked)
        crow1.addWidget(self.btn_refresh_courses)

        self.btn_start_hang = QPushButton("🚀 開始自動掛機")
        self.btn_start_hang.setStyleSheet("background-color: #2DA44E; font-size: 13px;")
        self.btn_start_hang.clicked.connect(self.on_start_hanging_clicked)
        crow1.addWidget(self.btn_start_hang)
        course_layout.addLayout(crow1)

        # 即時進度卡片
        progress_box = QVBoxLayout()
        p_row = QHBoxLayout()
        self.lbl_chapter_title = QLabel("章節: [ 尚未開始掛機 ]")
        self.lbl_chapter_title.setStyleSheet("font-weight: bold; color: #24292F;")
        p_row.addWidget(self.lbl_chapter_title)

        p_row.addStretch()
        self.lbl_chapter_time = QLabel("平台累計: 00:00 / 00:00 (0.0%)")
        self.lbl_chapter_time.setStyleSheet("color: #0969DA; font-weight: bold;")
        p_row.addWidget(self.lbl_chapter_time)
        progress_box.addLayout(p_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        progress_box.addWidget(self.progress_bar)
        course_layout.addLayout(progress_box)

        # 人機分工按鈕列
        action_row = QHBoxLayout()
        self.btn_open_browser = QPushButton("🌐 開啟瀏覽器接手作答 (測驗/問卷)")
        self.btn_open_browser.setStyleSheet("background-color: #8250DF;")
        self.btn_open_browser.clicked.connect(self.on_open_browser_clicked)
        action_row.addWidget(self.btn_open_browser)

        self.btn_stop_hang = QPushButton("⏹ 停止掛機")
        self.btn_stop_hang.setStyleSheet("background-color: #CF222E;")
        self.btn_stop_hang.setEnabled(False)
        self.btn_stop_hang.clicked.connect(self.on_stop_hanging_clicked)
        action_row.addWidget(self.btn_stop_hang)
        course_layout.addLayout(action_row)

        main_layout.addWidget(self.group_course)

        # ================= 區塊 3: 狀態列與即時 Log =================
        log_group = QGroupBox("3. 系統即時執行日誌")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(12, 10, 12, 10)

        # 狀態標籤
        self.lbl_main_status = QLabel("狀態: 正在連線啟動中...")
        self.lbl_main_status.setStyleSheet("font-size: 13px; font-weight: bold; color: #1976D2;")
        log_layout.addWidget(self.lbl_main_status)

        # 文字日誌
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        log_layout.addWidget(self.txt_log)
        main_layout.addWidget(log_group)

    # ================= 槽函數 (Slots) =================
    def on_refresh_captcha_clicked(self):
        self.btn_refresh_captcha.setEnabled(False)
        self.worker.post_command("refresh_captcha")
        QtCore.QTimer.singleShot(2000, lambda: self.btn_refresh_captcha.setEnabled(True))

    def on_login_clicked(self):
        user = self.edit_user.text().strip()
        pwd = self.edit_pwd.text().strip()
        cap = self.edit_captcha.text().strip()

        if not user or not pwd:
            QMessageBox.warning(self, "提示", "請輸入帳號與密碼！")
            return
        if not cap:
            QMessageBox.warning(self, "提示", "請輸入驗證碼！")
            return

        self.btn_login.setEnabled(False)
        self.btn_refresh_captcha.setEnabled(False)
        self.worker.post_command("login", username=user, password=pwd, captcha=cap)

    def on_refresh_courses_clicked(self):
        self.btn_refresh_courses.setEnabled(False)
        self.worker.post_command("refresh_courses")
        QtCore.QTimer.singleShot(3000, lambda: self.btn_refresh_courses.setEnabled(True))

    def on_start_hanging_clicked(self):
        selected_title = self.combo_courses.currentText().strip()
        if not selected_title:
            QMessageBox.warning(self, "提示", "請先選擇欲掛機的課程！")
            return

        self.btn_start_hang.setEnabled(False)
        self.btn_stop_hang.setEnabled(True)
        self.combo_courses.setEnabled(False)
        self.btn_refresh_courses.setEnabled(False)
        self.worker.post_command("start_hanging", course_title=selected_title)

    def on_stop_hanging_clicked(self):
        self.worker.stop_hanging_flag = True
        self.btn_stop_hang.setEnabled(False)
        self.btn_start_hang.setEnabled(True)
        self.combo_courses.setEnabled(True)
        self.btn_refresh_courses.setEnabled(True)
        self.append_log("[!] 同仁點擊停止掛機，正在結束當前單元...")

    def on_open_browser_clicked(self):
        self.worker.post_command("open_browser_for_quiz")

    # ================= 背景信號回呼 =================
    def append_log(self, text):
        self.txt_log.append(text)
        self.txt_log.moveCursor(QtGui.QTextCursor.End)

    def update_status(self, text, color):
        self.lbl_main_status.setText(f"狀態: {text}")
        self.lbl_main_status.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {color};")

    def display_captcha(self, img_bytes):
        pixmap = QPixmap()
        pixmap.loadFromData(img_bytes)
        if not pixmap.isNull():
            # 平滑放大讓同仁看得更清楚
            scaled = pixmap.scaled(self.lbl_captcha.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.lbl_captcha.setPixmap(scaled)
            self.lbl_captcha.setText("")
        self.edit_captcha.clear()
        self.edit_captcha.setFocus()
        self.btn_login.setEnabled(True)
        self.btn_refresh_captcha.setEnabled(True)

    def on_login_result(self, success, message):
        self.btn_login.setEnabled(True)
        self.btn_refresh_captcha.setEnabled(True)
        if success:
            self.group_login.setStyleSheet("background-color: #F0FDF4;")
            self.edit_user.setEnabled(False)
            self.edit_pwd.setEnabled(False)
            self.edit_captcha.setEnabled(False)
            self.btn_login.setEnabled(False)
            self.btn_refresh_captcha.setEnabled(False)
            self.group_course.setEnabled(True)
            QMessageBox.information(self, "成功", "登入成功！已成功載入課程管理面板。")
        else:
            QMessageBox.critical(self, "登入失敗", f"{message}")

    def populate_courses(self, course_cards):
        self.combo_courses.clear()
        if not course_cards:
            self.combo_courses.addItem("（未掃描到課程，請手動刷新）")
            return

        default_index = 0
        for idx, c in enumerate(course_cards):
            title = c['title']
            self.combo_courses.addItem(title)
            # 優先預選種子教官或性別平等訓練
            if "種子教官" in title or "性別平等" in title:
                default_index = idx

        if self.combo_courses.count() > 0:
            self.combo_courses.setCurrentIndex(default_index)

    def update_progress(self, title, curr_sec, req_sec, status_text):
        self.lbl_chapter_title.setText(f"章節: {title}")
        target_req = max(1, req_sec)
        pct = min(100.0, (curr_sec / target_req) * 100) if req_sec > 0 else 100.0
        self.progress_bar.setValue(int(pct))
        self.lbl_chapter_time.setText(f"平台累計: {format_seconds(curr_sec)} / {format_seconds(req_sec)} ({pct:.1f}%) | {status_text}")

    def on_all_completed(self, target_course_title):
        self.btn_start_hang.setEnabled(True)
        self.btn_stop_hang.setEnabled(False)
        self.combo_courses.setEnabled(True)
        self.btn_refresh_courses.setEnabled(True)
        self.progress_bar.setValue(100)
        self.lbl_chapter_time.setText("教材全數完成 (100%)")

        notice_title = "eHRD 課程自動完課通知"
        notice_content = (
            f"恭喜！【{target_course_title}】的所有影片與簡報教材已全數播放完畢，時數已安全回傳伺服器！\n\n"
            "請點擊介面上的【開啟瀏覽器接手作答】按鈕，\n"
            "系統將自動為您開啟瀏覽器進入測驗/問卷頁面。"
        )
        show_native_message_box(notice_title, notice_content)
        QMessageBox.information(self, notice_title, notice_content)

    def on_worker_error(self, err):
        QMessageBox.critical(self, "錯誤", f"系統發生嚴重錯誤:\n{err}")

    def closeEvent(self, event):
        """關閉視窗時優雅關閉背景工作"""
        self.worker.stop_hanging_flag = True
        self.worker.post_command("exit")
        self.worker.is_running = False
        self.worker.wait(2000)
        event.accept()

def main():
    # 支援 Windows 高 DPI 縮放
    try:
        QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft JhengHei UI", 9))
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
