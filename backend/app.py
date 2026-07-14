"""
YouTube 影片搜尋/逐字稿/留言擷取 後端服務
執行方式: python app.py  (預設監聽 http://127.0.0.1:5001)
"""
import os
import json
import re
import traceback

from flask import Flask, request, jsonify, send_from_directory, redirect, session

# 本機用 http://127.0.0.1 測試OAuth時,Google的oauthlib預設會要求HTTPS而報錯,
# 這行是允許本機開發用http測試;部署到Render等平台(有HTTPS)時這行不影響安全性判斷,
# 但正式對外使用還是建議走HTTPS網址。
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# Google在授權完成後,回傳的scope常會自動多加一個'openid',跟我們原本要求的scope
# 不完全一致,若沒有這行,oauthlib預設會直接丟例外導致500錯誤,這是已知常見雷。
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from flask_cors import CORS

# static_url_path="" 讓 static 資料夾內的檔案直接掛在網站根目錄，
# 例如 static/manifest.json 會變成 /manifest.json，不用寫 /static/manifest.json
app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
CORS(app, supports_credentials=True)  # 保留CORS支援：若之後想拆成前後端分離部署，仍然可以跨網域呼叫


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def extract_video_id(text: str) -> str:
    """支援直接貼YouTube連結或純video_id"""
    text = text.strip()
    patterns = [
        r"(?:v=|/)([0-9A-Za-z_-]{11}).*",
        r"^([0-9A-Za-z_-]{11})$",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return text


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


def _derive_upload_date(entry):
    """
    yt-dlp在『列表模式』(搜尋結果/頻道影片列表)抓到的資料,不同版本/不同頁面
    回傳的日期欄位不太一致:有時候是 upload_date(YYYYMMDD字串),有時候只有
    timestamp / release_timestamp(Unix秒數)。這裡把常見的幾種都嘗試一次,
    盡量避免因為欄位對不上而讓前端顯示空白。
    """
    if entry.get("upload_date"):
        return entry["upload_date"]
    epoch = entry.get("timestamp") or entry.get("release_timestamp")
    if epoch:
        try:
            import datetime

            return datetime.datetime.utcfromtimestamp(epoch).strftime("%Y%m%d")
        except Exception:
            return None
    return None


def _duration_in_bucket(seconds, bucket):
    """時長分級比照YouTube官方定義:短片<4分鐘,長片>20分鐘"""
    if bucket == "any":
        return True
    if seconds is None:
        return False
    if bucket == "short":
        return seconds < 240
    if bucket == "medium":
        return 240 <= seconds <= 1200
    if bucket == "long":
        return seconds > 1200
    return True


def _within_upload_date_bucket(upload_date_yyyymmdd, bucket):
    """upload_date_yyyymmdd 格式為 YYYYMMDD 字串"""
    if bucket == "any":
        return True
    if not upload_date_yyyymmdd:
        return False
    import datetime

    days_map = {"today": 1, "week": 7, "month": 31, "year": 366}
    cutoff_days = days_map.get(bucket)
    try:
        d = datetime.datetime.strptime(upload_date_yyyymmdd, "%Y%m%d")
    except Exception:
        return False
    return (datetime.datetime.utcnow() - d).days <= cutoff_days


def _parse_iso8601_duration(s):
    """把YouTube Data API回傳的ISO8601時長(例如 PT4M13S)轉成秒數"""
    if not s:
        return None
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return None
    h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + sec


def _video_entry_to_dict(entry, channel_name, channel_url):
    vid = entry.get("id")
    return {
        "video_id": vid,
        "title": entry.get("title"),
        "channel": channel_name,
        "channel_url": channel_url,
        "duration": entry.get("duration"),
        "view_count": entry.get("view_count"),
        "upload_date": _derive_upload_date(entry),  # 格式 YYYYMMDD,可能為 None
        "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "url": f"https://www.youtube.com/watch?v={vid}",
    }


def fetch_channel_data(channel_url: str, limit: int = 20, sort: str = "newest", q: str = "") -> dict:
    """
    共用函式:抓某頻道的基本資料(名稱/訂閱數/簡介)+影片清單

    sort: "newest"(預設,YouTube原始順序=最新優先) / "oldest"(把抓到的這批影片反過來,
          注意:這只是「這批已抓到的影片」內反轉,不是回溯頻道全部歷史) /
          "views"(依觀看數由高到低排序,同樣只在這批已抓到的影片內排序)
    q: 若有給,會在抓到的影片標題中做關鍵字篩選(等於「頻道內搜尋」,但搜尋範圍只有
       這批抓到的影片,不是頻道全部影片,詳見README的已知限制)
    """
    import yt_dlp

    # 做「觀看數排序」或「關鍵字搜尋」時,多抓一些候選(最多50)以提高命中機會
    fetch_limit = max(limit, 50) if (sort == "views" or q) else limit

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": fetch_limit,
    }
    target = channel_url if channel_url.rstrip("/").endswith("/videos") else channel_url.rstrip("/") + "/videos"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target, download=False)

    channel_name = info.get("channel") or info.get("uploader") or info.get("title") or ""
    description = (info.get("description") or "").strip()
    if len(description) > 160:
        description = description[:160] + "…"

    entries = [e for e in (info.get("entries") or []) if e]

    if q:
        needle = q.strip().lower()
        entries = [e for e in entries if needle in (e.get("title") or "").lower()]

    if sort == "oldest":
        entries = list(reversed(entries))
    elif sort == "views":
        entries = sorted(entries, key=lambda e: (e.get("view_count") or 0), reverse=True)
    # sort == "newest" -> 保持yt-dlp原始順序(頻道預設就是最新優先)

    videos = [_video_entry_to_dict(e, channel_name, channel_url) for e in entries[:limit]]

    return {
        "name": channel_name,
        "url": channel_url,
        "subscriber_count": info.get("channel_follower_count"),
        "description": description,
        "video_count": info.get("playlist_count"),
        "videos": videos,
        "sort": sort,
        "search_scope_note": "頻道內搜尋僅涵蓋最近抓取的影片批次,非頻道全部歷史影片" if q else None,
    }


