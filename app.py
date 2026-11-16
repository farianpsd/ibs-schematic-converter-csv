import os
import io
import re
import json
import time
import base64
import requests
from flask import Flask, render_template, request, jsonify
from PIL import Image

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
CACHED_MODELS = []

SYSTEM_INSTRUCTION = """
Kamu adalah Senior RF Engineer IBS (In-Building System) yang membaca topologi jaringan dari skematik multi-lantai menjadi JSON ringkas untuk setiap titik antena indoor.

ATURAN EKSTRAKSI (WAJIB JSON ARRAY VALID):
1. HANYA keluarkan JSON array murni (tanpa markdown ```json dan tanpa teks lain) dengan format key ringkas berikut:
[
  {"a":"LT4-AO-1","f":"LANTAI 4","t":"Antenna Omni","c":[["1/2",10],["1/2",50]],"d":["S3","S3","S3"]}
]

Keterangan key:
- "a": kode titik antena (gunakan awalan lantai seperti LT4-AO-1, LT3-AO-1, LT2-AO-1 jika nomor berulang antar-lantai).
- "f": lantai huruf kapital (contoh: LANTAI 4, LANTAI 3, LANTAI 2).
- "t": tipe antena (Antenna Omni atau Antenna Planar).
- "c": list segmen kabel [ukuran, panjang_meter] mulai dari output MCM/Combiner/HC sampai antena. Ukuran hanya "1/2", "7/8", atau "1-1/4".
- "d": list kode perangkat pasif G-Wave mulai dari splitter pertama setelah output MCM/Combiner/HC sampai antena (JANGAN masukkan MCM/Combiner/HC):
  * "S2" = Power Splitter 2-Way
  * "S3" = Power Splitter 3-Way
  * "S4" = Power Splitter 4-Way
  * "C5_C" / "C5_T" = Coupler 5 dB (Coupled / Through)
  * "C6_C" / "C6_T" = Coupler 6 dB (Coupled / Through)
  * "C7_C" / "C7_T" = Coupler 7 dB (Coupled / Through)
  * "C10_C" / "C10_T" = Coupler 10 dB (Coupled / Through)
  * "C15_C" / "C15_T" = Coupler 15 dB (Coupled / Through)
  * "C20_C" / "C20_T" = Coupler 20 dB (Coupled / Through)
  * "C30_C" / "C30_T" = Coupler 30 dB (Coupled / Through)

2. ATURAN PENELUSURAN JALUR (END-TO-END PER ANTENA):
   - HANYA keluarkan titik Antena Indoor distribusi (AO-1, AO-2, AP-1, dst.). DILARANG memasukkan Repeater, MCM/Combiner/HC, Splitter, Coupler, kabel riser (TO LT / FROM LT), maupun Antenna Donor.
   - TITIK AWAL (WAJIB): Sertakan segmen kabel pertama tepat setelah port output MCM/Combiner/HC (misal ["1/2",10]) dan splitter percabangan utama pertama yang membagi jalur ke tiap lantai (perhatikan dengan teliti jika ada 2 garis riser sejajar turun ke lantai berbeda -> total 3 cabang = "S3").
   - PENCOCOKAN RISER LINTAS GAMBAR: Cocokkan label "FROM LT X" dengan label "TO LT X" yang sesuai pada gambar sebelumnya (misal ke Lantai 3 memakai riser ["7/8",30] dan ke Lantai 2 memakai riser ["7/8",40]; JANGAN tertukar).
   - TELUSURI CABANG AKHIR SATU PER SATU: Pada lantai dengan kombinasi splitter 4-Way dan 3-Way berurutan, ikuti garis tiap antena ke simbol splitternya secara individual.
"""

CABLE_LOSS_PER_M = {
    "1/2": 0.07,
    "0.5": 0.07,
    "7/8": 0.04,
    "0.875": 0.04,
    "1-1/4": 0.03,
    "1 1/4": 0.03,
    "1.25": 0.03,
}

