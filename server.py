import os, time, hmac, hashlib, base64, requests
from urllib.parse import quote
from flask import Flask, request, jsonify

from analyze_trim import process_and_trim_video
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)
import logging
import sys

# =========================================
# 強制的に stdout へログを出力（Render対応）
# =========================================
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)
app.logger.propagate = False

CK = os.getenv("TW_CONSUMER_KEY")
CS = os.getenv("TW_CONSUMER_SECRET")
AT = os.getenv("TW_ACCESS_TOKEN")
AS = os.getenv("TW_ACCESS_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
TWEETS_V2_URL = "https://api.twitter.com/2/tweets"

def percent_encode(s): 
    return quote(str(s), safe='')

def oauth_header(method, url, params=None):
    oauth = {
        'oauth_consumer_key': CK,
        'oauth_nonce': base64.b64encode(os.urandom(16)).decode(),
        'oauth_signature_method': 'HMAC-SHA1',
        'oauth_timestamp': str(int(time.time())),
        'oauth_token': AT,
        'oauth_version': '1.0'
    }

    # パラメータを統合
    param_data = {**oauth}
    if params:
        # None値は署名対象に含めない
        for k,v in params.items():
            if v is not None:
                param_data[k] = str(v)

    # ベース文字列生成
    param_str = '&'.join(f"{percent_encode(k)}={percent_encode(param_data[k])}" for k in sorted(param_data))
    base = '&'.join([method.upper(), percent_encode(url), percent_encode(param_str)])
    key = f"{percent_encode(CS)}&{percent_encode(AS)}"
    sig = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth['oauth_signature'] = sig

    return 'OAuth ' + ', '.join(f'{k}="{percent_encode(v)}"' for k,v in oauth.items())

def upload_video(filepath):
    total = os.path.getsize(filepath)
    init = {'command':'INIT','total_bytes':total,'media_type':'video/mp4','media_category':'tweet_video'}
    r = requests.post(UPLOAD_URL, headers={'Authorization': oauth_header('POST', UPLOAD_URL, init)}, data=init)
    r.raise_for_status()
    media_id = r.json()['media_id_string']

    with open(filepath, 'rb') as f:
        seg = 0
        while True:
            chunk = f.read(5 * 1024 * 1024)
            if not chunk: break
            params = {'command':'APPEND','media_id':media_id,'segment_index':seg}
            files  = {'media': ('chunk.mp4', chunk, 'video/mp4')}
            rr = requests.post(UPLOAD_URL, headers={'Authorization': oauth_header('POST', UPLOAD_URL, params)},
                               data=params, files=files)
            rr.raise_for_status()
            seg += 1

    fin = {'command':'FINALIZE','media_id':media_id}
    r = requests.post(UPLOAD_URL, headers={'Authorization': oauth_header('POST', UPLOAD_URL, fin)}, data=fin)
    r.raise_for_status()

    for _ in range(30):
        time.sleep(3)
        params = {'command':'STATUS','media_id':media_id}
        rr = requests.get(UPLOAD_URL, headers={'Authorization': oauth_header('GET', UPLOAD_URL, params)}, params=params)
        rr.raise_for_status()
        info = rr.json().get('processing_info', {})
        if info.get('state') == 'succeeded': return media_id
        if info.get('state') == 'failed': raise Exception(f"Video processing failed: {rr.text}")
    return media_id

def post_tweet(text, media_id):
    payload = {"text": text}
    if media_id: payload["media"] = {"media_ids": [media_id]}
    r = requests.post(TWEETS_V2_URL, headers={'Authorization': oauth_header('POST', TWEETS_V2_URL, {})}, json=payload)
    r.raise_for_status()
    return r.json()["data"]["id"]

@app.route("/health", methods=["GET"])
def health(): return jsonify({"ok": True})

@app.route("/trim_fanza", methods=["POST"])
def trim_fanza():
    """
    Body: { "video_url": "https://...mp4" }
    戻り: analyze_trim.process_and_trim_video() の結果をそのまま返す
    """
    try:
        data = request.get_json(force=True) or {}
        video_url = data.get("video_url")
        if not video_url:
            return jsonify({"status": "error", "message": "video_url is required"}), 400

        app.logger.info(f"[trim_fanza] start video_url={video_url}")
        result = process_and_trim_video(video_url)
        app.logger.info(f"[trim_fanza] result={result.get('status')} start={result.get('start')} end={result.get('end')}")

        return jsonify(result), 200

    except Exception as e:
        app.logger.exception("[trim_fanza] error")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/trim_fanza_binary", methods=["POST"])
def trim_fanza_binary():
    """
    Body: { "video_url": "https://...mp4" }
    - process_and_trim_video() でトリミング
    - 出来上がった mp4 ファイルをレスポンスとして返す
    """
    try:
        data = request.get_json(force=True) or {}
        video_url = data.get("video_url")
        if not video_url:
            return jsonify({"status": "error", "message": "video_url is required"}), 400

        app.logger.info(f"[trim_fanza_binary] start video_url={video_url}")
        result = process_and_trim_video(video_url)

        if result.get("status") != "success":
            return jsonify(result), 500

        output_path = result.get("output_path")
        if not output_path or not os.path.exists(output_path):
            return jsonify({
                "status": "error",
                "message": "output file not found"
            }), 500

        # ここで動画本体を返す
        return send_file(
            output_path,
            mimetype="video/mp4",
            as_attachment=False,
        )

    except Exception as e:
        app.logger.exception("[trim_fanza_binary] error")
        return jsonify({
            "status": "error",
            "message": str(e),
        }), 500

# ============================================
# 署名検証（GAS→Render間の安全通信）
# ============================================
def verify_request(req):
    app.logger.info("[VERIFY] verify_request called")
    if not WEBHOOK_SECRET:
        return True
    ts = req.headers.get("X-Timestamp", "")
    sig = req.headers.get("X-Signature", "")

    # 👇 Renderのログに確実に出す
    app.logger.info(f"[VERIFY] ts={ts}, sig={sig}")
    app.logger.info(f"[VERIFY] form.text={req.form.get('text')}")
    app.logger.info(f"[VERIFY] form.fileId={req.form.get('fileId')}")

    text = req.form.get("text", "")
    fileId = req.form.get("fileId", "")
    base = f"{ts}::{text}::{fileId}"
    mac = hmac.new(WEBHOOK_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()

    app.logger.info(f"[VERIFY] base={base}")
    app.logger.info(f"[VERIFY] expected={mac}")
    app.logger.info(f"[VERIFY] got={sig}")

    if abs(time.time() - float(ts or 0)) > 300:
        app.logger.info("[VERIFY] timestamp too old")
        return False

    ok = hmac.compare_digest(mac, sig)
    app.logger.info(f"[VERIFY] result={ok}")
    return ok

@app.route("/upload_twitter", methods=["POST"])
def upload_twitter():
    if not verify_request(request):
        return jsonify({"error": "signature verification failed"}), 403

    text = request.form.get("text", "")
    if "file" not in request.files:
        return jsonify({"error": "file missing"}), 400
    fp = "/tmp/video.mp4"
    request.files["file"].save(fp)
    media_id = upload_video(fp)
    tweet_id = post_tweet(text, media_id)
    return jsonify({"status": "ok", "tweet_id": tweet_id})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

