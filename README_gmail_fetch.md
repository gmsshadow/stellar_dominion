# Stellar Dominion Gmail Orders Fetch (Label-Based)

This is a **standalone** script for fetching player orders from Gmail via the **Gmail API**.

It implements reliable **exactly-once** processing by using Gmail labels:

- **Orders label** (default: `sd-orders`) = messages waiting to be processed
- **Processed label** (default: `sd-processed`) = messages already processed

When a message is processed, the script:
1. Extracts orders text (attachment preferred; body fallback)
2. Writes it to `./fetched_orders/`
3. Removes the `sd-orders` label
4. Adds the `sd-processed` label
5. Removes `UNREAD`

That way, even if you later mark a message as unread manually, it won't be reprocessed unless you put it back into the orders label.

---

## Requirements

- Python 3.10+
- A Google account
- Gmail API enabled in Google Cloud Console

---

## Install dependencies

Recommended: use a virtual environment.

### Windows (PowerShell)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

---

## Google Cloud setup (one-time)

1. Go to Google Cloud Console and create/select a project
2. Enable **Gmail API**
3. Create OAuth credentials: **Desktop App**
4. Download the client secrets JSON
5. Save it as `credentials.json` next to the script

On first run, your browser will open for consent and the script will create `token.json`.

---

## Basic usage

### 1) Put incoming orders emails into a label

In Gmail, create a label named **`sd-orders`** (or whatever you choose) and apply it to incoming orders emails.

You can do this manually, or with a Gmail filter:
- If subject contains `SD Orders` → Apply label `sd-orders`

The script will also auto-create the label if it doesn't exist, but you still need to label the messages for them to match the default query.

### 2) Run the fetch

```powershell
python stellar_dominion_gmail_fetch_labeled.py --credentials credentials.json
```

Default Gmail query is:

```
label:"sd-orders" is:unread
```

Output goes to:

```
./fetched_orders/
```

---

## Options

### Change label names

```powershell
python stellar_dominion_gmail_fetch_labeled.py --credentials credentials.json --orders-label "orders" --processed-label "processed"
```

### Override Gmail query

```powershell
python stellar_dominion_gmail_fetch_labeled.py --credentials credentials.json --query 'label:"sd-orders" newer_than:7d'
```

### Dry run (don’t change Gmail state)

```powershell
python stellar_dominion_gmail_fetch_labeled.py --credentials credentials.json --dry-run
```

---

## Next integration step

Once you're happy this works, we can integrate it into `pbem.py` so that:
- fetched orders go directly into your usual incoming/turn folders
- orders are validated immediately (email -> account -> ship ownership)
- receipts/rejections can be emailed back automatically
- processing is recorded in the game DB
