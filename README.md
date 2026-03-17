# 📬 AI Email Agent

Reads your Gmail inbox, auto-replies to routine emails using Groq AI, and escalates important ones (invoices, contracts, decisions) to you in the terminal.

## Setup

1. `pip install groq google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client`
2. Add `credentials.json` from Google Cloud Console (Gmail API → OAuth Desktop App)
3. Add yourself as a test user in OAuth consent screen
4. Set your Groq API key: `export GROQ_API_KEY=gsk_...`

## Run

```bash
python main.py
```

## How it works

- Fetches **all unread emails** (no time limit)
- AI classifies each one: auto-reply or escalate
- Important emails (urgent, invoice, contract, etc.) pause and ask you
- All processed emails are marked as read
