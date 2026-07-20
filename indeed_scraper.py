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
    const fullText = (el.innerText || '').trim().slice(0, 2000);
    let snippet = fullText;
    if (title) snippet = snippet.replace(title, '').trim();
    snippet = snippet.slice(0, 200);
    return { title, url, snippet, fullText };
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
    phone: str = ""
    email: str = ""


# ── 汎用モード向け: 会社名・住所・電話番号・メールアドレスの正規表現抽出 ──
# 一覧ページの本文テキストに直接これらの情報が含まれている場合のみ抽出できる。
# 「詳細ページに飛ばないと分からない」サイトでは空欄になる(既知の制約)。

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"0\d{1,4}[-‐−ー]\d{1,4}[-‐−ー]\d{3,4}")
POSTAL_RE = re.compile(r"〒?\s*\d{3}[-‐−ー]\d{4}")

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
PREFECTURE_RE = re.compile("(" + "|".join(PREFECTURES) + ")")

COMPANY_SUFFIXES = [
    "株式会社", "有限会社", "合同会社", "合資会社", "合名会社",
    "一般社団法人", "公益社団法人", "一般財団法人", "公益財団法人",
    "NPO法人", "特定非営利活動法人", "医療法人", "社会福祉法人",
]
COMPANY_RE = re.compile(
    r"[^\s、。,\n\r\t]{0,20}(?:" + "|".join(COMPANY_SUFFIXES) + r")[^\s、。,\n\r\t]{0,20}"
)


def extract_company_name(text: str) -> str:
    m = COMPANY_RE.search(text)
    return m.group(0).strip() if m else ""


def extract_address(text: str) -> str:
    m = POSTAL_RE.search(text)
    if m:
        window = text[m.start(): m.start() + 60].split("\n")[0]
        return window.strip()
    m = PREFECTURE_RE.search(text)
    if m:
        window = text[m.start(): m.start() + 50].split("\n")[0]
        return window.strip()
    return ""


def extract_business_fields(text: str) -> dict:
    email_m = EMAIL_RE.search(text)
    phone_m = PHONE_RE.search(text)
    return {
        "company": extract_company_name(text),
        "address": extract_address(text),
        "phone": phone_m.group(0) if phone_m else "",
        "email": email_m.group(0) if email_m else "",
    }


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


class BlockedError(Exception):
    """Indeedのブロック/確認ページ(bot検知)を検知した場合に送出する例外。
    一時的なセッション単位の確認である場合もあるため、呼び出し側で
    時間を置いてリトライする。"""
    pass


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
        raise BlockedError("ブロック/確認ページを検知しました")

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


def goto_with_retry(page, url: str, notify=None, timeout: int = 45000) -> None:
    """domcontentloaded待ちがタイムアウトした場合、条件を緩めて1回だけ再試行する
    共通ナビゲーション処理。"""

    def log(msg: str):
        if notify:
            notify(msg)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except PWTimeoutError:
        log("  ページの読み込みに時間がかかっています。条件を緩めて再試行します…")
        page.goto(url, wait_until="commit", timeout=timeout)
        page.wait_for_timeout(3000)

    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWTimeoutError:
        pass
    page.wait_for_timeout(1500)


def scrape_generic_page(page, url: str, notify=None) -> List[Job]:
    """
    Indeed 以外の任意サイト向けの汎用スクレイピング。
    ページ内の「繰り返し要素」を自動検出してタイトル・URL・概要を抽出する。
    サイト専用のセレクタが無いぶん精度は Indeed 版より落ちる。
    """
    goto_with_retry(page, url, notify=notify)

    raw_items = page.evaluate(GENERIC_EXTRACT_JS)

    jobs: List[Job] = []
    for item in raw_items:
        job = Job()
        job.title = (item.get("title") or "").strip()
        job.url = item.get("url") or ""
        job.snippet = (item.get("snippet") or "").strip()

        full_text = item.get("fullText") or ""
        fields_found = extract_business_fields(full_text)
        job.company = fields_found["company"]
        job.location = fields_found["address"]
        job.phone = fields_found["phone"]
        job.email = fields_found["email"]

        if job.title:
            jobs.append(job)
    return jobs


