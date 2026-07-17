#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Indeed 求人情報スクレイピングツール
====================================

Playwright (Chromium) を使い、Indeed の検索結果ページから求人情報を
抽出して CSV に保存します。

【重要】法的リスクについて
Indeed の利用規約は自動化されたスクレイピングを禁止しています。
このスクリプトを使う場合は以下を理解した上で、自己責任で実行してください。
  - IP ブロックやアカウント停止の可能性がある
  - 取得したデータの商用利用・再配布は規約違反になり得る
  - robots.txt / 利用規約は https://jp.indeed.com/legal で随時確認すること
  - アクセス頻度を抑え、短時間に大量リクエストを送らない

セットアップ:
    pip install playwright
    python -m playwright install chromium

使い方の例:
    # 検索結果ページのURLを直接指定
    python indeed_scraper.py --url "https://jp.indeed.com/l-兵庫県-神戸市-求人.html" --pages 3

    # キーワードと勤務地から検索
    python indeed_scraper.py --keyword "エンジニア" --location "神戸市" --pages 2

    # 出力ファイル名を指定
    python indeed_scraper.py --url "..." --output jobs.csv
"""

import argparse
import csv
import random
import re
import sys
import time
from dataclasses import dataclass, fields
from typing import List, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
except ImportError:
    print(
        "エラー: playwright がインストールされていません。\n"
        "  pip install playwright\n"
        "  python -m playwright install chromium\n"
        "を実行してからもう一度お試しください。",
        file=sys.stderr,
    )
    sys.exit(1)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ブロック/CAPTCHA ページによく現れる文言
BLOCK_MARKERS = [
    "additional verification",
    "unusual traffic",
    "are you a human",
    "セキュリティ確認",
    "確認が必要です",
]

# Indeed 以外の汎用サイト向け: ページ内で「同じ形をした要素が繰り返されている
# ブロック」(検索結果の1件1件、記事一覧の1記事など)を自動検出し、
# タイトル・リンク・概要テキストを抜き出すヒューリスティック。
# 完全な精度は出ないが、サイトごとにセレクタを用意しなくても
# ある程度の情報を取得できる。
GENERIC_EXTRACT_JS = """
() => {
  const all = Array.from(document.querySelectorAll('body *'));
  const candidates = all.filter(el => {
    if (!el.querySelector('a[href]')) return false;
    const text = (el.innerText || '').trim();
    if (text.length < 15 || text.length > 4000) return false;
    return true;
  });

  const groups = {};
  candidates.forEach(el => {
    const cls = (el.className && el.className.toString) ? el.className.toString() : '';
    const sig = el.tagName + '.' + cls.split(' ').filter(Boolean).slice(0, 3).join('.');
    (groups[sig] = groups[sig] || []).push(el);
  });

  let bestSig = null, bestCount = 0;
  for (const sig in groups) {
    const els = groups[sig];
    if (els.length >= 3 && els.length > bestCount && els.length <= 200) {
      bestCount = els.length;
      bestSig = sig;
    }
  }
  if (!bestSig) return [];

  return groups[bestSig].slice(0, 100).map(el => {
    const linkEl = el.querySelector('a[href]');
    const headingEl = el.querySelector('h1,h2,h3,h4,h5,h6') || linkEl;
    const title = (headingEl ? headingEl.innerText : '').trim().slice(0, 200);
    const url = linkEl ? linkEl.href : '';
    let snippet = (el.innerText || '').trim();
    if (title) snippet = snippet.replace(title, '').trim();
    snippet = snippet.slice(0, 200);
    return { title, url, snippet };
  }).filter(item => item.title);
}
"""


@dataclass
class Job:
    title: str = ""
    company: str = ""
    location: str = ""
    salary: str = ""
    employment_type: str = ""
    posted: str = ""
    snippet: str = ""
    job_id: str = ""
    url: str = ""


def build_search_url(keyword: str, location: str) -> str:
    """キーワード・勤務地から Indeed の検索URLを組み立てる"""
    params = {}
    if keyword:
        params["q"] = keyword
    if location:
        params["l"] = location
    return "https://jp.indeed.com/jobs?" + urlencode(params)


def with_start_param(url: str, start: int) -> str:
    """検索結果URLに start= パラメータ(ページ送り)を付与する"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs["start"] = [str(start)]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def text_or_empty(locator) -> str:
    try:
        if locator.count() > 0:
            return locator.first.inner_text().strip().replace("\n", " ")
    except Exception:
        pass
    return ""