DEVICE_LOSS_DB = {
    "S2": 3.5, "SPLITTER_2WAY": 3.5,
    "S3": 5.5, "SPLITTER_3WAY": 5.5,
    "S4": 6.5, "SPLITTER_4WAY": 6.5,
    "C5_C": 5.5, "C5_T": 1.8,
    "C6_C": 6.5, "C6_T": 1.5,
    "C7_C": 7.5, "C7_T": 1.2,
    "C10_C": 10.5, "C10_T": 0.7,
    "C15_C": 15.4, "C15_T": 0.4,
    "C20_C": 20.3, "C20_T": 0.3,
    "C30_C": 30.3, "C30_T": 0.2,
}

def get_available_gemini_models():
    global CACHED_MODELS
    if CACHED_MODELS:
        return CACHED_MODELS

    base_host = "generativelanguage.googleapis.com"
    list_url = f"https://{base_host}/v1beta/models?key={GEMINI_API_KEY}"
    blacklist = [
        "embedding", "aqa", "imagen", "veo", "tts", "audio",
        "robotics", "computer-use", "image", "2.0", "2.5", "lite"
    ]
    try:
        resp = requests.get(list_url, timeout=10)
        if resp.status_code == 200:
            models_data = resp.json().get("models", [])
            valid_names = []
            for m in models_data:
                methods = m.get("supportedGenerationMethods", [])
                name = m.get("name", "").replace("models/", "")
                if "generateContent" in methods and "gemini" in name:
                    if not any(bad in name for bad in blacklist):
                        valid_names.append(name)

            def model_priority(n):
                if n == "gemini-flash-latest":
                    return 0
                if n == "gemini-3.8-flash":
                    return 1
                if "pro" in n and "latest" in n:
                    return 2
                if "3." in n and "flash" in n:
                    return 3
                return 4

            valid_names.sort(key=model_priority)
            if valid_names:
                CACHED_MODELS = valid_names
                return CACHED_MODELS
    except Exception:
        pass

    return ["gemini-flash-latest", "gemini-3.8-flash", "gemini-pro-latest"]

def encode_image_jpeg_b64(img):
    img = img.convert("RGB")
    img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def parse_uploaded_images(req):
    b64_images = []
    if req.files:
        uploaded_files = req.files.getlist('files')
        if not uploaded_files or all(f.filename == '' for f in uploaded_files):
            single = req.files.get('file')
            if single and single.filename != '':
                uploaded_files = [single]
        for f in uploaded_files:
            if f and f.filename != '':
                img = Image.open(f.stream)
                b64_images.append(encode_image_jpeg_b64(img))
    elif req.is_json:
        data = req.get_json(silent=True) or {}
        b64_list = data.get('images', [])
        if not b64_list and data.get('image'):
            b64_list = [data.get('image')]
        for b64_str in b64_list:
            if not b64_str:
                continue
            if "," in b64_str:
                b64_str = b64_str.split(",", 1)[1]
            img_bytes = base64.b64decode(b64_str)
            img = Image.open(io.BytesIO(img_bytes))
            b64_images.append(encode_image_jpeg_b64(img))
    return b64_images

