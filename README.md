# Indeed 求人スクレイピングツール

## 注意事項(必読)
Indeed の利用規約は自動収集(スクレイピング)を禁止しています。実行は自己責任で、
以下を守ってください。

- アクセス頻度を抑える(本ツールはページ間に3〜6秒のランダム待機を入れています)
- 取得データを商用利用・再配布しない
- ブロックや CAPTCHA が出たら中断する(本ツールは自動検知して停止します)
- 個人情報(応募者情報など)は取得対象にしない

## 使い方 D: Web上に公開して同僚と共有する(要デプロイ作業)
社内で複数人がURLだけで使えるようにするには、クラウドにデプロイします。
無料枠もある [Render](https://render.com) を使う手順の例です。

1. このフォルダ一式を GitHub リポジトリにアップロードする
   (GitHubアカウントが無い場合は https://github.com で作成)
2. https://render.com にアクセスしてアカウント作成(GitHubログイン可)
3. 「New +」→「Web Service」→ 1 のリポジトリを選択
4. Environment(環境)は `Docker` を選択(このフォルダの `Dockerfile` が自動で使われます)
5. Plan は Free だとメモリ不足で落ちることがあるため、`Starter`(有料・月$7〜)を推奨
6. 「Create Web Service」でデプロイ開始(数分)
7. 発行された `https://xxxxx.onrender.com` のようなURLを同僚に共有すれば、
   ブラウザだけで誰でもアクセスできます

**共有時の注意:**
- 現状パスワード保護はありません。URLを知っている人なら誰でもアクセスできます
- 同時に1件しかスクレイピングを実行しない制御を入れていますが、
  複数人が同時に使うとその分 Indeed への負荷・ブロックリスクが上がります
- 会社のサーバーIPで大量アクセスすると、社内の他の業務(求人閲覧など)にも
  影響が出る可能性があるため、頻度・件数は控えめにしてください

## 使い方 A: ダブルクリックだけで使う(誰でも使える版・おすすめ、自分専用)
1. Python が入っていない場合は https://www.python.org/ から先にインストール
   (Windowsは「Add python.exe to PATH」に必ずチェック)
2. フォルダの中の起動スクリプトをダブルクリック
   - Mac: `start_mac.command`
   - Windows: `start_windows.bat`
3. 初回のみ自動でセットアップ(数分)が走った後、ブラウザが自動で開きます
4. 次回以降はダブルクリックするだけですぐ起動します

画面上でキーワード/勤務地(またはURL)とページ数を入力して
「取得開始」を押すだけです。完了すると CSV のダウンロードボタンが表示されます。

※Macで「開発元が未確認のため開けません」と出た場合は、`start_mac.command` を
右クリック→「開く」を選ぶと実行できます。

## 使い方 B: 手動でセットアップして使う
```bash
pip install -r requirements.txt
python -m playwright install chromium
python app.py
```
起動したら `http://127.0.0.1:5000` をブラウザで開いてください
(自動で開かない場合のみ)。

## 使い方 C: コマンドラインで使う(上級者向け)
```bash
# 検索結果ページのURLを直接指定
python indeed_scraper.py --url "https://jp.indeed.com/l-兵庫県-神戸市-求人.html" --pages 3

# キーワード・勤務地から検索
python indeed_scraper.py --keyword "エンジニア" --location "神戸市" --pages 2

# デバッグ用にブラウザを表示
python indeed_scraper.py --url "..." --show-browser

# 出力ファイル名を指定
python indeed_scraper.py --url "..." --output jobs.csv
```

## 取得項目(CSV列)
title(職種名/タイトル) / company(会社名) / location(勤務地・住所) / salary(給与) /
employment_type(雇用形態) / posted(掲載日) / snippet(概要) / job_id /
url(詳細URL) / phone(電話番号) / email(メールアドレス)

Indeed以外の汎用モードでは company・location(住所)・phone・email は
ページ本文を正規表現で解析して自動検出しています(株式会社等の法人格、
郵便番号・都道府県名、電話番号形式、メールアドレス形式で判定)。
一覧ページの本文に直接書かれていない情報(詳細ページにしか無い情報)は
検出できず、空欄になります。

## 既知の制約
- Indeed はページのHTML構造を頻繁に変更するため、時間が経つとセレクタが
  効かなくなる可能性があります。その場合は `indeed_scraper.py` 内の
  CSSセレクタ(`data-testid` 等)を最新のページ構造に合わせて更新してください。
- ブロック検知は簡易的なキーワード判定です。過信せず、実行結果(CSVの件数)を
  都度確認してください。
- 汎用モードの会社名・住所・電話番号・メールアドレス抽出は正規表現による
  ヒューリスティックです。サイトの書き方によっては誤検出・未検出があります。