def fetch_channel_playlists(channel_url: str, limit: int = 20) -> list:
    """抓頻道主自己建立的播放清單列表"""
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
    }
    target = channel_url.rstrip("/") + "/playlists"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target, download=False)

    playlists = []
    for entry in (info.get("entries") or [])[:limit]:
        if not entry:
            continue
        pid = entry.get("id")
        playlists.append(
            {
                "playlist_id": pid,
                "title": entry.get("title"),
                "video_count": entry.get("playlist_count") or entry.get("video_count"),
                "thumbnail": entry.get("thumbnails", [{}])[-1].get("url") if entry.get("thumbnails") else None,
                "url": f"https://www.youtube.com/playlist?list={pid}",
            }
        )
    return playlists


@app.route("/api/search", methods=["GET"])
def search_videos():
    """
    用 yt-dlp 的 ytsearch 語法搜尋,不需要官方 API Key
    query params:
      q=關鍵字(必填), limit=筆數(預設10)
      sort=relevance(預設,YouTube原始排序)/newest/oldest/views
      duration=any(預設)/short(<4分鐘)/medium(4-20分鐘)/long(>20分鐘)
      upload_date=any(預設)/today/week/month/year

    注意(重要限制):sort/duration/upload_date這三個篩選,是在「已經抓到的這批搜尋結果」
    裡面做篩選跟排序,不是叫YouTube重新用該條件搜尋一次。也就是說,如果條件太窄(例如
    只篩「今天上傳」),有可能篩到剩0筆,這不代表YouTube上沒有符合的影片,只是這批結果
    剛好沒有,可以嘗試放寬條件或加大limit。
    回傳除了一般影片結果外,也會附上「最相關頻道」預覽(取第一筆結果所屬頻道)
    """
    query = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 10))
    sort = request.args.get("sort", "relevance")
    duration_filter = request.args.get("duration", "any")
    upload_date_filter = request.args.get("upload_date", "any")
    if not query:
        return jsonify({"error": "缺少查詢參數 q"}), 400

    try:
        import yt_dlp

        has_filters = sort != "relevance" or duration_filter != "any" or upload_date_filter != "any"
        fetch_limit = max(limit, 50) if has_filters else limit

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,  # 只取清單資訊,不解析完整格式,速度快
            "skip_download": True,
        }
        search_query = f"ytsearch{fetch_limit}:{query}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)

        entries = [e for e in info.get("entries", []) if e]

        # 時長篩選(秒數門檻比照YouTube官方定義:短片<4分鐘,長片>20分鐘)
        if duration_filter != "any":
            entries = [e for e in entries if _duration_in_bucket(e.get("duration"), duration_filter)]

        # 上傳時間篩選
        if upload_date_filter != "any":
            entries = [e for e in entries if _within_upload_date_bucket(_derive_upload_date(e), upload_date_filter)]

        if sort == "newest":
            entries.sort(key=lambda e: _derive_upload_date(e) or "", reverse=True)
        elif sort == "oldest":
            entries.sort(key=lambda e: _derive_upload_date(e) or "99999999")
        elif sort == "views":
            entries.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
        # sort == "relevance" -> 保持yt-dlp原始順序

        entries = entries[:limit]

        results = []
        for entry in entries:
            vid = entry.get("id")
            results.append(
                {
                    "video_id": vid,
                    "title": entry.get("title"),
                    "channel": entry.get("uploader") or entry.get("channel"),
                    "channel_url": entry.get("channel_url") or entry.get("uploader_url"),
                    "duration": entry.get("duration"),
                    "view_count": entry.get("view_count"),
                    "upload_date": _derive_upload_date(entry),
                    "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                }
            )

        # 嘗試附上「最相關頻道」預覽區塊(比照YouTube搜尋頁面的頻道卡片)
        channel_preview = None
        if results and results[0].get("channel_url"):
            try:
                data = fetch_channel_data(results[0]["channel_url"], limit=6)
                channel_preview = data
            except Exception:
                channel_preview = None  # 頻道預覽失敗不影響主要搜尋結果

        return jsonify({"query": query, "channel": channel_preview, "results": results})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"搜尋失敗: {str(e)}"}), 500


