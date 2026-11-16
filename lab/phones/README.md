# Phone captive-portal lab (Q3)

Do real phones (iOS CNA sheet, Android CaptivePortalLogin) keep a cookie our portal sets,
and when do they change MAC? It also exercises the openpiso flow: a cookie that returns
from a new MAC makes the portal rebind (remove old active entry, then `active login`).

Hardware: a Wi-Fi AP in bridge mode (open SSID `openpiso-lab`, DHCP off) plugged into a
USB-Ethernet adapter that the wizard enslaves into the `virbr-hs` bridge of the CHR lab
(see `lab/chr/`). The host serves `portal.py` on `192.168.122.1:8080`.

Run (needs sudo and real phones):

    ./lab/phones/wizard.sh plain   # iPhone + Android, no adb
    ./lab/phones/wizard.sh adb     # Android only, with adb logcat/screenshot capture
    ./lab/phones/wizard.sh recheck # iPhone MAC change + logged-in forget/reboot; Android forget/reboot after the session ended

Results go to `docs/research/results/<date>-phones-<mode>/` (`results.md`, `answers.tsv`,
`portal.jsonl`, `steps/<phone>-<step>/`). Wizard values live in `lab/phones/.env` (gitignored).