def extract_job_id(href: str) -> str:
    m = re.search(r"jk=([a-f0-9]+)", href or "")
    return m.group(1) if m else ""


def looks_blocked(html: str) -> bool:
    lowered = html.lower()
    return any(marker.lower() in lowered for marker in BLOCK_MARKERS)


def scrape_page(page, url: str) -> List[Job]:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)

    # 求人カードが描画されるまで待機(SPA的な追加描画に対応)
    try:
        page.wait_for_selector(
            "div.job_seen_beacon, td.resultContent, div[data-testid='slider_item']",
            timeout=15000,
        )
    except PWTimeoutError:
        pass

    # 少し待ってから追加のレイジーロード分も反映させる
    page.wait_for_timeout(1500)

    html = page.content()
    if looks_blocked(html):
        print("警告: ブロック/確認ページの可能性があります。取得を中断します。", file=sys.stderr)
        return []

    cards = page.locator("div.job_seen_beacon, td.resultContent")
    count = cards.count()
    jobs: List[Job] = []

    for i in range(count):
        card = cards.nth(i)
        job = Job()

        # タイトル & リンク & job_id
        title_link = card.locator("h2.jobTitle a, a.jcs-JobTitle")
        job.title = text_or_empty(title_link)
        try:
            if title_link.count() > 0:
                href = title_link.first.get_attribute("href") or ""
                if href.startswith("/"):
                    href = "https://jp.indeed.com" + href
                job.url = href
                job.job_id = extract_job_id(href) or (
                    title_link.first.get_attribute("data-jk") or ""
                )
        except Exception:
            pass

        job.company = text_or_empty(
            card.locator("span[data-testid='company-name'], span.companyName")
        )
        job.location = text_or_empty(
            card.locator("div[data-testid='text-location'], div.companyLocation")
        )

        # 給与・雇用形態は data-testid='attribute_snippet_testid' に複数入ることがある
        attrs = card.locator("div[data-testid='attribute_snippet_testid']")
        attr_texts = []
        try:
            for j in range(attrs.count()):
                t = attrs.nth(j).inner_text().strip()
                if t:
                    attr_texts.append(t)
        except Exception:
            pass
        if attr_texts:
            job.salary = attr_texts[0]
            if len(attr_texts) > 1:
                job.employment_type = attr_texts[1]

        job.posted = text_or_empty(
            card.locator("span[data-testid='myJobsStateDate'], span.date")
        )
        job.snippet = text_or_empty(
            card.locator("div.job-snippet, div[data-testid='belowJobSnippet']")
        )

        # 最低限タイトルが取れていれば採用
        if job.title:
            jobs.append(job)

    return jobs


def is_indeed_url(url: str) -> bool:
    """URLが Indeed のものかどうか判定する"""
    try:
        return "indeed.com" in urlparse(url).netloc.lower()
    except Exception:
        return False


def scrape_generic_page(page, url: str) -> List[Job]:
    """
    Indeed 以外の任意サイト向けの汎用スクレイピング。
    ページ内の「繰り返し要素」を自動検出してタイトル・URL・概要を抽出する。
    サイト専用のセレクタが無いぶん精度は Indeed 版より落ちる。
    """
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWTimeoutError:
        pass
    page.wait_for_timeout(1500)

    raw_items = page.evaluate(GENERIC_EXTRACT_JS)

    jobs: List[Job] = []
    for item in raw_items:
        job = Job()
        job.title = (item.get("title") or "").strip()
        job.url = item.get("url") or ""
        job.snippet = (item.get("snippet") or "").strip()
        if job.title:
            jobs.append(job)
    return jobs


