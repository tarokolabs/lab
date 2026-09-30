# tkctl lab

tkctl 的講師用 plugin：一道指令在 Proxmox VE 上為每位學員建一台預裝 [tk8s](https://github.com/tarokolabs/tk8s) 的 VM，並在 Guacamole 建好該學員的帳號與 SSH／桌面連線；課程結束一道指令整批清掉。

## 安裝（講師機器）

```bash
uv tool install git+https://github.com/tarokolabs/lab
tkctl lab init                       # 互動式：問 PVE、儲存、bridge、Guacamole，然後全部建好
tkctl lab create template            # 從 Debian cloud image 做範本，做一次
```

需要 tk8s v2026.10.1 以上的 `tkctl`（有 plugin 分派）；PVE 8 以上、Guacamole 1.5 以上。

## 指令

```
tkctl lab init [pve|guacamole] [--manual] [-f FILE] [--pve-url … --storage … --guacamole-url …]
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

`pve.clone_storage`（例如 `"local-lvm"`）設了以後，學員 VM 會 **full clone** 到目標節點的本機儲存，磁碟 I/O 不再經過 NAS，開班每台約 1 到 2 分鐘；`node = "auto"` 會挑該儲存剩餘空間最多的節點、剩不到 32 GiB 的跳過。沒設就是 linked clone，11 秒一台，但所有 I/O 都走共用儲存。互動式 `init` 看到節點有 lvmthin／zfs 儲存時會問這題。範本磁碟本身只有 `template.disk`（預設 12G）大，clone 後才放大到 `vm.disk`，因為 full clone 與遷移搬的是整顆虛擬磁碟。

`$XDG_CONFIG_HOME/tkctl/lab.toml`（預設 `~/.config/tkctl/lab.toml`）；`tkctl lab init` 會產生含註解的範本。token 與密碼從環境變數或 `lab.env`（0600，`init` 寫的）讀，環境變數優先。PVE 的 TLS 一律驗證，自簽 CA 用 `pve.ca_file` 指定（`/etc/pve/pve-root-ca.pem`）。

## 建立你的環境（做一次）

`tkctl lab init` 依序做三件事，每一件都可以重跑（存在就補齊或略過）：

1. **設定檔** `$XDG_CONFIG_HOME/tkctl/lab.toml`（預設 `~/.config/tkctl/lab.toml`）。三種給值方式擇一：什麼都不給就進**互動式**（會先登入 PVE，只列出能放 images、snippets、import 的共用儲存與實際存在的 bridge、節點讓你選；Debian 映像的 sha512 自動抓）；**參數**（`--pve-url`、`--storage`、`--guacamole-url` 必填，其餘有預設）；或 **`-f FILE`** 用預先寫好的檔案。
2. **PVE**：建七個角色、使用者 `lab@pve`、pool、兩個 token、ACL，並把 PVE 的 CA 抓到 `pve-root-ca.pem` 給設定檔的 `ca_file`。需要管理員：互動式會隱藏輸入密碼，非互動式從 `TK_LAB_PVE_ADMIN_PASSWORD` 讀（帳號用 `--pve-admin`，預設 `root@pam`）。
3. **Guacamole**：建服務帳號 `tkctl-lab`、只給三個權限、有 TOTP 就幫它註冊。管理員密碼從隱藏輸入或 `TK_LAB_GUAC_ADMIN_PASSWORD` 讀（帳號 `--guacamole-admin`，預設 `guacadmin`）。

管理員密碼只在當下使用，不寫檔、不進 log。產生的 secret（兩個 PVE token、Guacamole 密碼與 TOTP secret）寫在 `$XDG_CONFIG_HOME/tkctl/lab.env`（0600）；同名環境變數有設會優先。

`tkctl lab init pve`／`init guacamole` 只做一邊。範本重建後 PVE 會清掉 `/vms/3900` 的 ACL，跑一次 `init pve` 就補回。不想把管理員密碼交給工具的話，`--manual` 會印出等價的 `pveum` 腳本與 Guacamole 的設定清單。

然後 `tkctl lab create template`（`pve.node` 是 `auto` 時要加 `--node`）。第一次跑只會把 cloud-init 的 snippet 寫到本機並印出 `scp` 指令：PVE 的 API 不收 snippets，得自己複製到儲存的 `snippets/` 一次；講師機如果掛了那個目錄，在設定檔填 `template.snippets_dir` 就會直接寫進去。再跑一次就會匯入映像、第一次開機裝 tk8s、XFCE、xrdp 並預拉 node image，關機後轉成範本，約 15 分鐘。建置腳本任一步失敗 VM 會留著不關機，逾時後指令會叫你去看主控台。

## 權限

PVE：專用使用者 `lab@pve` 與兩個 token。開班用的 token 只能 clone 範本、管理 `lab` 資源池內的 VM、在一個儲存上配置空間、使用一個 bridge；建範本用的 token 才能寫映像、引用 snippets（PVE 要求 `Datastore.Allocate`，這個權限也能刪該儲存上的 volume，所以範本建好後建議 `pveum user token remove lab@pve tkctl-build`，要重建再開）與讓節點抓網址。`/`、`/vms`、`/nodes` 上什麼都不給，所以碰不到 pool 外的 VM；`tkctl lab init` 直接建立，`--manual` 可印出等價的 `pveum` 指令。`node = "auto"` 會把 VM 輪流放到線上的節點（token 看不到節點記憶體，看得到時會優先放最空的）。需要 PVE 8 以上（`VM.GuestAgent.Audit`）、Guacamole 1.5 以上。Guacamole：專用帳號只有 `CREATE_USER`、`CREATE_CONNECTION` 與 `CREATE_CONNECTION_GROUP`；它建出來的物件由它自己管理，碰不到別人的。學員在自己的 VM 裡有 sudo，VM 之間是硬體隔離。

## 授權

GPL-2.0-or-later。
