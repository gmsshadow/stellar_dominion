#!/usr/bin/env python3
"""
Stellar Dominion - Gmail Orders Fetch (Standalone, Label-Based "Exactly Once" Processing)

What it does
- Authenticates to Gmail via the Gmail API (OAuth Desktop flow) and caches a token.
- Searches for candidate order emails using a Gmail query (default targets a label + unread).
- Extracts orders text (prefers .yaml/.yml/.txt text attachments; falls back to text/plain body).
- Writes each extracted order to an output folder for later ingestion by pbem.py.
- Marks processed messages as read AND moves them from an "orders" label to a "processed" label.
  This gives you robust "exactly once" behaviour even if a message is later marked unread manually.

Designed to be tested independently before integrating into pbem.py.
"""

from __future__ import annotations

import argparse
import base64
import re
from email import message_from_bytes
from email.message import Message
from pathlib import Path
from typing import Optional, List, Tuple

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# We use gmail.modify so we can apply/remove labels + remove UNREAD.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

PREFERRED_ATTACHMENT_EXTS = (".yaml", ".yml", ".txt")


def _extract_email_address(from_header: str) -> str:
    import email.utils
    _, addr = email.utils.parseaddr(from_header)
    return addr.lower().strip()


def _decode_part_payload(part: Message) -> str:
    payload_bytes = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload_bytes.decode(charset, errors="replace")
    except LookupError:
        return payload_bytes.decode("utf-8", errors="replace")


def _find_orders_text(msg: Message) -> Optional[str]:
    """
    Prefer a text/plain attachment with a .yaml/.yml/.txt filename.
    Fall back to the first reasonable text/plain body part.
    """
    text_plain_body: Optional[str] = None

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue

            ctype = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()
            filename = (part.get_filename() or "").lower()

            # Some clients attach text as octet-stream but keep the filename extension.
            looks_like_text_attachment = (
                filename.endswith(PREFERRED_ATTACHMENT_EXTS)
                and ("attachment" in disp or bool(filename))
            )

            if ctype == "text/plain" or looks_like_text_attachment:
                text = _decode_part_payload(part).replace("\u00a0", " ").strip()
                is_attachment = "attachment" in disp or bool(filename)

                if is_attachment and filename.endswith(PREFERRED_ATTACHMENT_EXTS):
                    return text  # best case

                if not is_attachment and text_plain_body is None and text:
                    text_plain_body = text
    else:
        if (msg.get_content_type() or "").lower() == "text/plain":
            text_plain_body = _decode_part_payload(msg).replace("\u00a0", " ").strip()

    return text_plain_body


def get_gmail_service(credentials_path: Path, token_path: Path, port: int = 0):
    """
    Desktop OAuth flow + token cache.
    """
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


def list_labels(service) -> List[dict]:
    resp = service.users().labels().list(userId="me").execute()
    return resp.get("labels", [])


def get_label_id_by_name(service, name: str) -> Optional[str]:
    name_lower = name.strip().lower()
    for lbl in list_labels(service):
        if (lbl.get("name") or "").strip().lower() == name_lower:
            return lbl.get("id")
    return None


def ensure_label(service, name: str) -> str:
    """
    Ensure a user label exists and return its labelId.
    System labels like UNREAD/INBOX won't be created here (and don't need to be).
    """
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


def sanitize_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s.strip())
    return s[:120]


def fetch_candidate_message_ids(service, query: str, max_results: int) -> List[str]:
    resp = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()
    return [m["id"] for m in resp.get("messages", [])]


def read_message_raw(service, message_id: str) -> Tuple[bytes, dict]:
    full = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    raw_bytes = base64.urlsafe_b64decode(full["raw"].encode("utf-8"))
    return raw_bytes, full


def apply_post_process_labels(
    service,
    message_id: str,
    *,
    remove_label_ids: List[str],
    add_label_ids: List[str],
    remove_unread: bool = True,
) -> None:
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


def main():
    ap = argparse.ArgumentParser(description="Fetch Stellar Dominion orders from Gmail (label-based).")
    ap.add_argument("--credentials", required=True, type=Path, help="OAuth client secrets JSON (credentials.json).")
    ap.add_argument("--token", default=Path("./token.json"), type=Path, help="Token cache path (default: ./token.json).")
    ap.add_argument("--orders-label", default="sd-orders", help="Gmail label that marks incoming orders (default: sd-orders).")
    ap.add_argument("--processed-label", default="sd-processed", help="Gmail label to apply after processing (default: sd-processed).")
    ap.add_argument("--query", default=None, help='Override Gmail query. Default: label:"<orders-label>" is:unread')
    ap.add_argument("--max-results", default=25, type=int, help="Max messages to fetch per run (default: 25).")
    ap.add_argument("--outdir", default=Path("./fetched_orders"), type=Path, help="Where to write extracted orders.")
    ap.add_argument("--dry-run", action="store_true", help="Do not modify Gmail labels / UNREAD state.")
    ap.add_argument("--port", type=int, default=0, help="OAuth local server port (0 = auto).")

    args = ap.parse_args()

    service = get_gmail_service(args.credentials, args.token, port=args.port)

    # Ensure labels exist (user labels only)
    orders_label_id = ensure_label(service, args.orders_label)
    processed_label_id = ensure_label(service, args.processed_label)

    query = args.query or f'label:"{args.orders_label}" is:unread'
    msg_ids = fetch_candidate_message_ids(service, query=query, max_results=args.max_results)

    print(f"[info] query: {query}")
    print(f"[info] found {len(msg_ids)} candidate messages")

    args.outdir.mkdir(parents=True, exist_ok=True)

    processed = 0
    skipped_no_orders = 0

    for msg_id in msg_ids:
        raw_bytes, _full = read_message_raw(service, msg_id)
        eml = message_from_bytes(raw_bytes)

        from_email = _extract_email_address(eml.get("From", ""))
        subject = (eml.get("Subject") or "").strip()

        orders_text = _find_orders_text(eml)
        if not orders_text:
            skipped_no_orders += 1
            print(f"[skip] {msg_id} from {from_email} (no orders found)")
            continue

        filename = sanitize_filename(f"{from_email}__{msg_id}.txt")
        out_path = args.outdir / filename
        out_path.write_text(orders_text.strip() + "\n", encoding="utf-8")

        processed += 1
        print(f"[ok]   {msg_id} from {from_email} subj='{subject}' -> {out_path}")

        if not args.dry_run:
            apply_post_process_labels(
                service,
                msg_id,
                remove_label_ids=[orders_label_id],
                add_label_ids=[processed_label_id],
                remove_unread=True,
            )

    print("")
    print("[summary]")
    print(f"  processed: {processed}")
    print(f"  skipped (no orders): {skipped_no_orders}")
    print(f"  output dir: {args.outdir.resolve()}")
    print(f"  orders label: {args.orders_label}")
    print(f"  processed label: {args.processed_label}")
    print(f"  gmail modified: {'no (dry-run)' if args.dry_run else 'yes'}")


if __name__ == "__main__":
    main()
