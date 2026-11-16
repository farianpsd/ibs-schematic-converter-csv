import os
import io
import re
import json
import time
import uuid
import base64
import threading
import requests
from flask import Flask, render_template, request, jsonify
from PIL import Image

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
CACHED_MODELS = []
JOBS = {}  # Menyimpan status & hasil background job di memori

SYSTEM_INSTRUCTION = """
Kamu adalah Senior RF Engineer IBS (In-Building System) yang bertugas mengekstrak topologi jaringan dari skematik multi-lantai menjadi JSON terstruktur untuk setiap titik antena indoor dengan ketelitian tinggi.

ATURAN EKSTRAKSI (WAJIB JSON ARRAY VALID):
1. HANYA keluarkan JSON array murni (tanpa markdown ```json dan tanpa teks tambahan) dengan struktur berikut:
[
  {
    "a": "LT4-AO-1",
    "f": "LANTAI 4",
    "t": "Antenna Omni",
    "c": [["1/2", 10], ["1/2", 50]],
    "d": ["S3", "S3", "S3"]
  }
]

2. ATURAN PENELUSURAN JALUR (END-TO-END PER ANTENA):
   - HANYA ekstrak titik Antena Indoor distribusi (AO-1, AO-2, AP-1, dst.). DILARANG memasukkan Repeater, MCM/Combiner/HC, Splitter, Coupler, kabel riser (TO LT / FROM LT), maupun Antenna Donor sebagai baris antena.
   - Gabungkan semua gambar skematik menjadi SATU rangkaian sistem distribusi yang terhubung. Telusuri jalur MULAI DARI port output sumber utama (MCM/Combiner/HC atau Repeater) sampai ke masing-masing titik antena:
     * TITIK AWAL (WAJIB): Sertakan segmen kabel pertama tepat setelah port output MCM/Combiner/HC (misal ["1/2", 10]) dan splitter percabangan utama pertama yang membagi jalur ke tiap lantai. Perhatikan dengan sangat teliti jika ada 2 garis riser vertikal yang digambar berdekatan/sejajar turun ke lantai berbeda (misal 1 cabang ke Lantai 4 + 2 garis riser turun ke Lantai 3 & Lantai 2 = 3 output -> wajib dicatat sebagai "S3", BUKAN "S2").
     * PENCOCOKAN RISER LINTAS GAMBAR: Pastikan setiap label "FROM LT X" pada gambar lantai tujuan dicocokkan persis dengan label "TO LT Y" yang sesuai pada gambar sebelumnya (misal jalur ke Lantai 3 memakai riser ["7/8", 30] dan jalur ke Lantai 2 memakai riser ["7/8", 40]; JANGAN tertukar panjang riser antar-lantai).
     * TELUSURI CABANG SPLITTER AKHIR SATU PER SATU: Pada lantai dengan beberapa splitter akhir (misal kombinasi 4-Way dan 3-Way secara berurutan), ikuti garis masing-masing antena ke simbol splitternya secara individual—jangan mengelompokkan berdasarkan urutan nomor antena.
   - "a": kode titik antena sesuai label gambar (gunakan awalan lantai seperti LT4-AO-1, LT3-AO-1, LT2-AO-1 jika nomor berulang antar-lantai).
   - "f": posisi lantai dengan huruf kapital (contoh: LANTAI 4, LANTAI 3, LANTAI 2).
   - "t": jenis antena dengan rapi (Antenna Omni atau Antenna Planar).
   - "c": list seluruh segmen kabel [ukuran, panjang_meter] mulai dari output MCM/Combiner/HC hingga antena. Ukuran hanya boleh: "1/2", "7/8", atau "1-1/4".
   - "d": list seluruh kode perangkat pasif G-Wave mulai dari splitter pertama setelah output MCM/Combiner/HC hingga antena (JANGAN masukkan MCM/Combiner/HC):
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
                if n == "gemini-flash-latest":
                    return 0
                if "3." in n and "flash" in n:
                    return 1
                if "pro" in n:
                    return 2
                return 3

            valid_names.sort(key=model_priority)
            if valid_names:
                CACHED_MODELS = valid_names
                return CACHED_MODELS
    except Exception:
        pass

    return ["gemini-flash-latest", "gemini-pro-latest"]

def encode_image_jpeg_b64(img):
    img = img.convert("RGB")
    # Resolusi tinggi 2200px agar seluruh angka panjang kabel & garis sejajar terbaca tajam
    img.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
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

def process_extraction_job(job_id, b64_images, user_message):
    try:
        JOBS[job_id]["status"] = "processing"
        JOBS[job_id]["step"] = "Menghubungi Gemini Vision untuk analisis topologi presisi tinggi..."

        prompt_text = (
            f"{SYSTEM_INSTRUCTION}\n\n"
            f"Terdapat {len(b64_images)} gambar skematik IBS yang saling terhubung. "
            f"Telusuri seluruh jalur distribusi secara cermat mulai dari output MCM/Combiner/HC (termasuk segmen kabel awal dan splitter pertama) "
            f"menuju setiap titik antena indoor di semua lantai dan keluarkan JSON array lengkap.\n"
            f"Catatan tambahan user: {user_message}"
        )

        parts = [{"inline_data": {"mime_type": "image/jpeg", "data": b64}} for b64 in b64_images]
        parts.append({"text": prompt_text})

        candidate_models = get_available_gemini_models()
        raw_text = None
        last_error = None

        for model_name in candidate_models:
            JOBS[job_id]["step"] = f"Menganalisis jalur RF menggunakan model {model_name} (resolusi tinggi)..."
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

            for attempt in range(3):
                try:
                    # Timeout longgar 240 detik karena berjalan di background thread
                    resp = requests.post(url, json=current_payload, timeout=240)
                    if resp.status_code == 200:
                        data = resp.json()
                        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        if raw_text:
                            break
                    elif resp.status_code == 400 and "thinking" in resp.text.lower():
                        current_payload = payload_basic
                        continue
                    elif resp.status_code in (429, 503):
                        last_error = f"HTTP {resp.status_code} ({model_name}): Server sibuk, mencoba ulang..."
                        time.sleep(5 * (attempt + 1))
                        continue
                    else:
                        last_error = f"HTTP {resp.status_code} ({model_name}): {resp.text[:300]}"
                        break
                except Exception as err:
                    last_error = f"{model_name}: {str(err)}"
                    time.sleep(2)

            if raw_text:
                break

        if not raw_text:
            raise Exception(f"Gagal mengekstrak skematik: {last_error}")

        JOBS[job_id]["step"] = "Menghitung Link Budget G-Wave secara eksak..."
        csv_result = calculate_rf_csv_from_json(raw_text)

        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["csv"] = csv_result
        JOBS[job_id]["image_count"] = len(b64_images)

    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["message"] = f"Error: {str(e)}"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
@app.route('/api/chat', methods=['POST'])
def chat():
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

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "queued",
        "step": "Menyiapkan gambar resolusi tinggi...",
        "created_at": time.time()
    }

    worker = threading.Thread(
        target=process_extraction_job,
        args=(job_id, b64_images, user_message),
        daemon=True
    )
    worker.start()

    return jsonify({
        'status': 'processing',
        'job_id': job_id,
        'step': JOBS[job_id]["step"]
    })

@app.route('/api/job/<job_id>', methods=['GET'])
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'message': 'Job tidak ditemukan atau server baru saja di-reload.'}), 404

    if job["status"] == "done":
        csv_data = job["csv"]
        return jsonify({
            'status': 'success',
            'image_count': job.get("image_count", 1),
            'csv': csv_data,
            'reply': csv_data,
            'response': csv_data
        })
    elif job["status"] == "error":
        return jsonify({
            'status': 'error',
            'message': job.get("message", "Terjadi kesalahan"),
            'response': job.get("message", "Terjadi kesalahan")
        })
    else:
        return jsonify({
            'status': 'processing',
            'step': job.get("step", "Sedang memproses...")
        })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
