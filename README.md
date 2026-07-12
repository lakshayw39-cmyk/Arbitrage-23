# Crypto Arb Paper Scanner — Streamlit Cloud deploy

## Files (all must be at the ROOT of the repo)

```
crypto/
├── streamlit_app.py    <- the app
├── requirements.txt    <- tells Streamlit Cloud to install ccxt
└── README.md
```

## Push (copy-paste, from inside your local repo folder)

```bash
git add streamlit_app.py requirements.txt README.md
git commit -m "Add Streamlit app with requirements and self-install fallback"
git push
```

Verify on github.com that `requirements.txt` is visible at the repo root,
spelled exactly `requirements.txt` (all lowercase, no double extension).

## Deploy / redeploy

1. share.streamlit.io → your app → **Manage app** → ⋮ menu → **Reboot app**
2. If it still misbehaves: **Delete app**, then **New app** → pick the repo,
   branch `main`, main file `streamlit_app.py`. A fresh deploy always
   re-reads requirements.

## Why it can no longer fail on `import ccxt`

`streamlit_app.py` now starts with a self-healing block: if `ccxt` is not
installed on the host, the app pip-installs it at startup and continues.
Even a deploy with a missing/misnamed requirements.txt will boot (first
load takes ~30s longer while it installs).

## Expected behavior once live

- Streamlit Cloud runs on US IPs → **Binance and Bybit are geo-blocked**
  there and will show zero quotes (see the Exchange health panel).
  Kraken / KuCoin / OKX / Gate respond fine.
- Run locally (`streamlit run streamlit_app.py`) from Canada to include
  Binance/Bybit.
- Paper trading only. Simulated fills assume zero latency — live results
  would be worse. No order-placement code exists in this app.
