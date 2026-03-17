import os, base64, json, re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from groq import Groq

# ─── CONFIG ──────────────────────────────────────────────────────────────────

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "your-groq-api-key")  # set this in your environment for security
GROQ_MODEL     = "llama-3.3-70b-versatile"           # fast & smart
GMAIL_SCOPES   = ["https://www.googleapis.com/auth/gmail.modify"]
CREDS_FILE     = "credentials.json"          # downloaded from Google Cloud
TOKEN_FILE     = "token.json"                # auto-created on first run
YOUR_NAME      = os.environ.get("YOUR_NAME", "Alex")

# Keywords that force human review instead of auto-reply
IMPORTANT_KEYWORDS = [
    "urgent", "asap", "deadline", "invoice", "payment", "contract",
    "legal", "lawsuit", "emergency", "critical", "decision", "approve",
    "sign", "meeting request", "interview", "offer", "salary",
]

# ─── GMAIL AUTH ───────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)

# ─── EMAIL HELPERS ────────────────────────────────────────────────────────────

def fetch_unread_emails(service):
    """Fetch every single unread email in the inbox using pagination."""
    query = "is:unread -from:me"
    messages = []
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        batch = result.get("messages", [])
        messages.extend(batch)
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    print(f"  → {len(messages)} unread message(s) found, fetching details…")

    emails = []
    for msg in messages:
        full = service.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        body = extract_body(full["payload"])
        emails.append({
            "id":      msg["id"],
            "from":    headers.get("From", "Unknown"),
            "to":      headers.get("To", ""),
            "subject": headers.get("Subject", "(no subject)"),
            "date":    headers.get("Date", ""),
            "body":    body[:3000],   # cap to avoid huge prompts
        })
    return emails


def extract_body(payload):
    """Recursively pull plain text from the Gmail payload."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore") if data else ""
    for part in payload.get("parts", []):
        text = extract_body(part)
        if text:
            return text
    return ""


def send_reply(service, original, reply_text):
    msg = MIMEMultipart()
    msg["To"]      = original["from"]
    msg["Subject"] = "Re: " + original["subject"]
    msg["In-Reply-To"] = original["id"]
    msg.attach(MIMEText(reply_text, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(
        userId="me",
        body={"raw": raw, "threadId": original["id"]}
    ).execute()
    print(f"  ✉  Reply sent to {original['from']}")


def mark_as_read(service, msg_id):
    service.users().messages().modify(
        userId="me", id=msg_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()

# ─── GROQ AI ──────────────────────────────────────────────────────────────────

groq_client = Groq(api_key=GROQ_API_KEY)


def classify_and_draft(email: dict) -> dict:
    """
    Ask Groq to:
      1. Decide if the email needs human attention (needs_human: true/false)
      2. Draft a reply if auto-replying
      3. Summarize why if escalating
    Returns a dict with keys: needs_human, reason, draft_reply
    """
    body_snippet = email["body"][:1500]
    flag_hint = any(kw in (email["subject"] + body_snippet).lower()
                    for kw in IMPORTANT_KEYWORDS)

    system_prompt = f"""You are an AI email assistant for {YOUR_NAME}.
Your job:
1. Read the incoming email carefully.
2. Decide if it needs {YOUR_NAME}'s personal attention (needs_human=true) or can be auto-replied (needs_human=false).

Mark needs_human=TRUE if the email:
- Requires a decision, approval, or signature
- Contains financial, legal, or contractual matters
- Is a calendar invite / meeting request that needs acceptance
- Has urgent or sensitive content
- You are not confident about the correct reply
- Contains {"flagged keywords (found in this email!)" if flag_hint else "important keywords"}

Otherwise draft a polite, professional short reply on {YOUR_NAME}'s behalf.

Respond ONLY with valid JSON:
{{
  "needs_human": true|false,
  "reason": "one-sentence explanation",
  "draft_reply": "the reply text (empty string if needs_human=true)"
}}"""

    user_msg = f"""From: {email['from']}
Subject: {email['subject']}
Date: {email['date']}

{body_snippet}"""

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=600,
    )
    raw = response.choices[0].message.content.strip()

    # Strip markdown fences if present
    raw = re.sub(r"^```json\s*|```$", "", raw, flags=re.MULTILINE).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: escalate to human
        return {"needs_human": True, "reason": "Could not parse AI response.", "draft_reply": ""}


def ask_human(email: dict, reason: str) -> str | None:
    """
    Print the email summary and reason, then ask the user what to do.
    Returns the reply text, or None to skip.
    """
    print("\n" + "═"*60)
    print(f"  ⚠  NEEDS YOUR ATTENTION")
    print(f"  From   : {email['from']}")
    print(f"  Subject: {email['subject']}")
    print(f"  Reason : {reason}")
    print("─"*60)
    print(email["body"][:800])
    print("─"*60)
    print("Options: [r] reply  [s] skip  [q] quit agent")
    choice = input("Your choice: ").strip().lower()

    if choice == "q":
        raise SystemExit("Agent stopped by user.")
    if choice == "s":
        return None
    if choice == "r":
        print("Type your reply (press Enter twice when done):")
        lines = []
        while True:
            line = input()
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
        return "\n".join(lines).strip()
    return None

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run_agent():
    print(f"\n🤖  Email Agent starting  |  Model: {GROQ_MODEL}")
    print(f"    Fetching ALL unread emails …\n")

    service = get_gmail_service()
    emails  = fetch_unread_emails(service)

    if not emails:
        print("✅  No unread emails found.")
        return

    print(f"📬  Found {len(emails)} unread email(s).\n")

    for i, email in enumerate(emails, 1):
        print(f"[{i}/{len(emails)}] From: {email['from'][:50]}  |  {email['subject'][:50]}")
        result = classify_and_draft(email)

        if result.get("needs_human"):
            reply = ask_human(email, result.get("reason", "Escalated by AI"))
            if reply:
                send_reply(service, email, reply)
            mark_as_read(service, email["id"])
        else:
            draft = result.get("draft_reply", "").strip()
            if draft:
                print(f"  🤖  Auto-replying: {draft[:80]}…")
                send_reply(service, email, draft)
            mark_as_read(service, email["id"])

    print("\n✅  All emails processed.\n")


if __name__ == "__main__":
    run_agent()