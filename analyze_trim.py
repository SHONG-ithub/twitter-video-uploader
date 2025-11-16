import cv2
import numpy as np
import librosa
import subprocess
import tempfile
import os
from tqdm import tqdm

# ======================
# 設定パラメータ
# ======================
INPUT_FILE = "input.mp4"
OUTPUT_FILE = "output_trim.mp4"

SAFE_MODE = False  # ← True=非エロモード / False=エロモード
SAFE_MODE = True  # ← True=非エロモード / False=エロモード

FRAME_INTERVAL = 0.5   # 秒間隔でサンプリング
SKIN_THRESHOLD = 0.25  # 肌色率がこの値を超えたらエロ寄り
RMS_THRESHOLD_RATIO = 2.0  # 平均音量の何倍以上で「喘ぎ声」
TRIM_LENGTH = 20       # 出力秒数（最大値）

# 黒背景タイトル除去用
BLACK_THRESHOLD = 20       # 平均明度がこの値以下を「黒」とみなす
BLACK_CONTINUE_SEC = 2.0   # 黒が何秒以上続いたらタイトル判定

# シーン変化検出用
SCENE_DIFF_THRESHOLD = 0.35   # どの程度のフレーム差をシーン変化とみなすか
SCENE_ADJUST_MARGIN = 2.0     # 終端補正範囲（秒）

# ======================
# 肌色＋明度解析
# ======================
def analyze_skin_ratio_and_brightness(video_path):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frame_count / fps
    interval = int(fps * FRAME_INTERVAL)

    ratios, brightness = [], []
    for i in tqdm(range(0, frame_count, interval), desc="解析中（肌色率・明度）"):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret:
            break
        img_ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        mask = cv2.inRange(img_ycrcb, (0, 133, 77), (255, 173, 127))
        ratio = np.sum(mask > 0) / mask.size
        ratios.append(ratio)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness.append(np.mean(gray))
    cap.release()
    return np.array(ratios), np.array(brightness), duration

# ======================
# 音声解析
# ======================
def analyze_audio(video_path):
    tmp_wav = tempfile.NamedTemporaryFile(delete=False, suffix=".wav").name
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "44100", "-f", "wav", tmp_wav]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    y, sr = librosa.load(tmp_wav, sr=None)
    os.remove(tmp_wav)
    rms = librosa.feature.rms(y=y)[0]
    avg = np.mean(rms)
    loudness = rms > avg * RMS_THRESHOLD_RATIO
    loud_ratio = np.sum(loudness) / len(loudness)
    return loud_ratio, rms, sr

# ======================
# 黒背景タイトル検出
# ======================
def detect_black_title(brightness, duration):
    step = FRAME_INTERVAL
    dark = brightness < BLACK_THRESHOLD
    dark_len = 0
    black_end_time = 0.0
    for i, is_dark in enumerate(dark):
        if is_dark:
            dark_len += step
        else:
            if dark_len >= BLACK_CONTINUE_SEC:
                black_end_time = i * step
            break
    if black_end_time > 0:
        print(f"🕶️ 黒背景タイトル検出: {black_end_time:.1f} 秒までスキップ")
    return black_end_time

# ======================
# シーン変化検出
# ======================
def detect_scene_changes(video_path):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    interval = int(fps * 0.5)
    prev_hist = None
    changes = []

    for i in range(0, frame_count, interval):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret:
            break
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        if prev_hist is not None:
            diff = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
            if diff > SCENE_DIFF_THRESHOLD:
                changes.append(i / fps)
        prev_hist = hist
    cap.release()
    print(f"🎞️ シーン変化 {len(changes)} 箇所検出")
    return changes

# ======================
# 区間検出（SAFE_MODEに応じて）
# ======================
def find_target_section(skin_ratios, brightness, duration, skip_until, safe_mode):
    step = FRAME_INTERVAL
    if safe_mode:
        condition = (skin_ratios < SKIN_THRESHOLD) & (brightness > BLACK_THRESHOLD)
    else:
        condition = (skin_ratios >= SKIN_THRESHOLD) & (brightness > BLACK_THRESHOLD)

    valid_indices = [i for i, ok in enumerate(condition) if ok and i * step > skip_until]
    if len(valid_indices) == 0:
        print("❌ 条件に合う区間が見つかりませんでした。")
        return None, None

    if safe_mode:
        start = valid_indices[0] * step
        end = min(start + TRIM_LENGTH, duration)
        print(f"✅ 非エロ区間: {start:.1f}〜{end:.1f} 秒")
    else:
        diffs = np.diff(valid_indices)
        segments = []
        seg_start = valid_indices[0]
        for i, d in enumerate(diffs):
            if d > 1:
                segments.append((seg_start, valid_indices[i]))
                seg_start = valid_indices[i + 1]
        segments.append((seg_start, valid_indices[-1]))
        seg = max(segments, key=lambda x: x[1] - x[0])
        start = seg[0] * step
        end = min(seg[1] * step + TRIM_LENGTH, duration)
        print(f"🔥 エロ区間: {start:.1f}〜{end:.1f} 秒")
    return start, end

