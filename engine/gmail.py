"""
Stellar Dominion - Gmail API Integration
Label-based exactly-once email fetch for player orders.

Requires optional dependencies:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib

Uses Gmail labels for robust exactly-once processing:
    - "orders" label (default: sd-orders) = messages waiting to be processed
    - "processed" label (default: sd-processed) = messages already handled

When a message is processed, the script:
    1. Extracts sender email from the envelope
    2. Extracts orders text (attachment preferred; body fallback)
    3. Removes the orders label, adds the processed label, marks as read

Even if a message is later marked unread manually, it won't be reprocessed
unless the orders label is reapplied.
"""

from __future__ import annotations

import base64
import email.utils
import re
from email import message_from_bytes
from email.message import Message
from pathlib import Path
from typing import Optional, List, Tuple

# Gmail API scopes:
#   gmail.modify = apply/remove labels + remove UNREAD
#   gmail.send   = send emails (for order confirmation replies)
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

PREFERRED_ATTACHMENT_EXTS = (".yaml", ".yml", ".txt")


def check_dependencies():
    """Check that Google API dependencies are installed. Returns (ok, error_message)."""
    try:
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from google.auth.transport.requests import Request  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
        return True, None
    except ImportError as e:
        return False, (
            f"Gmail integration requires additional packages.\n"
            f"Install with: pip install google-api-python-client "
            f"google-auth-httplib2 google-auth-oauthlib\n"
            f"Missing: {e}"
        )


# ======================================================================
# Gmail Authentication
# ======================================================================

def get_gmail_service(credentials_path: Path, token_path: Path, port: int = 0):
    """
    Desktop OAuth flow with token caching.
    
    On first run, opens a browser for consent and saves token.json.
    On subsequent runs, reuses the cached token (refreshing if expired).
    """
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds: Optional[Credentials] = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=port)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


# ======================================================================
# Label Management
# ======================================================================

def list_labels(service) -> List[dict]:
    """List all Gmail labels."""
    resp = service.users().labels().list(userId="me").execute()
    return resp.get("labels", [])


def get_label_id_by_name(service, name: str) -> Optional[str]:
    """Find a label ID by its display name (case-insensitive)."""
    name_lower = name.strip().lower()
    for lbl in list_labels(service):
        if (lbl.get("name") or "").strip().lower() == name_lower:
            return lbl.get("id")
    return None


def ensure_label(service, name: str) -> str:
    """Ensure a user label exists, creating it if necessary. Returns label ID."""
    existing = get_label_id_by_name(service, name)
    if existing:
        return existing

    created = service.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    return created["id"]


# ======================================================================
# Message Fetching & Parsing
# ======================================================================

def fetch_candidate_message_ids(service, query: str, max_results: int = 25) -> List[str]:
    """Search Gmail and return matching message IDs."""
    resp = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()
    return [m["id"] for m in resp.get("messages", [])]


def read_message_raw(service, message_id: str) -> Tuple[bytes, dict]:
    """Fetch a single message in raw (RFC 2822) format."""
    full = service.users().messages().get(
        userId="me", id=message_id, format="raw"
    ).execute()
    raw_bytes = base64.urlsafe_b64decode(full["raw"].encode("utf-8"))
    return raw_bytes, full


def extract_email_address(from_header: str) -> str:
    """Extract bare email address from a From header."""
    _, addr = email.utils.parseaddr(from_header)
    return addr.lower().strip()


def _decode_part_payload(part: Message) -> str:
    """Decode a MIME part's payload to a string."""
    payload_bytes = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload_bytes.decode(charset, errors="replace")
    except LookupError:
        return payload_bytes.decode("utf-8", errors="replace")


def find_orders_text(msg: Message) -> Optional[str]:
    """
    Extract orders text from an email message.
    
    Priority:
    1. Text attachment with .yaml/.yml/.txt extension (best case)
    2. First text/plain body part (fallback for inline orders)
    
    Returns None if no orders text found.
    """
    text_plain_body: Optional[str] = None

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue

            ctype = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()
            filename = (part.get_filename() or "").lower()

            # Some clients attach text as octet-stream but keep the extension
            looks_like_text_attachment = (
                filename.endswith(PREFERRED_ATTACHMENT_EXTS)
                and ("attachment" in disp or bool(filename))
            )

            if ctype == "text/plain" or looks_like_text_attachment:
                text = _decode_part_payload(part).replace("\u00a0", " ").strip()
                is_attachment = "attachment" in disp or bool(filename)

                if is_attachment and filename.endswith(PREFERRED_ATTACHMENT_EXTS):
                    return text  # best case: named attachment

                if not is_attachment and text_plain_body is None and text:
                    text_plain_body = text
    else:
        if (msg.get_content_type() or "").lower() == "text/plain":
            text_plain_body = _decode_part_payload(msg).replace("\u00a0", " ").strip()

    return text_plain_body


# ======================================================================
# Post-Processing
# ======================================================================

def apply_post_process_labels(
    service,
    message_id: str,
    *,
    remove_label_ids: List[str],
    add_label_ids: List[str],
    remove_unread: bool = True,
) -> None:
    """Move a message between labels and optionally mark as read."""
    to_remove = list(remove_label_ids)
    if remove_unread:
        to_remove.append("UNREAD")

    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={
            "removeLabelIds": to_remove,
            "addLabelIds": list(add_label_ids),
        },
    ).execute()


# ======================================================================
# Sending Replies
# ======================================================================

def get_message_metadata(service, message_id: str) -> dict:
    """
    Fetch just the headers we need for threading a reply.
    Returns dict with message_id_header, thread_id, subject, from_email.
    """
    msg = service.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["Message-ID", "Subject", "From"],
    ).execute()

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    return {
        'message_id_header': headers.get("Message-ID", ""),
        'thread_id': msg.get("threadId", ""),
        'subject': headers.get("Subject", ""),
        'from_email': extract_email_address(headers.get("From", "")),
    }


def send_reply(service, original_metadata: dict, reply_body: str) -> str:
    """
    Send a reply that threads against the original message.
    
    original_metadata: dict from get_message_metadata()
    reply_body: plain text body of the reply
    
    Returns the sent message ID.
    """
    from email.mime.text import MIMEText

    # Build the reply subject
    subject = original_metadata['subject']
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    # Build the MIME message
    msg = MIMEText(reply_body, "plain", "utf-8")
    msg["To"] = original_metadata['from_email']
    msg["Subject"] = subject

    # Threading headers
    orig_msg_id = original_metadata['message_id_header']
    if orig_msg_id:
        msg["In-Reply-To"] = orig_msg_id
        msg["References"] = orig_msg_id

    # Encode and send
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    send_body = {"raw": raw}
    # Attach to the same thread so Gmail groups them
    if original_metadata.get('thread_id'):
        send_body["threadId"] = original_metadata['thread_id']

    sent = service.users().messages().send(
        userId="me", body=send_body
    ).execute()

    return sent.get("id", "")
