"""
YouTube 動画の privacyStatus を毎日チェックし、
意図せず 'private'(非公開) になっている動画があれば 'public'(公開) に戻し、
結果をメールで通知するスクリプト。

GitHub Actions から実行される想定。必要な環境変数は README.md を参照。
"""

import os
import sys
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ---- 環境変数から設定を読み込む ----
CLIENT_ID = os.environ["YOUTUBE_CLIENT_ID"]
CLIENT_SECRET = os.environ["YOUTUBE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["YOUTUBE_REFRESH_TOKEN"]
CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"]

GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_TO_EMAIL = os.environ.get("NOTIFY_TO_EMAIL", GMAIL_ADDRESS)

# 意図的に非公開/限定公開にしている動画IDはカンマ区切りでここに除外指定できる
# (GitHub Secrets/Variables の EXCLUDE_VIDEO_IDS で上書き可能。未設定なら空)
EXCLUDE_VIDEO_IDS = set(
    x.strip() for x in os.environ.get("EXCLUDE_VIDEO_IDS", "").split(",") if x.strip()
)

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def get_youtube_client():
    creds = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("youtube", "v3", credentials=creds)


def get_uploads_playlist_id(youtube, channel_id):
    resp = youtube.channels().list(part="contentDetails", id=channel_id).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"チャンネルが見つかりません: {channel_id}")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def get_all_video_ids(youtube, uploads_playlist_id):
    video_ids = []
    page_token = None
    while True:
        resp = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return video_ids


def chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def get_video_statuses(youtube, video_ids):
    """video_id -> {"title": str, "privacyStatus": str}"""
    result = {}
    for batch in chunked(video_ids, 50):
        resp = youtube.videos().list(
            part="status,snippet",
            id=",".join(batch),
        ).execute()
        for item in resp.get("items", []):
            result[item["id"]] = {
                "title": item["snippet"]["title"],
                "privacyStatus": item["status"]["privacyStatus"],
            }
    return result


def restore_to_public(youtube, video_id):
    body = {"id": video_id, "status": {"privacyStatus": "public"}}
    youtube.videos().update(part="status", body=body).execute()


def send_email(subject, body_text):
    msg = MIMEText(body_text)
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = NOTIFY_TO_EMAIL

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [NOTIFY_TO_EMAIL], msg.as_string())


def main():
    youtube = get_youtube_client()
    uploads_playlist_id = get_uploads_playlist_id(youtube, CHANNEL_ID)
    video_ids = get_all_video_ids(youtube, uploads_playlist_id)
    statuses = get_video_statuses(youtube, video_ids)

    restored = []
    failed = []

    for video_id, info in statuses.items():
        if video_id in EXCLUDE_VIDEO_IDS:
            continue
        if info["privacyStatus"] == "private":
            try:
                restore_to_public(youtube, video_id)
                restored.append((video_id, info["title"]))
                print(f"[復帰] {info['title']} ({video_id})")
            except HttpError as e:
                failed.append((video_id, info["title"], str(e)))
                print(f"[失敗] {info['title']} ({video_id}): {e}", file=sys.stderr)

    if not restored and not failed:
        print("非公開になっている動画はありませんでした。問題なし。")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"実行日時: {now}", ""]

    if restored:
        lines.append(f"■ 自動で「公開」に戻した動画 ({len(restored)}件)")
        for vid, title in restored:
            lines.append(f"- {title}\n  https://www.youtube.com/watch?v={vid}")
        lines.append("")

    if failed:
        lines.append(f"■ 公開に戻せなかった動画 ({len(failed)}件) ※要手動確認")
        lines.append("  YouTube側の審査でブロックされている可能性があります。")
        for vid, title, err in failed:
            lines.append(f"- {title}\n  https://www.youtube.com/watch?v={vid}\n  エラー: {err}")

    body_text = "\n".join(lines)
    subject = f"[YouTube監視] 非公開検出 {len(restored)}件復帰 / {len(failed)}件失敗"
    send_email(subject, body_text)
    print("メール通知を送信しました。")


if __name__ == "__main__":
    main()
