# cy4500-cli

[English](README.md) | 日本語

**Infineon/Cypress CY4500-EPR USB-PDアナライザー**用のPython CLIです。PD通信と電圧・電流波形を取得し、Infineon EZ-PD Protocol Analyzer Utility形式へ保存できます。ハードウェアトリガー・CC終端の設定、EPR AVS遷移の解析にも対応します。変更履歴はGitで管理します。

## 準備

- Python 3.10以上。ローカルのソフトウェア検証はPython 3.12を使用。
- CY4500-EPR（USB VID:PID `04B4:FDEF`、interface 0）。WindowsのWinUSBドライバーを使用。
- `libusb1`（Pythonでは `usb1` としてimport）。旧CY4500や他OSは未検証。
- **取得中はEZ-PD Protocol Analyzer Utilityを閉じてください。**

```powershell
git clone https://github.com/inuchanbt/cy4500-cli.git
cd cy4500-cli
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe cy4500_cli.py usb-info
.\.venv\Scripts\python.exe cy4500_cli.py version
```

`version` は機器ファームウェアのバージョンを読みます。`cy4500_cli.py`、`ezpd_protocol.py`、`utility_export.py` を同じ場所に置いてください。以下の `python` は仮想環境のPythonへ置き換えられます。helpは英語です。

## 取得

```powershell
# 既定でCtrl+Cまで取得し、日時付き保存名を自動生成
python cy4500_cli.py capture
# 10秒取得して保存名を指定
python cy4500_cli.py capture --seconds 10 --out-prefix captures/session01
# 停止・ログ保存後にAVS遷移を解析
python cy4500_cli.py capture --analyze-transitions
# 全GoodCRCをコンソールにも表示
python cy4500_cli.py capture --show-goodcrc
```

| 設定 | 既定 | 変更 |
| --- | --- | --- |
| 取得時間 | Ctrl+Cまで | `--seconds N` |
| 保存名 | `captures/cy4500_YYYYMMDD_HHMMSS_ffffff`（ローカル日時） | `--out-prefix PREFIX` |
| 保存 | CSV・ccgx3・PD生データ/hex/JSONL・scope CSV・summary | `--no-ccgx3` |
| 波形 | 有効 | `--no-scope` |
| 正常GoodCRC表示 | 非表示、全件保存 | `--show-goodcrc` |
| 状態表示 | 1秒間隔 | `--status-interval N` / `--quiet` |
| AVS解析 | 無効 | `--analyze-transitions` |
| EP83 USB生転送の追加保存 | 無効 | `--scope-raw` |
| 上書き | 無効 | `--force` |

`--until-ctrl-c`、`--scope`、`--ccgx3`、`--hide-goodcrc` は既定動作を明示できます。**GoodCRCの非表示はコンソールだけで、ログや件数に影響しません。** エラー付きGoodCRCはquiet指定時を除き表示します。保存名は拡張子を含まないprefixです。既存出力との衝突はUSB取得前に拒否し、上書きには `--force` が必要です。解析やscope-rawと `--no-scope` の併用はエラーです。

Ctrl+CでUSB取得を停止し、出力を確定します。通常のライブ表示と定期状態はstdout、エラーはstderrです。quietでも終了時の結果や解析出力まで全部抑制するわけではありません。

## 保存形式

| 拡張子 | 内容 |
| --- | --- |
| `.csv` | Utility互換のPDテーブル（15列） |
| `.ccgx3` | JavaシリアライズされたPD/波形を格納したGUI用ZIP。生成にJava不要 |
| `.records.bin` | 固定64バイトPDレコードの連結 |
| `.records.jsonl` / `.records.hex.txt` | デコード結果・生データのhex |
| `.xfers.bin` | 長さを4バイトlittle-endianで付けたEP81転送 |
| `.summary.txt` | セッション解析と時計情報 |
| `.scope.csv` | EP83のVBUS/IBUS/CC1/CC2波形（約1 kS/s） |
| `.scope.xfers.bin` | `--scope-raw` 指定時だけ追加するEP83転送 |
| `.transitions.csv/.txt` | AVS遷移の詳細 |
| `.transition_summary.csv/.txt` | 遷移ごとの一覧 |

Utility CSVの **`Vbus(V)` 列は整数mV** です。SNoはVBUSイベントを含めた通し番号。生の機器値はrecordsに保持します。End Timeは実際の終了時刻です。PD CSVはUTF-8、scope/解析CSVはUTF-8 BOM付き。GUI注釈・トリガー・表示設定は再現しません。ccgx3には既定で取得波形も入り、`--no-scope` なら波形リストが空になります。

