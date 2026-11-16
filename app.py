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
Kamu adalah Senior RF Engineer IBS (In-Building System) yang bertugas membaca topologi jaringan dari skematik multi-lantai menjadi JSON terstruktur untuk setiap titik antena indoor.

ATURAN EKSTRAKSI (WAJIB JSON VALID):
1. HANYA keluarkan JSON murni dengan struktur array berikut (tanpa markdown ```json dan tanpa teks tambahan):
[
  {
    "titik_antena": "LT4-AO-1",
    "lantai_posisi": "LANTAI 4",
    "tipe_antena": "Antenna Omni",
    "cables": [
      {"size": "1/2", "length_m": 10},
      {"size": "1/2", "length_m": 50}
    ],
    "devices": ["SPLITTER_3WAY", "SPLITTER_3WAY", "SPLITTER_3WAY"]
  }
]

2. ATURAN PEMFILTERAN & PENELUSURAN JALUR (END-TO-END PER ANTENA):
   - HANYA ekstrak titik Antena Indoor distribusi (seperti AO-1, AO-2, AP-1, dst.). DILARANG memasukkan Repeater, MCM/Combiner/HC, Splitter, Coupler, kabel riser (TO LT / FROM LT), maupun Antenna Donor sebagai baris antena.
   - Gabungkan semua gambar skematik menjadi SATU sistem terhubung. Telusuri jalur MULAI DARI port output sumber utama (MCM/Combiner/HC atau Repeater) sampai ke masing-masing antena:
     * TITIK AWAL (WAJIB): Sertakan segmen kabel pertama tepat setelah output MCM/Combiner/HC (misal 1/2" 10m) dan splitter percabangan utama pertama yang membagi jalur ke tiap lantai (perhatikan dengan teliti jika ada 2 garis riser sejajar turun ke lantai berbeda -> total 3 cabang = SPLITTER_3WAY).
     * PENCOCOKAN RISER LINTAS GAMBAR: Cocokkan label "FROM LT X" dengan label "TO LT X" yang sesuai pada gambar sebelumnya (misal ke Lantai 3 memakai riser 7/8" 30m dan ke Lantai 2 memakai riser 7/8" 40m; JANGAN tertukar antar-lantai).
     * TELUSURI CABANG AKHIR SATU PER SATU: Pada lantai dengan beberapa splitter akhir (misal kombinasi 4-Way dan 3-Way secara berurutan), ikuti garis masing-masing antena ke simbol splitternya secara individual.
   - `titik_antena`: gunakan awalan lantai seperti LT4-AO-1, LT3-AO-1, LT2-AO-1 jika nomor antena berulang antar-lantai.
   - `lantai_posisi`: huruf kapital (contoh: LANTAI 4, LANTAI 3, LANTAI 2).
   - `tipe_antena`: tulis rapi (contoh: Antenna Omni, Antenna Planar).
   - `cables`: daftar seluruh segmen kabel yang dilewati dari output MCM/Combiner/HC hingga antena tersebut. Nilai `size` hanya boleh: "1/2", "7/8", atau "1-1/4". Nilai `length_m` berupa angka panjang dalam meter.
   - `devices`: daftar seluruh perangkat pasif G-Wave yang dilewati mulai dari splitter pertama setelah MCM/Combiner/HC hingga antena tersebut (jangan masukkan MCM/Combiner/HC). Gunakan kode standar berikut:
     * "SPLITTER_2WAY"
     * "SPLITTER_3WAY"
     * "SPLITTER_4WAY"
     * "COUPLER_5_COUPLED" atau "COUPLER_5_THROUGH"
     * "COUPLER_6_COUPLED" atau "COUPLER_6_THROUGH"
     * "COUPLER_7_COUPLED" atau "COUPLER_7_THROUGH"
     * "COUPLER_10_COUPLED" atau "COUPLER_10_THROUGH"
     * "COUPLER_15_COUPLED" atau "COUPLER_15_THROUGH"
     * "COUPLER_20_COUPLED" atau "COUPLER_20_THROUGH"
     * "COUPLER_30_COUPLED" atau "COUPLER_30_THROUGH"
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
    "SPLITTER_2WAY": 3.5,
    "SPLITTER_3WAY": 5.5,
    "SPLITTER_4WAY": 6.5,
    "COUPLER_5_COUPLED": 5.5,
    "COUPLER_5_THROUGH": 1.8,
    "COUPLER_6_COUPLED": 6.5,
    "COUPLER_6_THROUGH": 1.5,
    "COUPLER_7_COUPLED": 7.5,
    "COUPLER_7_THROUGH": 1.2,
    "COUPLER_10_COUPLED": 10.5,
    "COUPLER_10_THROUGH": 0.7,
    "COUPLER_15_COUPLED": 15.4,
    "COUPLER_15_THROUGH": 0.4,
    "COUPLER_20_COUPLED": 20.3,
    "COUPLER_20_THROUGH": 0.3,
    "COUPLER_30_COUPLED": 30.3,
    "COUPLER_30_THROUGH": 0.2,
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
                if n == "gemini-3.8-flash":
                    return 0
                if "3." in n and "flash" in n:
                    return 1
                if n == "gemini-flash-latest":
                    return 2
                if "pro" in n:
                    return 3
                return 4

            valid_names.sort(key=model_priority)
            if valid_names:
                CACHED_MODELS = valid_names
                return CACHED_MODELS
    except Exception:
        pass

    return ["gemini-3.8-flash", "gemini-flash-latest", "gemini-pro-latest"]

def encode_image_jpeg_b64(img):
    img = img.convert("RGB")
    img.thumbnail((1700, 1700), Image.Resampling.LANCZOS)
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
        raise ValueError("Respons AI tidak mengandung struktur JSON array yang valid.")

    items = json.loads(text[start_idx:end_idx + 1])
    rows = ["TITIK_ANTENA,LANTAI_POSISI,TIPE_ANTENA,FEEDER_LOSS_DB,SPLITTER_LOSS_DB,TOTAL_LOSS_DB"]

    for item in items:
        titik = str(item.get("titik_antena", "")).strip().replace(",", "")
        lantai = str(item.get("lantai_posisi", "")).strip().upper().replace(",", "")
        tipe = str(item.get("tipe_antena", "Antenna Omni")).strip().replace(",", "")

        theo_feeder = 0.0
        for c in item.get("cables", []):
            sz = str(c.get("size", "1/2")).replace('"', '').strip()
            length_m = float(c.get("length_m", 0))
            rate = CABLE_LOSS_PER_M.get(sz, 0.07)
            theo_feeder += length_m * rate

        feeder_loss = round(theo_feeder * 1.12, 2)

        devices = item.get("devices", [])
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
            f"Ekstrak jalur kabel dan perangkat pasif untuk SELURUH titik antena indoor dari semua lantai menjadi 1 JSON array sesuai format di atas.\n"
            f"Catatan tambahan user: {user_message}"
        )

        parts = [{"inline_data": {"mime_type": "image/jpeg", "data": b64}} for b64 in b64_images]
        parts.append({"text": prompt_text})

        candidate_models = get_available_gemini_models()
        raw_text = None
        last_error = None
        start_time = time.time()

        for model_name in candidate_models:
            if time.time() - start_time > 80:
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

            for attempt in range(2):
                try:
                    resp = requests.post(url, json=current_payload, timeout=75)
                    if resp.status_code == 200:
                        data = resp.json()
                        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        if raw_text:
                            break
                    elif resp.status_code == 400 and "thinking" in resp.text.lower():
                        current_payload = payload_basic
                        continue
                    elif resp.status_code == 429:
                        last_error = f"Kuota API Gemini ({model_name}) penuh sementara (HTTP 429)."
                        if attempt == 0 and (time.time() - start_time < 60):
                            time.sleep(4)
                            continue
                        break
                    elif resp.status_code == 503:
                        last_error = f"HTTP 503 ({model_name}): Server sibuk."
                        time.sleep(2)
                        continue
                    else:
                        last_error = f"HTTP {resp.status_code} ({model_name}): {resp.text[:300]}"
                        break
                except Exception as err:
                    last_error = f"{model_name}: {str(err)}"
                    break

            if raw_text:
                break

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
