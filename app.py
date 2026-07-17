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

from indeed_scraper import build_search_url, run_scrape, save_csv

# index.html はリポジトリのルート直下に置く運用(GitHubへのドラッグ&ドロップ
# アップロードでサブフォルダ構成を作らずに済むように、テンプレートフォルダを
# app.py と同じディレクトリにしている)
app = Flask(__name__, template_folder=os.path.dirname(os.path.abspath(__file__)) or ".")

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# job_id -> {"status": "running"|"done"|"error", "logs": [...], "count": int, "filename": str}
JOBS = {}
JOBS_LOCK = threading.Lock()

MAX_PAGES = 100  # 暴走防止の上限

# 複数人での共有利用を想定し、同時に1件しかスクレイピングを実行しないようにする
# (サーバーのリソース節約と、Indeedへの同時アクセス集中を避けるため)
SCRAPE_LOCK = threading.Lock()


def append_log(job_id: str, message: str):
    with JOBS_LOCK:
        JOBS[job_id]["logs"].append(message)


def worker(job_id: str, base_url: str, pages: int, filename: str):
    try:
        append_log(job_id, "ブラウザを起動しています…")

        def cb(msg):
            append_log(job_id, msg)

        jobs = run_scrape(base_url, pages=pages, headless=True, progress_cb=cb)
        output_path = os.path.join(OUTPUT_DIR, filename)
        save_csv(jobs, output_path)

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["count"] = len(jobs)
            JOBS[job_id]["filename"] = filename
        append_log(job_id, f"完了: {len(jobs)} 件を取得しました。")
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        append_log(job_id, f"エラーが発生しました: {e}")
    finally:
        SCRAPE_LOCK.release()


@app.route("/")
def index():
    return render_template("index.html", max_pages=MAX_PAGES)


@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.get_json(force=True)
    mode = data.get("mode")
    pages = int(data.get("pages", 1))
    pages = max(1, min(pages, MAX_PAGES))

    if mode == "url":
        url = (data.get("url") or "").strip()
        if not url.startswith("http"):
            return jsonify({"error": "有効なURLを入力してください"}), 400
        base_url = url
    else:
        keyword = (data.get("keyword") or "").strip()
        location = (data.get("location") or "").strip()
        if not keyword and not location:
            return jsonify({"error": "キーワードか勤務地のどちらかを入力してください"}), 400
        base_url = build_search_url(keyword, location)

    if not SCRAPE_LOCK.acquire(blocking=False):
        return jsonify({"error": "現在ほかの人が取得を実行中です。しばらく待ってからもう一度お試しください。"}), 429

    job_id = uuid.uuid4().hex[:12]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"indeed_jobs_{timestamp}.csv"

    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "logs": [], "count": 0, "filename": None}

    t = threading.Thread(target=worker, args=(job_id, base_url, pages, filename), daemon=True)
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