def calculate_rf_csv_from_json(raw_text):
    text = raw_text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    start_idx = text.find("[")
    end_idx = text.rfind("]")
    if start_idx == -1 or end_idx == -1:
        raise ValueError("Respons AI tidak mengandung JSON array yang valid.")

    items = json.loads(text[start_idx:end_idx + 1])
    rows = ["TITIK_ANTENA,LANTAI_POSISI,TIPE_ANTENA,FEEDER_LOSS_DB,SPLITTER_LOSS_DB,TOTAL_LOSS_DB"]

    for item in items:
        titik = str(item.get("a") or item.get("titik_antena", "")).strip().replace(",", "")
        lantai = str(item.get("f") or item.get("lantai_posisi", "")).strip().upper().replace(",", "")
        tipe = str(item.get("t") or item.get("tipe_antena", "Antenna Omni")).strip().replace(",", "")

        theo_feeder = 0.0
        for c in (item.get("c") or item.get("cables") or []):
            if isinstance(c, list) and len(c) >= 2:
                sz = str(c[0]).replace('"', '').strip()
                length_m = float(c[1])
            elif isinstance(c, dict):
                sz = str(c.get("size", "1/2")).replace('"', '').strip()
                length_m = float(c.get("length_m", 0))
            else:
                continue
            rate = CABLE_LOSS_PER_M.get(sz, 0.07)
            theo_feeder += length_m * rate

        feeder_loss = round(theo_feeder * 1.12, 2)

        devices = item.get("d") or item.get("devices") or []
        splitter_loss = 0.0
        for dev in devices:
            key = str(dev).strip().upper()
            splitter_loss += DEVICE_LOSS_DB.get(key, 0.0)
        splitter_loss = round(splitter_loss, 2)

        connector_loss = (len(devices) * 0.20) + 0.50
        total_loss = round(feeder_loss + splitter_loss + connector_loss, 2)

        rows.append(f"{titik},{lantai},{tipe},{feeder_loss:.2f},{splitter_loss:.1f},{total_loss:.2f}")

    return "\n".join(rows)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        if not GEMINI_API_KEY:
            msg = "GEMINI_API_KEY belum diset di file WSGI PythonAnywhere."
            return jsonify({'status': 'error', 'response': msg, 'message': msg}), 500

        b64_images = parse_uploaded_images(request)
        if not b64_images:
            msg = 'Silakan unggah atau tempel (Ctrl+V) 1 atau lebih gambar skematik IBS terlebih dahulu.'
            return jsonify({'status': 'error', 'response': msg, 'message': msg}), 400

        user_message = (
            request.form.get('message', '')
            if not request.is_json
            else (request.get_json(silent=True) or {}).get('message', '')
        )

        prompt_text = (
            f"{SYSTEM_INSTRUCTION}\n\n"
            f"Terdapat {len(b64_images)} gambar skematik IBS yang saling terhubung. "
            f"Ekstrak jalur kabel dan perangkat pasif untuk SELURUH titik antena indoor dari semua lantai menjadi 1 JSON array ringkas sesuai format di atas.\n"
            f"Catatan tambahan user: {user_message}"
        )

        parts = [{"inline_data": {"mime_type": "image/jpeg", "data": b64}} for b64 in b64_images]
        parts.append({"text": prompt_text})

        candidate_models = get_available_gemini_models()
        raw_text = None
        last_error = None
        start_time = time.time()

        for model_name in candidate_models:
            elapsed = time.time() - start_time
            if elapsed > 75:
                break

            base_host = "generativelanguage.googleapis.com"
            url = f"https://{base_host}/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"

            payload_with_thinking = {
                "contents": [{"parts": parts}],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                    "thinkingConfig": {"thinkingLevel": "LOW"}
                }
            }
            payload_basic = {
                "contents": [{"parts": parts}],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json"
                }
            }

            current_payload = payload_with_thinking if "3." in model_name else payload_basic
            per_req_timeout = min(40, max(20, int(78 - elapsed)))

            try:
                resp = requests.post(url, json=current_payload, timeout=per_req_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    if raw_text:
                        break
                elif resp.status_code == 400 and "thinking" in resp.text.lower():
                    resp2 = requests.post(url, json=payload_basic, timeout=per_req_timeout)
                    if resp2.status_code == 200:
                        raw_text = resp2.json()["candidates"][0]["content"]["parts"][0]["text"]
                        if raw_text:
                            break
                else:
                    last_error = f"HTTP {resp.status_code} ({model_name}): {resp.text[:250]}"
            except Exception as err:
                last_error = f"{model_name}: {str(err)}"

        if not raw_text:
            raise Exception(f"Detail error: {last_error}")

        csv_result = calculate_rf_csv_from_json(raw_text)

        return jsonify({
            'status': 'success',
            'image_count': len(b64_images),
            'csv': csv_result,
            'reply': csv_result,
            'response': csv_result
        })

    except Exception as e:
        err_msg = f"Error: {str(e)}"
        return jsonify({
            'status': 'error',
            'message': err_msg,
            'response': err_msg
        }), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