def enrich_with_detail_pages(
    page,
    jobs: List[Job],
    max_details: int = 20,
    min_delay: float = 1.5,
    max_delay: float = 3.0,
    notify=None,
) -> List[Job]:
    """
    一覧ページだけでは会社名・住所・電話番号・メールアドレスが取れない
    サイト向けに、各項目の詳細ページを実際に開いて本文から再抽出し、
    情報を補完する。詳細ページの方が情報が多いことを想定し、
    見つかった項目だけ上書きする(見つからなければ一覧ページの値を残す)。

    サイトへの負荷とサーバーのリソース・実行時間を考慮し、
    詳細ページを開く件数には上限(max_details)を設ける。
    """

    def log(msg: str):
        if notify:
            notify(msg)

    targets = [j for j in jobs if j.url][:max_details]
    if not targets:
        return jobs

    log(f"詳細ページを{len(targets)}件開いて会社名・住所・電話番号などを補完します…")

    for i, job in enumerate(targets, start=1):
        label = job.title[:30] if job.title else job.url
        log(f"  詳細取得中 ({i}/{len(targets)}): {label}")
        try:
            goto_with_retry(page, job.url, notify=notify)
            detail_text = page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            ) or ""
            fields_found = extract_business_fields(detail_text)
            if fields_found["company"]:
                job.company = fields_found["company"]
            if fields_found["address"]:
                job.location = fields_found["address"]
            if fields_found["phone"]:
                job.phone = fields_found["phone"]
            if fields_found["email"]:
                job.email = fields_found["email"]
        except Exception as e:
            log(f"    詳細ページの取得に失敗しました({e})。この項目はスキップします。")

        if i < len(targets):
            time.sleep(random.uniform(min_delay, max_delay))

    return jobs


def run_generic_scrape(
    url: str,
    headless: bool = True,
    progress_cb=None,
    max_details: int = 20,
) -> List[Job]:
    """
    Indeed 以外のサイトを汎用モードでスクレイピングする。
    まず一覧ページから項目(タイトル・URL)を検出し、続けて各項目の
    詳細ページを開いて会社名・住所・電話番号・メールアドレスを補完する。
    """

    def notify(msg: str):
        if progress_cb:
            progress_cb(msg)
        else:
            print(msg)

    notify("Indeed以外のサイトと判定しました。汎用モードで取得します。")
    notify(f"一覧ページを取得中: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="ja-JP",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        try:
            jobs = scrape_generic_page(page, url, notify=notify)
            notify(f"  -> 一覧から {len(jobs)} 件検出しました")
            if jobs:
                jobs = enrich_with_detail_pages(
                    page, jobs, max_details=max_details, notify=notify
                )
        except Exception as e:
            notify(f"エラー: ページ取得に失敗しました ({e})")
            notify(
                "  ヒント: サイトが重い、アクセスが多い時間帯、または"
                "サーバー側からのアクセス制限の可能性があります。"
                "時間を置くか、別のURLでお試しください。"
            )
            jobs = []
        browser.close()

    notify(f"完了: {len(jobs)} 件取得(タイトル・URL・概要・会社名・住所・電話・メール)")
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
    Indeed はサーバーのIPが継続的にブロックされているため、このツールでは
    非対応として扱う(試行自体を行わない)。それ以外のサイトのみ、
    汎用ロジック(一覧+詳細ページ巡回)で取得する。
    """

    def notify(msg: str):
        if progress_cb:
            progress_cb(msg)
        else:
            print(msg)

    if is_indeed_url(base_url):
        notify(
            "Indeedは現在サーバーからのアクセスがブロックされているため、"
            "このツールでは非対応としています。別の求人サイトのURLを"
            "指定するか、お使いのPC上でローカル実行してください。"
        )
        return []

    # 汎用モードでは「ページ数」入力を、詳細ページを何件まで開くかの
    # 上限として流用する(サイトへの負荷・実行時間を考慮し最大30件)
    max_details = max(1, min(pages, 30))
    return run_generic_scrape(
        base_url,
        headless=headless,
        progress_cb=progress_cb,
        max_details=max_details,
    )


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

        # ブロック検知時のリトライ設定(セッション単位の一時的な確認である
        # ケースもあるため、間隔を空けて数回だけ再試行する)
        block_retry_waits = [30, 90]  # 秒。この回数+1回まで試行する

        for page_num in range(pages):
            start = page_num * 10
            url = with_start_param(base_url, start) if page_num > 0 else base_url
            notify(f"[{page_num + 1}/{pages}] 取得中: {url}")

            jobs = None
            blocked_out = False
            attempt = 0
            while True:
                attempt += 1
                try:
                    jobs = scrape_page(page, url)
                    break
                except BlockedError:
                    if attempt - 1 < len(block_retry_waits):
                        wait_s = block_retry_waits[attempt - 1]
                        notify(
                            f"  ブロック/確認ページを検知しました。{wait_s}秒待って再試行します "
                            f"({attempt}/{len(block_retry_waits) + 1}回目)"
                        )
                        time.sleep(wait_s)
                        continue
                    notify("  再試行してもブロックが解除されませんでした。取得を中断します。")
                    blocked_out = True
                    break
                except Exception as e:
                    notify(f"エラー: ページ取得に失敗しました ({e})")
                    blocked_out = True
                    break

            if blocked_out:
                break

            if not jobs:
                notify("これ以上の求人が見つかりませんでした。終了します。")
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
