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

# ==============================================================================
# STANDAR SPESIFIKASI TEKNIS PERANGKAT RF MEREK G-WAVE & TOLERANSI LAPANGAN
# ==============================================================================
FEEDER_DB_PER_M = {
    "1/2": 0.07,
    "7/8": 0.04,
    "1 1/4": 0.03,
}
BENDING_MARGIN = 1.12  # +12% margin lapangan (rentang 10% - 15%)

SPLITTER_DB = {
    2: 3.5,  # G-Wave Splitter 2-Way
    3: 5.5,  # G-Wave Splitter 3-Way
    4: 6.5,  # G-Wave Splitter 4-Way
}

COUPLER_COUPLED_DB = {
    5: 5.5, 6: 6.5, 7: 7.5, 10: 10.5, 15: 15.4, 20: 20.3, 30: 30.3
}
COUPLER_THROUGH_DB = {
    5: 1.8, 6: 1.5, 7: 1.2, 10: 0.7, 15: 0.4, 20: 0.3, 30: 0.2
}

SYSTEM_INSTRUCTION = """
Kamu adalah Senior RF Engineer IBS (In-Building System). Tugasmu HANYA membaca gambar skematik multi-lantai dan mengekstrak daftar urutan segmen kabel serta perangkat pasif yang dilewati dari port output MCM/Combiner menuju setiap titik Antena Indoor dalam format JSON valid.

ATURAN EKSTRAKSI TOPOLOGI:
1. HANYA ekstrak titik Antena Indoor distribusi (seperti AO-1, AO-2, AP-1, dst.).
2. DILARANG memasukkan Repeater, MCM/Combiner, Splitter, Coupler, kabel riser antar-lantai (TO LT / FROM LT), maupun Antenna Donor sebagai item antena.
3. Gabungkan seluruh gambar skematik menjadi SATU sistem terhubung. Telusuri jalur MULAI DARI port output MCM/Combiner melewati seluruh splitter tingkat 1, tingkat 2, kabel riser vertikal, hingga ke masing-masing antena di setiap lantai.
4. Untuk setiap titik antena, catat SEMUA segmen kabel dan SEMUA perangkat pasif (Splitter / Coupler) yang dilewati secara berurutan dari output MCM sampai ujung antena:
   - `id`: kode antena (jika antar-lantai memakai nomor sama, beri awalan lantai seperti "LT4-AO-1", "LT3-AO-1", agar unik).
   - `floor`: nama lantai dalam huruf kapital (contoh: "LANTAI 4", "LANTAI 3", "LANTAI 2").
   - `antenna_type`: jenis antena (contoh: "Antenna Omni", "Antenna Planar").
   - `cables`: list seluruh segmen kabel yang dilewati dari output MCM hingga antena tersebut, masing-masing berisi `{"size": "1/2" atau "7/8" atau "1 1/4", "length_m": angka_meter}`.
   - `passives`: list seluruh perangkat Splitter/Coupler setelah port output MCM hingga antena tersebut (JANGAN masukkan MCM/Combiner ke dalam list ini):
     * Untuk Splitter: hitung TOTAL seluruh jalur kabel yang keluar dari sisi output komponen tersebut (termasuk garis riser vertikal yang sejajar/berdekatan maupun cabang horizontal), lalu tulis `{"type": "splitter", "ways": 2 atau 3 atau 4}`.
     * Untuk Coupler: tulis `{"type": "coupler", "value_db": angka_nilai_coupler, "port": "coupled" atau "through"}`.

FORMAT KELUARAN WAJIB JSON OBJECT MURNI TANPA MARKDOWN:
{
  "antennas": [
    {
      "id": "LT4-AO-1",
      "floor": "LANTAI 4",
      "antenna_type": "Antenna Omni",
      "cables": [
        {"size": "1/2", "length_m": 10},
        {"size": "1/2", "length_m": 10}
      ],
      "passives": [
        {"type": "splitter", "ways": 3},
        {"type": "splitter", "ways": 3}
      ]
    }
  ]
}
"""

