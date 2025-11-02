import os, time, hmac, hashlib, base64, requests
from urllib.parse import quote
from flask import Flask, request, jsonify

app = Flask(__name__)

CK = os.getenv("TW_CONSUMER_KEY")
CS = os.getenv("TW_CONSUMER_SECRET")
AT = os.getenv("TW_ACCESS_TOKEN")
AS = os.getenv("TW_ACCESS_SECRET")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
TWEETS_V2_URL = "https://api.twitter.com/2/tweets"

def percent_encode(s): 
    return quote(str(s), safe='')

def oauth_header(method, url, params):
    oauth = {
        'oauth_consumer_key': CK,
        'oauth_nonce': base64.b64encode(os.urandom(16)).decode(),
        'oauth_signature_method': 'HMAC-SHA1',
        'oauth_timestamp': str(int(time.time())),
        'oauth_token': AT,
        'oauth_version': '1.0'
    }
    all_params = {**oauth, **(params or {})}
    base = '&'.join([
        method.upper(),
        percent_encode(url),
        percent_encode('&'.join(f"{percent_encode(k)}={percent_encode(all_params[k])}" 
                                for k in sorted(all_params)))
    ])
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

@app.route("/upload_twitter", methods=["POST"])
def upload_twitter():
    # 署名はまずはオフ（後で有効化）
    text = request.form.get("text", "")
    if "file" not in request.files: return jsonify({"error":"file missing"}), 400
    fp = "/tmp/video.mp4"; request.files["file"].save(fp)
    media_id = upload_video(fp)
    tweet_id = post_tweet(text, media_id)
    return jsonify({"status":"ok", "tweet_id": tweet_id})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
