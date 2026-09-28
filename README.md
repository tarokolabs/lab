# tkctl lab

tkctl 的講師用 plugin：一道指令在 Proxmox VE 上為每位學員建一台預裝 [tk8s](https://github.com/tarokolabs/tk8s) 的 VM，並在 Guacamole 建好該學員的帳號與 SSH／桌面連線；課程結束一道指令整批清掉。

## 安裝（講師機器）

```bash
uv tool install git+https://github.com/tarokolabs/lab
tkctl lab init            # 寫出 ~/.config/tkctl/lab.toml 並印出 PVE 端要跑的 pveum 指令
export TK_LAB_PVE_TOKEN=...          # PVE API token 的 secret
export TK_LAB_GUAC_PASSWORD=...      # Guacamole 專用帳號的密碼
tkctl lab create template            # 從 Debian cloud image 做範本，做一次
```

需要 tk8s v2026.10.0 以上的 `tkctl`（有 plugin 分派）。

## 指令

```
tkctl lab init
tkctl lab create template [--k8s 1.37.0] [--node NAME]
tkctl lab create class <班名> --students N [--expires 2026-10-20] [--node NAME|auto] [--cores N] [--memory MiB]
tkctl lab create class -f k8s-101.toml
tkctl lab get classes
tkctl lab describe class <班名> [--roster] [-o toml]
tkctl lab delete class <班名> [--yes]
tkctl lab delete class --expired [--yes]
```

`create class` 的旗標與 `-f` 互斥。定義檔是 TOML，`students` 給名字、`count` 給人數（二擇一）；`[vm]` 蓋過設定檔的預設，`[[students_override]]` 再蓋單一學員：

```toml
name = "k8s-101"
expires = 2026-10-20
students = ["alice", "bob", "carol"]

[vm]
memory = 16384

[[students_override]]
name = "carol"
memory = 32768
```

`describe class <班名> -o toml` 會把現有班級倒回這種格式，改個班名就能再開一班。

每位學員一台 `lab-<班名>-<學員>` 的 VM（tag `lab;class-<班名>;expires-<日期>`），Guacamole 一個帳號（有名字的學員用名字，編號的用 `<班名>-<編號>`）與兩條連線：`<班名>-<學員> SSH` 和 `<班名>-<學員> Desktop`。名冊（VMID、IP、Guacamole 帳密、VM 的 `student` 密碼、失敗原因）寫在 `$XDG_STATE_HOME/tkctl/lab/<班名>.csv`（預設 `~/.local/state/…`），權限 0600，`describe class --roster` 會印出來。

`--parallel N`（預設 5）控制同時建幾台。某位學員失敗時其他人照建，名冊的 `error` 欄寫原因，結束碼 1；把原因排除後**再跑一次同一道 `create class`** 會續建：已完成的學員不動，失敗的從中斷處接下去（clone 失敗的重 clone、沒拿到 IP 的再等一次、Guacamole 沒建好的補建）。班級已有 VM 但名冊不在時會拒絕，避免建出重複的 VM。

結束碼：0 成功；1 有學員失敗、刪除有殘留或 PVE／Guacamole 回錯（一行訊息，不是 traceback）；2 用法或設定錯誤。`TK_ASSUME_YES=1` 等同 `--yes`。

## 設定檔

`$XDG_CONFIG_HOME/tkctl/lab.toml`（預設 `~/.config/tkctl/lab.toml`）；`tkctl lab init` 會產生含註解的範本。token 與密碼只從環境變數讀，不寫檔。PVE 的 TLS 一律驗證，自簽 CA 用 `pve.ca_file` 指定（`/etc/pve/pve-root-ca.pem`）。

## 建立你的 PVE 環境（做一次）

1. **設定檔**：`tkctl lab init` 寫出 `lab.toml`，填 PVE 網址、`pve.storage`（要能放 qcow2、snippets 與 import 的共用儲存，linked clone 不能用純 LVM）、bridge、VMID 範圍、Guacamole 網址。
2. **PVE 角色與 token**：再跑一次 `tkctl lab init`，把印出的 `pveum` 指令以管理員身分在任一 PVE 節點執行。會建兩個 token：`tkctl`（開班、刪班用）與 `tkctl-build`（只有 `create template` 用，多了寫映像與抓網址的權限）；`pveum user token add` 印出的 secret 只出現一次，分別放進 `TK_LAB_PVE_TOKEN` 與 `TK_LAB_PVE_BUILD_TOKEN`。把 `/etc/pve/pve-root-ca.pem` 複製到講師機器並在 `pve.ca_file` 指定。
3. **Guacamole 帳號**：在 Guacamole 建使用者 `tkctl-lab`，系統權限只勾 Create new users 與 Create new connections；密碼放進 `TK_LAB_GUAC_PASSWORD`。
4. **範本**：到 `cloud.debian.org` 的 `SHA512SUMS` 抄 `debian-13-genericcloud-amd64.qcow2` 的 sha512 填進 `template.image_sha512`，然後 `tkctl lab create template`（`pve.node` 是 `auto` 時要加 `--node`）。第一次跑只會把 cloud-init 的 snippet 寫到本機並印出 `scp` 指令：PVE 的 API 不收 snippets，得自己複製到儲存的 `snippets/` 一次；講師機如果掛了那個目錄，在設定檔填 `template.snippets_dir` 就會直接寫進去。再跑一次就會匯入映像、第一次開機裝 tk8s、XFCE、xrdp 並預拉 node image，關機後轉成範本，約 15 分鐘。建置腳本任一步失敗 VM 會留著不關機，逾時後指令會叫你去看主控台。

## 權限

PVE：專用使用者 `lab@pve` 與兩個 token。開班用的 token 只能 clone 範本、管理 `lab` 資源池內的 VM、在一個儲存上配置空間、使用一個 bridge；建範本用的 token 才能寫映像與讓節點抓網址。`/`、`/vms`、`/nodes` 上什麼都不給，所以碰不到 pool 外的 VM；`tkctl lab init` 印出完整的 `pveum` 指令。`node = "auto"` 會把 VM 輪流放到線上的節點（token 看不到節點記憶體，看得到時會優先放最空的）。需要 PVE 8 以上（`VM.GuestAgent.Audit`）、Guacamole 1.5 以上。Guacamole：專用帳號只有 `CREATE_USER` 與 `CREATE_CONNECTION`。學員在自己的 VM 裡有 sudo，VM 之間是硬體隔離。

## 授權

GPL-2.0-or-later。