**`VBUS_UP` / `VBUS_DN` はCY機器のイベントとして保存します。** TI CLIの任意の推定イベントとは検出方法・精度が異なります。hardware idle-errorなども正常パケットへ書き換えず残します。

少し逆行するPD開始時刻を32bit時計周回と誤認しないよう、最も近いepochを選択します。本物の周回と遅れて到着した周回前レコードにも対応し、実際の逆行・負Deltaは保持します。隣接観測間隔が2^31 µs（約35.8分）未満という前提があります。

## 保存後の変換・解析

USBを使わずに、CLIの `.records.bin` をCSV/ccgx3へ変換できます。`.xfers.bin` やGUIのccgx3は入力できず、この変換では波形を追加しません。

```powershell
python cy4500_cli.py export-gui --records captures/session01.records.bin --out-prefix captures/converted01
python cy4500_cli.py analyze-sync --pd-csv captures/session01.csv --scope-csv captures/session01.scope.csv --out-prefix captures/analysis01
```

解析には同じ取得セッションのPD/波形CSVを使います。旧CLI/Utility CSVの両形式を受け付け、単位とペイロード表現を解釈します。`--gui-csv` は互換用の何もしないオプションで、CSVを追加しません。

AVS解析は要求、Accept/PS_RDY、波形の動き始め・到達・整定・スルーレートを照合します。要求電圧の±1%に対するAbsと、実測安定電圧の±0.5%に対するObs.Setを分けて表示します。負荷変化で後の安定区間が選ばれる場合があります。USB-PD規格の適合判定ではありません。

方向は持続的な実測電圧変化から判断し、逆向きのオーバーシュート回復をランプ速度として扱いません。測定点不足や未解像の値はflagsと空欄で残します。機器時刻は保持し、ホストオフセットや機器間校正は加えません。**EP81のPDとEP83の波形が共通時計であることは未確立**で、異なるストリーム間の時刻比較にはこの制約があります。詳しいしきい値は `capture --help` / `analyze-sync --help` を参照してください。