@app.route("/api/channel", methods=["GET"])
def get_channel_videos():
    """
    列出某個頻道的完整資料(名稱/訂閱數/簡介)+影片
    query params:
      url=頻道網址(必填)
      limit=筆數(預設24)
      sort=newest(預設)/oldest/views
      q=頻道內關鍵字篩選(選填,搜尋範圍見函式說明)
    """
    channel_url = request.args.get("url", "").strip()
    limit = int(request.args.get("limit", 24))
    sort = request.args.get("sort", "newest")
    q = request.args.get("q", "").strip()
    if not channel_url:
        return jsonify({"error": "缺少 url 參數"}), 400

    try:
        data = fetch_channel_data(channel_url, limit=limit, sort=sort, q=q)
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"頻道擷取失敗: {str(e)}"}), 500


@app.route("/api/channel_playlists", methods=["GET"])
def get_channel_playlists():
    """列出頻道主自己建立的播放清單"""
    channel_url = request.args.get("url", "").strip()
    limit = int(request.args.get("limit", 20))
    if not channel_url:
        return jsonify({"error": "缺少 url 參數"}), 400

    try:
        playlists = fetch_channel_playlists(channel_url, limit=limit)
        return jsonify({"playlists": playlists})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"播放清單擷取失敗: {str(e)}"}), 500


def _resolve_secret_path(filename: str) -> str:
    """Render的Secret Files功能會把檔案放在/etc/secrets/<filename>,
    本機開發時則是放在backend資料夾內跟app.py同一層,這裡依序檢查,
    找不到的話仍回傳本機路徑(讓錯誤訊息維持原本'請先完成OAuth憑證申請'的提示)。"""
    render_secret_path = os.path.join("/etc/secrets", filename)
    local_path = os.path.join(os.path.dirname(__file__), filename)
    if os.path.exists(render_secret_path):
        return render_secret_path
    return local_path


CLIENT_SECRETS_FILE = _resolve_secret_path("client_secret.json")
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "token.json")
CATEGORIES_FILE = os.path.join(os.path.dirname(__file__), "categories.json")
FAVORITES_FILE = os.path.join(os.path.dirname(__file__), "favorites.json")
YT_READONLY_SCOPES = ["https://www.googleapis.com/auth/youtube"]  # 含讀寫,支援訂閱/取消訂閱(原本只有.readonly)