def run_generic_scrape(
    url: str,
    headless: bool = True,
    progress_cb=None,
) -> List[Job]:
    """Indeed 以外のサイトを1ページだけスクレイピングする(汎用モード)。"""

    def notify(msg: str):
        if progress_cb:
            progress_cb(msg)
        else:
            print(msg)

    notify("Indeed以外のサイトと判定しました。汎用モードで指定ページのみ取得します。")
    notify(f"取得中: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="ja-JP",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        try:
            jobs = scrape_generic_page(page, url)
        except Exception as e:
            notify(f"エラー: ページ取得に失敗しました ({e})")
            jobs = []
        browser.close()

    notify(f"  -> {len(jobs)} 件取得(タイトル・URL・概要のみ)")
    return jobs


def run_scrape_any(
    base_url: str,
    pages: int = 1,
    headless: bool = True,
    min_delay: float = 3.0,
    max_delay: float = 6.0,
    progress_cb=None,
) -> List[Job]:
    """
    URLを見て Indeed なら専用ロジック(複数ページ・詳細項目対応)、
    それ以外なら汎用ロジック(1ページのみ・タイトル/URL/概要)を使う。
    """
    if is_indeed_url(base_url):
        return run_scrape(
            base_url,
            pages=pages,
            headless=headless,
            min_delay=min_delay,
            max_delay=max_delay,
            progress_cb=progress_cb,
        )
    return run_generic_scrape(base_url, headless=headless, progress_cb=progress_cb)


def save_csv(jobs: List[Job], output: str) -> None:
    fieldnames = [f.name for f in fields(Job)]
    with open(output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for job in jobs:
            writer.writerow(job.__dict__)


def run_scrape(
    base_url: str,
    pages: int = 1,
    headless: bool = True,
    min_delay: float = 3.0,
    max_delay: float = 6.0,
    progress_cb=None,
) -> List[Job]:
    """
    検索結果URLを起点に指定ページ数分スクレイピングして Job のリストを返す。
    progress_cb(str) が渡されていれば、各ステップの状況テキストを通知する。
    CLI からも Web アプリからも共通で使う中核ロジック。
    """

    def notify(msg: str):
        if progress_cb:
            progress_cb(msg)
        else:
            print(msg)

    all_jobs: List[Job] = []
    seen_ids = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="ja-JP",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        for page_num in range(pages):
            start = page_num * 10
            url = with_start_param(base_url, start) if page_num > 0 else base_url
            notify(f"[{page_num + 1}/{pages}] 取得中: {url}")

            try:
                jobs = scrape_page(page, url)
            except Exception as e:
                notify(f"エラー: ページ取得に失敗しました ({e})")
                break

            if not jobs:
                notify("これ以上の求人が見つからないか、ブロックされました。終了します。")
                break

            new_count = 0
            for job in jobs:
                key = job.job_id or job.url or job.title
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                all_jobs.append(job)
                new_count += 1

            notify(f"  -> {new_count} 件取得(累計 {len(all_jobs)} 件)")

            if page_num < pages - 1:
                delay = random.uniform(min_delay, max_delay)
                time.sleep(delay)

        browser.close()

    return all_jobs


def main():
    parser = argparse.ArgumentParser(description="Indeed 求人スクレイピングツール")
    parser.add_argument("--url", help="検索結果ページのURL(指定した場合 --keyword/--location は無視)")
    parser.add_argument("--keyword", default="", help="検索キーワード")
    parser.add_argument("--location", default="", help="勤務地")
    parser.add_argument("--pages", type=int, default=1, help="取得するページ数(1ページ=最大約15件)")
    parser.add_argument("--output", default="indeed_jobs.csv", help="出力CSVファイル名")
    parser.add_argument("--headless", action="store_true", default=True, help="ヘッドレスモードで実行(既定)")
    parser.add_argument("--show-browser", dest="headless", action="store_false", help="ブラウザを表示して実行(デバッグ用)")
    parser.add_argument("--min-delay", type=float, default=3.0, help="ページ間の最小待機秒数")
    parser.add_argument("--max-delay", type=float, default=6.0, help="ページ間の最大待機秒数")
    args = parser.parse_args()

    if not args.url and not (args.keyword or args.location):
        parser.error("--url か、--keyword/--location のいずれかを指定してください")

    base_url = args.url or build_search_url(args.keyword, args.location)

    all_jobs = run_scrape_any(
        base_url,
        pages=args.pages,
        headless=args.headless,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
    )

    save_csv(all_jobs, args.output)
    print(f"\n完了: {len(all_jobs)} 件を {args.output} に保存しました。")


if __name__ == "__main__":
    main()