def calculate_link_budget_csv(raw_text):
    text = raw_text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx == -1 or end_idx == -1:
        raise ValueError("Respons AI tidak mengandung struktur JSON yang valid.")

    parsed = json.loads(text[start_idx:end_idx + 1])
    antennas = parsed.get("antennas", [])
    if not antennas:
        raise ValueError("Tidak ada titik antena indoor yang terdeteksi dari gambar.")

    csv_lines = [
        "TITIK_ANTENA,LANTAI_POSISI,TIPE_ANTENA,FEEDER_LOSS_DB,SPLITTER_LOSS_DB,TOTAL_LOSS_DB"
    ]

    for item in antennas:
        ant_id = str(item.get("id", "")).strip().replace(",", "")
        floor = str(item.get("floor", "")).strip().upper().replace(",", "")
        ant_type = str(item.get("antenna_type", "Antenna Omni")).strip().replace(",", "")

        # 1. Hitung Feeder Loss + Margin Bending 12%
        theo_feeder = 0.0
        for c in item.get("cables", []):
            raw_size = str(c.get("size", "1/2")).replace('"', '').replace("”", "").strip()
            if "7/8" in raw_size:
                size_key = "7/8"
            elif "1/4" in raw_size:
                size_key = "1 1/4"
            else:
                size_key = "1/2"
            length_m = float(c.get("length_m", 0))
            theo_feeder += length_m * FEEDER_DB_PER_M[size_key]

        feeder_loss = round(theo_feeder * BENDING_MARGIN, 2)

        # 2. Hitung Splitter / Coupler Loss Standar G-Wave
        splitter_loss = 0.0
        passives = item.get("passives", [])
        valid_passive_count = 0

        for p in passives:
            ptype = str(p.get("type", "")).lower().strip()
            if ptype == "splitter":
                ways = int(p.get("ways", 2))
                splitter_loss += SPLITTER_DB.get(ways, 3.5)
                valid_passive_count += 1
            elif ptype == "coupler":
                val = int(p.get("value_db", 6))
                port = str(p.get("port", "coupled")).lower().strip()
                if "through" in port or "main" in port:
                    splitter_loss += COUPLER_THROUGH_DB.get(val, 1.5)
                else:
                    splitter_loss += COUPLER_COUPLED_DB.get(val, 6.5)
                valid_passive_count += 1

        splitter_loss = round(splitter_loss, 2)

        # 3. Hitung Dynamic Connector Loss + Jumper & Total Loss
        conn_jumper_loss = (valid_passive_count * 0.20) + 0.50
        total_loss = round(feeder_loss + splitter_loss + conn_jumper_loss, 2)

        csv_lines.append(
            f"{ant_id},{floor},{ant_type},{feeder_loss:.2f},{splitter_loss:.2f},{total_loss:.2f}"
        )

    return "\n".join(csv_lines)

def get_available_gemini_models():
    global CACHED_MODELS
    if CACHED_MODELS:
        return CACHED_MODELS

    base_host = "generativelanguage.googleapis.com"
    list_url = f"https://{base_host}/v1beta/models?key={GEMINI_API_KEY}"
    blacklist = [
        "embedding", "aqa", "imagen", "veo", "tts", "audio",
        "robotics", "computer-use", "image", "2.0", "2.5"
    ]
    try:
        resp = requests.get(list_url, timeout=15)
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
                if n == "gemini-flash-latest":
                    return 1
                if "3." in n and "flash" in n:
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
    img.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
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
            f"Ekstrak seluruh jalur distribusi mulai dari port output MCM/Combiner menuju setiap titik antena indoor di semua lantai ke dalam JSON sesuai skema di atas.\n"
            f"Catatan tambahan user: {user_message}"
        )

        parts = [{"inline_data": {"mime_type": "image/jpeg", "data": b64}} for b64 in b64_images]
        parts.append({"text": prompt_text})

        candidate_models = get_available_gemini_models()
        raw_text = None
        last_error = None

        for model_name in candidate_models:
            base_host = "generativelanguage.googleapis.com"
            url = f"https://{base_host}/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"

            payload_with_thinking = {
                "contents": [{"parts": parts}],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                    "thinkingConfig": {"thinkingLevel": "HIGH"}
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

            for attempt in range(4):
                try:
                    resp = requests.post(url, json=current_payload, timeout=110)
                    if resp.status_code == 200:
                        data = resp.json()
                        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        if raw_text:
                            break
                    elif resp.status_code == 400 and ("thinking" in resp.text.lower() or "json" in resp.text.lower()):
                        current_payload = {
                            "contents": [{"parts": parts}],
                            "generationConfig": {"temperature": 0.1}
                        }
                        continue
                    elif resp.status_code == 503:
                        last_error = f"HTTP 503 ({model_name}): Server sibuk, mencoba ulang..."
                        time.sleep(2 * (attempt + 1))
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

        csv_result = calculate_link_budget_csv(raw_text)

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