@app.route("/api/debug-secret", methods=["GET"])
def _debug_secret():
    """暫時性的排查端點:檢查兩個候選路徑上到底存不存在client_secret.json,
    確認後這個路由可以整個刪掉,不影響其他功能。"""
    render_path = "/etc/secrets/client_secret.json"
    local_path = os.path.join(os.path.dirname(__file__), "client_secret.json")
    return jsonify({
        "render_secret_path": render_path,
        "render_secret_exists": os.path.exists(render_path),
        "local_path": local_path,
        "local_exists": os.path.exists(local_path),
        "resolved_CLIENT_SECRETS_FILE": CLIENT_SECRETS_FILE,
        "etc_secrets_dir_listing": os.listdir("/etc/secrets") if os.path.isdir("/etc/secrets") else "目錄不存在",
    })


JSONBIN_API_KEY = os.environ.get("JSONBIN_API_KEY", "")
JSONBIN_CATEGORIES_ID = os.environ.get("JSONBIN_CATEGORIES_ID", "")
JSONBIN_FAVORITES_ID = os.environ.get("JSONBIN_FAVORITES_ID", "")


def _load_json_file(path, default, bin_id=None):
    """
    優先從JSONBin.io(免費雲端JSON儲存)讀取,因為Render免費方案的檔案系統是暫存的,
    每次重新部署/重啟都會被清空,本機檔案只能當作JSONBin連不上時的緊急備援。
    """
    if bin_id and JSONBIN_API_KEY:
        try:
            import requests
            resp = requests.get(
                f"https://api.jsonbin.io/v3/b/{bin_id}/latest",
                headers={"X-Master-Key": JSONBIN_API_KEY},
                timeout=8,
            )
            resp.raise_for_status()
            return resp.json().get("record", default)
        except Exception:
            traceback.print_exc()  # 讀取失敗就往下退回本機檔案,不讓整個功能掛掉
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json_file(path, data, bin_id=None):
    if bin_id and JSONBIN_API_KEY:
        try:
            import requests
            resp = requests.put(
                f"https://api.jsonbin.io/v3/b/{bin_id}",
                headers={"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"},
                json=data,
                timeout=8,
            )
            resp.raise_for_status()
            return
        except Exception:
            traceback.print_exc()  # 雲端寫入失敗仍寫一份本機檔案,至少這次執行期間資料不會憑空消失
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.route("/api/categories", methods=["GET"])
def api_get_categories():
    """讀取頻道分類(格式: {頻道ID: [分類名,...]}),存在伺服器上,所有裝置共用同一份"""
    return jsonify({"categories": _load_json_file(CATEGORIES_FILE, {}, JSONBIN_CATEGORIES_ID)})


@app.route("/api/categories", methods=["POST"])
def api_save_categories():
    """整包覆蓋儲存頻道分類"""
    data = request.json or {}
    categories = data.get("categories", {})
    _save_json_file(CATEGORIES_FILE, categories, JSONBIN_CATEGORIES_ID)
    return jsonify({"status": "ok"})


@app.route("/api/favorites", methods=["GET"])
def api_get_favorites():
    """讀取「我的最愛」篩選組合,存在伺服器上,所有裝置共用同一份"""
    return jsonify({"favorites": _load_json_file(FAVORITES_FILE, [], JSONBIN_FAVORITES_ID)})


@app.route("/api/favorites", methods=["POST"])
def api_save_favorites():
    """整包覆蓋儲存「我的最愛」篩選組合"""
    data = request.json or {}
    favorites = data.get("favorites", [])
    _save_json_file(FAVORITES_FILE, favorites, JSONBIN_FAVORITES_ID)
    return jsonify({"status": "ok"})


JSONBIN_TOKEN_ID = os.environ.get("JSONBIN_TOKEN_ID", "")


