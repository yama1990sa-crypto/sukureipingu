#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Indeed 求人スクレイピングツール - Web アプリ版
=================================================

ブラウザから操作できる簡易UI。indeed_scraper.py の中核ロジックを
Flask 経由で呼び出し、進捗をポーリング表示、完了後にCSVをダウンロードできる。

起動方法(誰でも使える版):
    Mac    : start_mac.command をダブルクリック
    Windows: start_windows.bat をダブルクリック
    -> 自動でセットアップし、ブラウザが自動で開きます

起動方法(手動):
    pip install -r requirements.txt
    python -m playwright install chromium
    python app.py
    -> ブラウザで http://127.0.0.1:5000 を開く

【重要】Indeed の利用規約は自動収集を禁止しています。自己責任で使用してください。
"""

import os
import threading
import uuid
import webbrowser
from datetime import datetime

from flask import Flask, render_template, request, jsonify, send_from_directory, abort

from indeed_scraper import is_indeed_url, run_scrape_any, run_company_search, save_csv

# index.html はリポジトリのルート直下に置く運用(GitHubへのドラッグ&ドロップ
# アップロードでサブフォルダ構成を作らずに済むように、テンプレートフォルダを
# app.py と同じディレクトリにしている)
app = Flask(__name__, template_folder=os.path.dirname(os.path.abspath(__file__)) or ".")

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# job_id -> {"status": "running"|"done"|"error", "logs": [...], "count": int, "filename": str}
JOBS = {}
JOBS_LOCK = threading.Lock()

MAX_ITEMS = 30  # 詳細ページを開く件数 / 企業名検索の社数の上限

# 複数人での共有利用を想定し、同時に1件しかスクレイピングを実行しないようにする
# (サーバーのリソース節約と、Indeedへの同時アクセス集中を避けるため)
SCRAPE_LOCK = threading.Lock()


def append_log(job_id: str, message: str):
    with JOBS_LOCK:
        JOBS[job_id]["logs"].append(message)


def worker(job_id: str, mode: str, base_url: str, companies: list, count: int, filename: str):
    try:
        append_log(job_id, "ブラウザを起動しています…")

        def cb(msg):
            append_log(job_id, msg)

        if mode == "companies":
            items = run_company_search(companies, headless=True, progress_cb=cb, max_companies=count)
        else:
            items = run_scrape_any(base_url, pages=count, headless=True, progress_cb=cb)

        output_path = os.path.join(OUTPUT_DIR, filename)
        save_csv(items, output_path)

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["count"] = len(items)
            JOBS[job_id]["filename"] = filename
        append_log(job_id, f"完了: {len(items)} 件を取得しました。")
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        append_log(job_id, f"エラーが発生しました: {e}")
    finally:
        SCRAPE_LOCK.release()


@app.route("/")
def index():
    return render_template("index.html", max_pages=MAX_ITEMS)


@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.get_json(force=True)
    mode = data.get("mode", "url")
    count = int(data.get("pages", 1))
    count = max(1, min(count, MAX_ITEMS))

    base_url = ""
    companies = []

    if mode == "companies":
        raw = data.get("companies", "") or ""
        companies = [line.strip() for line in raw.splitlines() if line.strip()]
        if not companies:
            return jsonify({"error": "会社名を1行に1社ずつ、1つ以上入力してください"}), 400
    else:
        url = (data.get("url") or "").strip()
        if not url.startswith("http"):
            return jsonify({"error": "有効なURLを入力してください"}), 400
        if is_indeed_url(url):
            return jsonify({
                "error": "Indeedは現在サーバーからのアクセスがブロックされているため、"
                         "このツールでは利用できません。別の求人サイトのURLを指定してください。"
            }), 400
        base_url = url

    if not SCRAPE_LOCK.acquire(blocking=False):
        return jsonify({"error": "現在ほかの人が取得を実行中です。しばらく待ってからもう一度お試しください。"}), 429

    job_id = uuid.uuid4().hex[:12]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"scraped_data_{timestamp}.csv"

    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "logs": [], "count": 0, "filename": None}

    t = threading.Thread(
        target=worker, args=(job_id, mode, base_url, companies, count, filename), daemon=True
    )
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        return jsonify(dict(job))


@app.route("/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job["status"] != "done" or not job["filename"]:
            abort(404)
        filename = job["filename"]
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    # クラウド環境(Render等)は PORT 環境変数でポートを指定してくる。
    # ローカルのワンクリック起動時はこれが無いので、127.0.0.1 + ブラウザ自動起動にする。
    is_local = "PORT" not in os.environ
    port = int(os.environ.get("PORT", 5000))
    host = "127.0.0.1" if is_local else "0.0.0.0"

    print("Indeed 求人スクレイピングツールを起動しました。")
    if is_local:
        url = f"http://127.0.0.1:{port}"
        print(f"ブラウザが自動で開かない場合は {url} を開いてください。")
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    else:
        print(f"port {port} で待ち受けています。")

    app.run(host=host, port=port, debug=False, threaded=True)