同時測定のAVS往復では正常PD 1,622件とAVS67要求の全Accept/PS_RDYがTIと一致しました。波形はCY 163,035点、TI 9,016点で、同じPCの解析本体は約23.54秒/0.67秒。密度と安定区間の反復探索が速度差に寄与します。データの間引きは行いません。[TI側の比較レポート](https://github.com/inuchanbt/TI-PD-ANALYZER-CLI/tree/main/docs/reports)に詳細を記録しています。元測定ファイルは公開リポジトリに含めません。

## 波形CSVの集計・グラフ

`analyze_sweep_csv.py` はASD-PD31の解析スクリプトと同様に、正規化CSV、集計CSV、PNG、日本語・英語レポートを作成します。USB接続は不要です。CY4500の `.scope.csv` と `live-status --csv` の出力を受け付けます。PDの `.csv` を渡すと、同名の `.scope.csv` を読みます。

```powershell
# PNG用の追加ライブラリ（CSV・テキストだけなら不要）
.\.venv\Scripts\python.exe -m pip install -r requirements-analysis.txt
# 既定は1秒ごとの平均・最小・最大・標準偏差・電圧p-p
.\.venv\Scripts\python.exe analyze_sweep_csv.py captures/session01.scope.csv
# AVS契約ごとに集計。PS_RDY後の先頭50msを除外
.\.venv\Scripts\python.exe analyze_sweep_csv.py captures/session01.csv --group-by request --settle-seconds 0.05
# 固定24V測定、最初の5秒を除外し、0.5秒ごとに集計
.\.venv\Scripts\python.exe analyze_sweep_csv.py captures/session01.scope.csv --target-voltage 24 --start 5 --window-seconds 0.5
# 追加ライブラリなしでCSV・英語レポートを作成
.\.venv\Scripts\python.exe analyze_sweep_csv.py captures/session01.scope.csv --no-plots
# 日本語レポート（英語版と保存先を分ける例）
.\.venv\Scripts\python.exe analyze_sweep_csv.py captures/session01.scope.csv --report-lang ja --out captures/session01_ja
```

既定の出力prefixは入力波形ファイルのstemに `_analysis` を付けたものです。`--out PREFIX` で変更できます。既存出力の上書きには `--force` が必要です。`--end` は先頭サンプルからの経過秒（指定時刻を除外）、`--discard-first N` は各区間の先頭Nサンプル除外、`--min-samples N` は集計に必要な最低点数（既定2）です。`--settle-seconds` による時間除外の後に `--discard-first` を適用します。時間窓の境界は入力先頭が基準で、`--start` では移動しません。

| 出力suffix | 内容 |
| --- | --- |
| `_normalized.csv` | 解析に使った全サンプル。電圧V・電流A・電力W・時刻µs/経過秒 |
| `_summary.csv` | 区間ごとのサンプル数、平均・最小・最大・母標準偏差、電圧p-p、既知の設定電圧に対する誤差 |
| `_human_report.txt` | 測定範囲・サンプル間隔・実測値・電圧変動の説明（既定は英語、`--report-lang ja` で日本語） |
| `_voltage_actual.png` / `_current.png` / `_power.png` | 区間平均の時間推移 |
| `_voltage_pp.png` / `_voltage_pp_vs_voltage.png` | 区間内電圧変動の時間推移 / 実測電圧との関係 |
| `_voltage_error.png` | 設定電圧が分かる場合の誤差 |

**電圧p-pは区間内の最大値−最小値で、ASD-PD31のripple値とは測定帯域・方法が異なります。** 遷移・ドリフトも含むため、安定区間を選んで評価してください。不明な電流・設定電圧は空欄とし、負の実測電流はそのまま保持します。集計値はPNGの各点を代表するもので、生波形全点のプロットではありません。

`--group-by request` は同セッションの正常なSOP AVS EPR_REQUEST、Accept、PS_RDYを照合します。連続した同一契約はまとめ、別電圧を経て戻った同じ電圧は別区間として保持します。固定/PPS契約・成立前の区間は除外します。PD CSVが別名の場合は `--pd-csv PATH` で指定できます。`request_current_a` は要求上限電流で、設定負荷電流ではありません。EP81/EP83の共通時計は未確立で、補正は行いません。`live-status` のホスト時刻は要求区間集計には使えません。

レポートは既定で英語です。`--report-lang ja` で日本語の `_human_report.txt`、`--report-lang both` で `_human_report_ja.txt` と `_human_report_en.txt` を作成します。`--no-report` は両言語とも出力しません。

## その他のコマンド

| コマンド | 用途 |
| --- | --- |
| `usb-info` / `version` | USB情報 / 機器FWバージョン |
| `live-status` / `volt-amp` | 電圧・電流をポーリング（`--count` 省略でCtrl+Cまで） |
| `scope` | EP81を排出しながらEP83波形だけ保存 |
| `trigger` | トリガーの設定・確認・クリア・arm |
| `trigger-arm` / `trigger-clear` | 既存条件のarm / 明示クリア |
| `trigger-epr-request` | EPR_REQUEST条件を設定（armしない） |
| `termination` / `termination-clear` | CC1/CC2終端の設定 / 両方NONE |

トリガー条件はAND結合。設定だけでは測定エンジンは起動せず、`--arm` はCtrl+Cまで、`--arm-seconds N` は時間指定で起動します。arm中はEP81/EP83を排出し、キャプチャ出力は作りません。`--clear-on-exit` を付けなければ停止時に条件を明示クリアしません。

CC終端は両側を指定する必要があり、未指定側を保持するreadbackはありません。RP/RA/RD/NONEを選べ、CC回路を物理的に変更します。USB書き込み成功は設定の意味的な確認やreadbackを保証しません。パケットを確認する `--dry-run` が使えます。具体例・トリガー構成の詳細は[英語版README](README.md)を参照してください。

## フォルダー・検証

CLI・プロトコル・エクスポートのPythonモジュールはルート、人工データによるテストと任意のJava互換確認は `tests/`、開発手順は `docs/` に配置しています。`captures/`、`archive/`、`notes/`、`.venv/` はGit管理対象外です。生成ログはcapturesへ保存してください。

```powershell
python -m unittest discover -s tests -v
```

[開発・検証手順（英語）](docs/DEVELOPMENT.md)。GitHub ActionsもWindows/Python 3.10・3.12で人工データの回帰テストを実行します。テストに機器・個人測定ログ・Utilityの独自ランタイムは不要です。任意のJava互換チェックにはJDKとインストール済みUtilityのクラスが必要です。

## 困ったとき

- usb1がない: 起動時と同じPythonでrequirementsをインストール。
- デバイス未検出・アクセス拒否: USB接続、`04B4:FDEF`のドライバー、GUIによる占有を確認。
- AVSが見つからない: AVS EPR_REQUESTの前から取得し、同セッションのPD/波形を使用。

[MIT](LICENSE)、Copyright (c) 2026 inuchanbt。Infineonの公式ソフトウェアではありません。