def _load_google_credentials():
    """讀取保存的Google憑證(優先從JSONBin雲端讀,本機檔案僅供備援),過期會自動用refresh_token換新並存回"""
    data = _load_json_file(TOKEN_FILE, None, JSONBIN_TOKEN_ID)
    if not data:
        return None
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleAuthRequest

    creds = Credentials.from_authorized_user_info(data, YT_READONLY_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
            _save_google_credentials(creds)
        except Exception:
            return None
    return creds


def _save_google_credentials(creds):
    _save_json_file(TOKEN_FILE, json.loads(creds.to_json()), JSONBIN_TOKEN_ID)


@app.route("/auth/status", methods=["GET"])
def auth_status():
    creds = _load_google_credentials()
    return jsonify({"logged_in": bool(creds and creds.valid)})


def _oauth_error_page(title: str, err: Exception) -> str:
    """統一的OAuth錯誤畫面,顯示具體錯誤原因方便排查,而不是Flask預設的空白500頁"""
    return (
        f"<div style='font-family:sans-serif;background:#0F1115;color:#E7E5E0;"
        f"padding:40px;min-height:100vh'>"
        f"<h2 style='color:#E8A33D'>{title}</h2>"
        f"<p>發生以下錯誤，把這段訊息複製給開發協助者即可快速定位問題：</p>"
        f"<pre style='background:#171A21;padding:16px;border-radius:8px;"
        f"white-space:pre-wrap;color:#e08888'>{type(err).__name__}: {err}</pre>"
        f"<p><a href='/' style='color:#E8A33D'>← 返回首頁</a></p></div>"
    ), 500


@app.route("/auth/login", methods=["GET"])
def auth_login():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        return jsonify({"error": "尚未設定 client_secret.json，請先完成 Google OAuth 憑證申請(見README)"}), 400
    try:
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=YT_READONLY_SCOPES,
            redirect_uri=request.url_root.rstrip("/") + "/auth/callback",
        )
        auth_url, state = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent"
        )
        session["oauth_state"] = state
        # PKCE安全機制:Google現在會要求一組配對的「驗證碼」(code_verifier),
        # 這裡產生的驗證碼跟/auth/login是不同的Flow物件(不同次請求),
        # 所以要先存進session,等/auth/callback時再讀回來配對,不然Google會拒絕換token。
        session["code_verifier"] = flow.code_verifier
        return redirect(auth_url)
    except Exception as e:
        traceback.print_exc()
        return _oauth_error_page("登入啟動失敗", e)


@app.route("/auth/callback", methods=["GET"])
def auth_callback():
    try:
        from google_auth_oauthlib.flow import Flow

        state = session.get("oauth_state")
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=YT_READONLY_SCOPES,
            state=state,
            redirect_uri=request.url_root.rstrip("/") + "/auth/callback",
        )
        # 帶回/auth/login時存的驗證碼,跟Google要求的PKCE配對,不然會出現
        # invalid_grant: Missing code verifier 錯誤。
        flow.code_verifier = session.get("code_verifier")
        flow.fetch_token(authorization_response=request.url)
        _save_google_credentials(flow.credentials)
        return redirect("/")
    except Exception as e:
        traceback.print_exc()
        return _oauth_error_page("登入完成失敗", e)


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
    return jsonify({"status": "ok"})


@app.route("/api/subscriptions", methods=["GET"])
def api_subscriptions():
    """列出目前登入帳號訂閱的所有頻道"""
    creds = _load_google_credentials()
    if not creds:
        return jsonify({"error": "尚未登入Google帳號"}), 401
    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=creds)
        subs = []
        page_token = None
        while True:
            resp = youtube.subscriptions().list(
                part="snippet", mine=True, maxResults=50, pageToken=page_token
            ).execute()
            for item in resp.get("items", []):
                sn = item["snippet"]
                thumbs = sn.get("thumbnails") or {}
                subs.append(
                    {
                        "channel_id": sn["resourceId"]["channelId"],
                        "subscription_id": item["id"],  # 取消訂閱要用這個id,不是channel_id
                        "title": sn["title"],
                        "thumbnail": (thumbs.get("default") or {}).get("url"),
                    }
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return jsonify({"subscriptions": subs})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"訂閱清單擷取失敗: {str(e)}"}), 500


@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    """訂閱一個頻道"""
    creds = _load_google_credentials()
    if not creds:
        return jsonify({"error": "尚未登入Google帳號"}), 401
    channel_id = (request.json or {}).get("channel_id", "").strip()
    if not channel_id:
        return jsonify({"error": "缺少 channel_id"}), 400
    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=creds)
        resp = youtube.subscriptions().insert(
            part="snippet",
            body={"snippet": {"resourceId": {"kind": "youtube#channel", "channelId": channel_id}}},
        ).execute()
        return jsonify({"subscription_id": resp["id"]})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"訂閱失敗: {str(e)}"}), 500


