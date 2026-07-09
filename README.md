**[English](README.md)** | **[Tiếng Việt](docs/vi.md)**

![Cloudflare Gateway](https://github.com/luxysiv/Cloudflare-Gateway-Pihole/assets/46205571/b8b7b12b-2fd8-4978-8e3c-2472a4167acb)

# Cloudflare Gateway DNS Filter — Block + Allow

> Pihole-style DNS ad blocking using Cloudflare Gateway Zero Trust, with a dedicated Allow rule that takes precedence over blocking.

`For Devs, Ops, and everyone who hates Ads.`

---

# Schedule

[![Deploy to Cloudflare Workers](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/luxysiv/cloudflare-gateway-pihole-trigger)

| Variable | Description | Example |
| :--- | :--- | :--- |
| `GITHUB_TOKEN` | Your GitHub Personal Access Token (Need Workflow permission and no expiration) | `ghp_xxxxxxxxxxxx` |
| `GITHUB_USER` | Your GitHub username | `luxysiv` |
| `GITHUB_REPO` | The name of your repository | `Cloudflare-Gateway-DNS-Filter` |
| `WORKFLOW_ID` | The filename of your workflow | `main.yml` |

* Opt for a private repository when deploying.

* Once deployment is complete, you may remove the cloned repository.

---

## How it works

This script manages two sets of Cloudflare Gateway DNS rules in a single workflow run:

| Rule | Action | Precedence | Source |
| :--- | :--- | :--- | :--- |
| `[AdBlock-DNS-Filters] Block Ads` | block | 1000 | `adlist.ini` + `dynamic_blacklist.txt` |
| `[AdAllow-DNS-Filters] Allow` | allow | 999 *(higher priority)* | `whitelist.ini` + `dynamic_whitelist.txt` |

Because the Allow rule has a **lower precedence number** (999 < 1000), Cloudflare evaluates it first — so whitelisted domains are always resolved even if they appear in a blocklist. There is no need to subtract the whitelist from the block list in code.

The script also enforces Cloudflare Gateway's **free tier limit of 300 lists** across both rules combined. If your sources would require more than 300 lists, the script stops before making any changes.

---

## Setup

### 1. Fork this repository

### 2. Get your Cloudflare credentials

- **Account ID** — found in the URL after `https://dash.cloudflare.com/`:
  `https://dash.cloudflare.com/?to=/:account/workers`

- **API Token** — create at `https://dash.cloudflare.com/profile/api-tokens` with these 3 permissions:
  1. `Account › Zero Trust : Edit`
  2. `Account › Account Firewall Access Rules : Edit`
  3. `Account › Access: Apps and Policies : Edit`

### 3. Add Repository Secrets

Go to `https://github.com/<username>/Cloudflare-Gateway-DNS-Filter/settings/secrets/actions` and add:

| Secret | Value |
| :--- | :--- |
| `CF_API_TOKEN` | Your Cloudflare API Token |
| `CF_IDENTIFIER` | Your Cloudflare Account ID |

### 4. Configure your lists

**Block list** — edit [`lists/adlist.ini`](./lists/adlist.ini):
```ini
[Ad-Urls]
Adguard = https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt
```

**Allow list** — edit [`lists/whitelist.ini`](./lists/whitelist.ini):
```ini
[Allow-Urls]
MyAllow = https://example.com/my-whitelist.txt
```

Both files accept plain URLs (one per line) or the `[Section] key = url` INI format.

### 5. (Optional) Add lists via GitHub Actions Variables

Go to `https://github.com/<username>/Cloudflare-Gateway-DNS-Filter/settings/variables/actions` and add:

| Name | Value |
| :--- | :--- |
| `ADLIST_URLS` | Space-separated block list URLs |
| `WHITELIST_URLS` | Space-separated allow list URLs |
| `DYNAMIC_BLACKLIST` | Extra domains to block (one per line) |
| `DYNAMIC_WHITELIST` | Extra domains to allow (one per line) |

* You should add your ad list and whitelist to Action variables. If you update your fork, your custom list will not be lost.

---

## Supported list formats

```
# Plain URL
https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt
```
```ini
# INI format
[Ad-Urls]
Adguard = https://adguardteam.github.io/AdGuardSDNSFilter/Filters/filter.txt
```

Supported domain list styles: hosts files, AdBlock/uBlock syntax, plain domain lists.

> **Note:** Use **DNS filter lists**, not browser extension filter lists. Browser lists contain cosmetic rules that cause errors when used as DNS blocklists.

---

## How to set up using Termux?

To use this tool on the **GOAT** [Termux](https://github.com/termux/termux-app/releases/latest), follow the steps below. If you are already familiar with setting up Python and the basics, you can skip this section.

#### Method 1:

1. Open Termux and run the following commands one by one:

```sh
yes | pkg upgrade
yes | pkg install python-pip
yes | pkg install git
git clone https://github.com/<username>/Cloudflare-Gateway-DNS-Filter.git
```

2. Navigate to the cloned repository folder:

```sh
cd Cloudflare-Gateway-DNS-Filter
```

3. Edit the `.env` file (required):

```sh
nano .env
```

After editing, press `CTRL + X`, then `Y`, and `ENTER` to save the file.

4. Run the command to upload (update) your DNS list:

```sh
python -m src run
```

5. Run the command to delete your DNS list:

```sh
python -m src leave
```

#### Method 2:

1. Download the ZIP file of the repository from the 'Code' button on the GitHub page and select 'Download ZIP'.

2. Unzip the downloaded file.

3. Edit the values in `.env`, `lists/adlist.ini`, `lists/whitelist.ini` etc...

4. Open Termux and enter the following commands to set up Python and necessary tools:

```sh
yes | pkg upgrade
yes | pkg install python-pip
termux-setup-storage
```

5. Allow Termux to access storage.

6. Navigate to the folder containing the unzipped source code:

```sh
cd storage/downloads/Cloudflare-Gateway-DNS-Filter-main
```

7. Run the command to upload (update) your DNS list:

```sh
python -m src run
```

8. Run the command to delete your DNS list:

```sh
python -m src leave
```

If you encounter issues during setup, you can refer to [termux-change-repo](https://wiki.termux.com/wiki/Package_Management) for changing Termux repositories.

---

## Note

* The **limit** of `Cloudflare Gateway Zero Trust` free is **300 lists** (block + allow combined), with 1,000 domains per list — up to **300,000 domains** total. The script stops automatically if this limit would be exceeded.

* If you have uploaded lists using another script, you should delete them using the delete feature of that script or delete them manually.

* To delete all lists and rules created by this script, go to [main.yml](.github/workflows/main.yml) and change the command:

```yml
      - name: Cloudflare Gateway Zero Trust
        run: python -m src leave
```

---

## Credits

> Thanks a lot to [@nhubaotruong](https://github.com/nhubaotruong) for his contributions.

> Readme by [@minlaxz](https://github.com/minlaxz).

🥂🥂 Cheers! 🍻🍻
===