# ======================
# 終端調整（シーン境界で切る）
# ======================
def adjust_to_scene_end(start, end, scene_changes):
    for t in scene_changes:
        if start < t < end:
            # 終了時間の少し前にシーン切り替えがあればそこまでにする
            if end - t < SCENE_ADJUST_MARGIN:
                print(f"✂️ シーン境界に合わせて終了時刻を {t:.1f} 秒に調整")
                return start, t
    return start, end

# ======================
# ffmpegトリミング
# ======================
def trim_video(video_path, start, end, out_path):
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", video_path, "-c", "copy", out_path]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print(f"🎬 出力完了: {out_path}")

def process_and_trim_video(video_url: str, work_dir: str = "/tmp") -> dict:
    """
    FANZAサンプル動画URLを受け取り、
    - ffmpeg でローカルにDL
    - 解析＆SAFE_MODEで非エロ区間を決定
    - ffmpegでトリミング
    を行い、結果メタデータを dict で返す。
    """
    import uuid

    # 一時ファイルパスを決める
    base = f"fanza_{uuid.uuid4().hex}.mp4"
    input_path = os.path.join(work_dir, base)
    output_path = os.path.join(work_dir, base.replace(".mp4", "_trim.mp4"))

    # 動画をダウンロード（今の __main__ と同じやり方）
    print(f"▶ FANZA動画DL開始: {video_url}")
    cmd_dl = ["ffmpeg", "-y", "-i", video_url, "-c", "copy", input_path]
    subprocess.run(cmd_dl, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if not os.path.exists(input_path):
        raise RuntimeError("動画のダウンロードに失敗しました")

    mode = "非エロモード" if SAFE_MODE else "エロモード"
    print(f"▶ 解析開始: {input_path} ({mode})")

    # === 解析パート（既存処理をそのまま利用） ===
    skin_ratios, brightness, duration = analyze_skin_ratio_and_brightness(input_path)
    loud_ratio, rms, sr = analyze_audio(input_path)
    print(f"音量解析: loud_ratio={loud_ratio:.2f}")

    skip_until = detect_black_title(brightness, duration)
    scene_changes = detect_scene_changes(input_path)

    start, end = find_target_section(skin_ratios, brightness, duration, skip_until, SAFE_MODE)
    if start is None:
        print("❌ トリミング対象区間が見つかりませんでした")
        return {
            "status": "fail",
            "reason": "no_safe_section",
            "input_path": input_path,
        }

    start, end = adjust_to_scene_end(start, end, scene_changes)
    trim_video(input_path, start, end, output_path)

    print(f"✅ 出力完了: {output_path}")
    return {
        "status": "success",
        "input_path": input_path,
        "output_path": output_path,
        "start": start,
        "end": end,
        "duration": duration,
        "safe_mode": SAFE_MODE,
    }

# ======================
# メイン実行
# ======================
if __name__ == "__main__":
    import sys
    import requests

    if len(sys.argv) >= 3:
        video_url = sys.argv[1]
        local_path = sys.argv[2]
        print(f"▶ FANZA動画DL開始: {video_url}")
        subprocess.run(["ffmpeg", "-y", "-i", video_url, "-c", "copy", local_path])
        INPUT_FILE = local_path
        OUTPUT_FILE = local_path.replace(".mp4", "_trim.mp4")

    mode = "非エロモード" if SAFE_MODE else "エロモード"
    print(f"▶ 解析開始: {INPUT_FILE} ({mode})")

    skin_ratios, brightness, duration = analyze_skin_ratio_and_brightness(INPUT_FILE)
    loud_ratio, rms, sr = analyze_audio(INPUT_FILE)
    print(f"音量解析: loud_ratio={loud_ratio:.2f}")

    skip_until = detect_black_title(brightness, duration)
    scene_changes = detect_scene_changes(INPUT_FILE)

    start, end = find_target_section(skin_ratios, brightness, duration, skip_until, SAFE_MODE)
    if start is not None:
        start, end = adjust_to_scene_end(start, end, scene_changes)
        trim_video(INPUT_FILE, start, end, OUTPUT_FILE)
        print(f"✅ 出力完了: {OUTPUT_FILE}")
        with open("status.json", "w", encoding="utf-8") as f:
            f.write('{"status":"success","output":"'+OUTPUT_FILE+'"}')
    else:
        print("❌ トリミング失敗")
        with open("status.json", "w", encoding="utf-8") as f:
            f.write('{"status":"fail"}')