@app.route("/api/unsubscribe", methods=["POST"])
def api_unsubscribe():
    """取消訂閱一個頻道"""
    creds = _load_google_credentials()
    if not creds:
        return jsonify({"error": "尚未登入Google帳號"}), 401
    subscription_id = (request.json or {}).get("subscription_id", "").strip()
    if not subscription_id:
        return jsonify({"error": "缺少 subscription_id"}), 400
    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=creds)
        youtube.subscriptions().delete(id=subscription_id).execute()
        return jsonify({"status": "ok"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"取消訂閱失敗: {str(e)}"}), 500


@app.route("/api/channels_last_video", methods=["GET"])
def api_channels_last_video():
    """
    批次查詢多個頻道『最新一部影片』的發佈時間(給分類管理頁面顯示更新狀態用)
    query params: channels=頻道ID(逗號分隔)
    注意:頻道數量多時,這個端點會依序打好幾次API,回應時間會隨頻道數增加而變長。
    """
    creds = _load_google_credentials()
    if not creds:
        return jsonify({"error": "尚未登入Google帳號"}), 401
    channel_ids = [c for c in request.args.get("channels", "").split(",") if c]
    if not channel_ids:
        return jsonify({"error": "缺少 channels 參數"}), 400

    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=creds)
        result = {}
        for cid in channel_ids:
            if not cid.startswith("UC"):
                continue
            uploads_playlist = "UU" + cid[2:]
            try:
                resp = youtube.playlistItems().list(
                    part="snippet", playlistId=uploads_playlist, maxResults=1
                ).execute()
                items = resp.get("items", [])
                result[cid] = items[0]["snippet"]["publishedAt"] if items else None
            except Exception:
                result[cid] = None
        return jsonify({"last_updated": result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"最後更新時間查詢失敗: {str(e)}"}), 500


@app.route("/api/my_playlists", methods=["GET"])
def api_my_playlists():
    """列出登入帳號自己建立的播放清單(不是頻道的,是『你自己的YouTube帳號』底下的清單)"""
    creds = _load_google_credentials()
    if not creds:
        return jsonify({"error": "尚未登入Google帳號"}), 401
    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=creds)
        playlists = []
        page_token = None
        while True:
            resp = youtube.playlists().list(
                part="snippet,contentDetails", mine=True, maxResults=50, pageToken=page_token
            ).execute()
            for item in resp.get("items", []):
                playlists.append(
                    {
                        "playlist_id": item["id"],
                        "title": item["snippet"]["title"],
                        "video_count": item.get("contentDetails", {}).get("itemCount"),
                    }
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return jsonify({"playlists": playlists})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"播放清單擷取失敗: {str(e)}"}), 500


@app.route("/api/playlist_items", methods=["GET"])
def api_playlist_items():
    """
    列出某個播放清單裡的所有影片(含playlist_item_id,移除影片時要用這個id,不是video_id)
    query params: playlist_id=必填, limit=筆數上限(預設100)
    """
    creds = _load_google_credentials()
    if not creds:
        return jsonify({"error": "尚未登入Google帳號"}), 401
    playlist_id = request.args.get("playlist_id", "").strip()
    limit = int(request.args.get("limit", 100))
    if not playlist_id:
        return jsonify({"error": "缺少 playlist_id 參數"}), 400
    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=creds)
        items = []
        page_token = None
        while len(items) < limit:
            resp = youtube.playlistItems().list(
                part="snippet", playlistId=playlist_id, maxResults=50, pageToken=page_token
            ).execute()
            for item in resp.get("items", []):
                sn = item["snippet"]
                res_id = sn.get("resourceId", {})
                if res_id.get("kind") != "youtube#video":
                    continue
                ch_id = sn.get("videoOwnerChannelId")
                items.append(
                    {
                        "playlist_item_id": item["id"],
                        "video_id": res_id.get("videoId"),
                        "title": sn.get("title"),
                        "channel": sn.get("videoOwnerChannelTitle"),  # 影片實際所屬頻道(不是播放清單擁有者)
                        "channel_id": ch_id,
                        "channel_url": f"https://www.youtube.com/channel/{ch_id}" if ch_id else None,
                        "position": sn.get("position"),
                    }
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return jsonify({"items": items[:limit]})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"播放清單內容擷取失敗: {str(e)}"}), 500


