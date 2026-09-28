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
tkctl lab create template [--k8s 1.37.0]
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

每位學員一台 `lab-<班名>-<學員>` 的 VM（tag `lab;class-<班名>;expires-<日期>`），Guacamole 一個帳號（有名字的學員用名字，編號的用 `<班名>-<編號>`）與兩條連線：`<班名>-<學員> SSH` 和 `<班名>-<學員> Desktop`。名冊（VMID、IP、Guacamole 帳密、失敗原因）寫在 `$XDG_STATE_HOME/tkctl/lab/<班名>.csv`（預設 `~/.local/state/…`），權限 0600，`describe class --roster` 會印出來。

結束碼：0 成功；1 有學員失敗或刪除有殘留（名冊的 ERROR 欄寫原因）；2 用法或設定錯誤。`TK_ASSUME_YES=1` 等同 `--yes`。

## 設定檔

`$XDG_CONFIG_HOME/tkctl/lab.toml`（預設 `~/.config/tkctl/lab.toml`）；`tkctl lab init` 會產生含註解的範本。token 與密碼只從環境變數讀，不寫檔。PVE 的 TLS 一律驗證，自簽 CA 用 `pve.ca_file` 指定（`/etc/pve/pve-root-ca.pem`）。

## 權限

PVE：專用使用者 `lab@pve` 與 token，自訂角色只掛在 `lab` 資源池、範本 VM、一個儲存與 bridge 上（另有唯讀的 `Sys.Audit` 在 `/nodes`，讓 `node = "auto"` 看得到節點記憶體），碰不到 pool 外的 VM；`tkctl lab init` 印出完整的 `pveum` 指令。Guacamole：專用帳號只有 `CREATE_USER` 與 `CREATE_CONNECTION`。學員在自己的 VM 裡有 sudo，VM 之間是硬體隔離。

## 授權

GPL-2.0-or-later。