@app.route("/api/playlist_remove_item", methods=["POST"])
def api_playlist_remove_item():
    """把一部影片從播放清單移除(需要playlist_item_id,不是video_id)"""
    creds = _load_google_credentials()
    if not creds:
        return jsonify({"error": "尚未登入Google帳號"}), 401
    playlist_item_id = (request.json or {}).get("playlist_item_id", "").strip()
    if not playlist_item_id:
        return jsonify({"error": "缺少 playlist_item_id"}), 400
    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=creds)
        youtube.playlistItems().delete(id=playlist_item_id).execute()
        return jsonify({"status": "ok"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"移除失敗: {str(e)}"}), 500


@app.route("/api/playlist_add_item", methods=["POST"])
def api_playlist_add_item():
    """把一部影片加進指定的播放清單(供「移到其他清單」使用)"""
    creds = _load_google_credentials()
    if not creds:
        return jsonify({"error": "尚未登入Google帳號"}), 401
    body = request.json or {}
    playlist_id = body.get("playlist_id", "").strip()
    video_id = body.get("video_id", "").strip()
    if not playlist_id or not video_id:
        return jsonify({"error": "缺少 playlist_id 或 video_id"}), 400
    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=creds)
        resp = youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        ).execute()
        return jsonify({"playlist_item_id": resp["id"]})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"加入播放清單失敗: {str(e)}"}), 500


@app.route("/api/subscriptions_feed", methods=["GET"])
def api_subscriptions_feed():
    """
    彙整「勾選的訂閱頻道」最新影片,依發佈時間新到舊排序
    query params: channels=頻道ID(逗號分隔,必填), per_channel=每頻道抓幾支(預設5)
    """
    creds = _load_google_credentials()
    if not creds:
        return jsonify({"error": "尚未登入Google帳號"}), 401

    channel_ids = [c for c in request.args.get("channels", "").split(",") if c]
    per_channel = int(request.args.get("per_channel", 5))
    duration_filter = request.args.get("duration", "any")
    upload_date_filter = request.args.get("upload_date", "any")
    if not channel_ids:
        return jsonify({"error": "缺少 channels 參數(勾選的頻道ID清單)"}), 400

    try:
        from googleapiclient.discovery import build

        youtube = build("youtube", "v3", credentials=creds)
        all_videos = []
        for cid in channel_ids:
            if not cid.startswith("UC"):
                continue
            uploads_playlist = "UU" + cid[2:]  # YouTube頻道的「上傳播放清單」ID命名慣例
            try:
                resp = youtube.playlistItems().list(
                    part="snippet", playlistId=uploads_playlist, maxResults=per_channel
                ).execute()
            except Exception:
                continue
            for item in resp.get("items", []):
                sn = item["snippet"]
                vid = sn["resourceId"]["videoId"]
                all_videos.append(
                    {
                        "video_id": vid,
                        "title": sn.get("title"),
                        "channel": sn.get("channelTitle"),
                        "channel_url": f"https://www.youtube.com/channel/{cid}",
                        "published_at": sn.get("publishedAt"),
                        "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                        "url": f"https://www.youtube.com/watch?v={vid}",
                    }
                )

        # 補上觀看數與時長(videos.list一次最多查50個影片id,分批處理)
        video_ids = [v["video_id"] for v in all_videos]
        stats_map = {}
        duration_map = {}
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            try:
                resp = youtube.videos().list(part="statistics,contentDetails", id=",".join(batch)).execute()
                for item in resp.get("items", []):
                    stats_map[item["id"]] = item.get("statistics", {}).get("viewCount")
                    duration_map[item["id"]] = _parse_iso8601_duration(
                        item.get("contentDetails", {}).get("duration")
                    )
            except Exception:
                pass
        for v in all_videos:
            vc = stats_map.get(v["video_id"])
            v["view_count"] = int(vc) if vc else None
            v["duration"] = duration_map.get(v["video_id"])

        # 時長篩選
        if duration_filter != "any":
            all_videos = [v for v in all_videos if _duration_in_bucket(v.get("duration"), duration_filter)]

        # 上傳時間篩選(用published_at的日期部分,轉成YYYYMMDD比照其他端點的判斷邏輯)
        if upload_date_filter != "any":
            def _pub_to_yyyymmdd(v):
                pa = v.get("published_at")
                if not pa:
                    return None
                return pa[:10].replace("-", "")

            all_videos = [
                v for v in all_videos
                if _within_upload_date_bucket(_pub_to_yyyymmdd(v), upload_date_filter)
            ]

        all_videos.sort(key=lambda v: v.get("published_at") or "", reverse=True)
        return jsonify({"videos": all_videos})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"訂閱動態擷取失敗: {str(e)}"}), 500


@app.route("/api/transcript", methods=["GET"])
def get_transcript():
    """
    擷取影片逐字稿
    query params: video= (可傳完整連結或video_id), lang=語言優先順序(預設 zh-Hant,zh,en)
    """
    raw = request.args.get("video", "").strip()
    if not raw:
        return jsonify({"error": "缺少 video 參數"}), 400
    video_id = extract_video_id(raw)
    lang_pref = request.args.get("lang", "zh-Hant,zh-TW,zh,en").split(",")

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            TranscriptsDisabled,
            NoTranscriptFound,
            VideoUnavailable,
        )

        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)

        transcript = None
        try:
            transcript = transcript_list.find_transcript(lang_pref)
        except NoTranscriptFound:
            # 找不到偏好語言時,退而求其次抓第一個可用的(含自動產生字幕)
            for t in transcript_list:
                transcript = t
                break

        if transcript is None:
            return jsonify({"error": "此影片沒有可用的逐字稿"}), 404

        fetched = transcript.fetch()
        segments = [
            {"start": seg.start, "duration": seg.duration, "text": seg.text}
            for seg in fetched
        ]
        full_text = "\n".join(seg["text"] for seg in segments)

        return jsonify(
            {
                "video_id": video_id,
                "language": transcript.language,
                "language_code": transcript.language_code,
                "is_generated": transcript.is_generated,
                "segments": segments,
                "full_text": full_text,
            }
        )
    except TranscriptsDisabled:
        return jsonify({"error": "此影片已關閉字幕功能"}), 404
    except VideoUnavailable:
        return jsonify({"error": "影片不存在或無法存取"}), 404
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"逐字稿擷取失敗: {str(e)}"}), 500


@app.route("/api/comments", methods=["GET"])
def get_comments():
    """
    擷取影片留言
    query params: video=, limit=留言數(預設50), sort=0(熱門)/1(最新, 預設0)
    """
    raw = request.args.get("video", "").strip()
    if not raw:
        return jsonify({"error": "缺少 video 參數"}), 400
    video_id = extract_video_id(raw)
    limit = int(request.args.get("limit", 50))
    sort_by = int(request.args.get("sort", 0))

    try:
        from youtube_comment_downloader import YoutubeCommentDownloader, SORT_BY_POPULAR, SORT_BY_RECENT

        downloader = YoutubeCommentDownloader()
        url = f"https://www.youtube.com/watch?v={video_id}"
        sort_mode = SORT_BY_POPULAR if sort_by == 0 else SORT_BY_RECENT

        comments = []
        for i, c in enumerate(downloader.get_comments_from_url(url, sort_by=sort_mode)):
            if i >= limit:
                break
            comments.append(
                {
                    "author": c.get("author"),
                    "text": c.get("text"),
                    "votes": c.get("votes"),
                    "time": c.get("time"),
                    "reply": c.get("reply", False),
                }
            )

        return jsonify({"video_id": video_id, "count": len(comments), "comments": comments})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"留言擷取失敗: {str(e)}"}), 500


if __name__ == "__main__":
    import os

    # 綁定 0.0.0.0(而非127.0.0.1),這樣同一個WiFi網路下的手機/平板才連得進來
    # (用電腦的區網IP存取,例如 http://192.168.x.x:5001,不是127.0.0.1)。
    # 部署到 Render 等雲端平台時,平台會透過環境變數 PORT 指定要監聽的埠號。
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
