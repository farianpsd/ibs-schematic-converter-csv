import os
import io
import re
import time
import base64
import requests
from flask import Flask, render_template, request, jsonify
from PIL import Image

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
CACHED_MODELS = []

SYSTEM_INSTRUCTION = """
Kamu adalah Senior RF Engineer IBS (In-Building System) yang bertugas mengekstrak seluruh titik antena indoor dari skematik multi-lantai menjadi 1 tabel CSV Link Budget per titik antena berdasarkan spesifikasi perangkat pasif RF merek G-WAVE dan toleransi instalasi lapangan.

ATURAN KELUARAN (STRICT CSV):
1. WAJIB HANYA MENGELUARKAN TEKS CSV MURNI.
2. DILARANG KERAS menggunakan simbol Markdown seperti bold (**), backtick (`), atau pagar (#) di mana pun.
3. BARIS PERTAMA (HEADER) WAJIB EXACT SAMA SEPERTI INI:
TITIK_ANTENA,LANTAI_POSISI,TIPE_ANTENA,FEEDER_LOSS_DB,SPLITTER_LOSS_DB,TOTAL_LOSS_DB

4. ATURAN PEMFILTERAN & BARIS DATA (HANYA TITIK ANTENA INDOOR):
   - HANYA keluarkan baris untuk titik Antena Indoor distribusi (seperti AO-1, AO-2, AP-1, dst.).
   - DILARANG KERAS mengeluarkan baris untuk Repeater, MCM/Combiner/HC, Splitter, Coupler, Kabel Jumper antar-lantai (TO LT / FROM LT), maupun Antenna Donor.
   - Gabungkan semua gambar skematik yang dikirim menjadi SATU rangkaian sistem distribusi yang terhubung. Telusuri jalur MULAI DARI port output MCM/Combiner/HC melewati segmen kabel awal, seluruh splitter tingkat 1 (pembagi antar-lantai), splitter tingkat 2, kabel riser "TO LT X" / "FROM LT Y", hingga ke setiap titik antena di masing-masing lantai.
   - Kolom `TITIK_ANTENA`: tulis kode antena sesuai label gambar (jika antar-lantai memiliki nomor sama, wajib gunakan awalan lantai seperti LT4-AO-1, LT3-AO-1, LT2-AO-1 agar unik).
   - Kolom `LANTAI_POSISI`: tulis posisi lantai dengan huruf kapital (contoh: LANTAI 4, LANTAI 3, LANTAI 2, LANTAI 1).
   - Kolom `TIPE_ANTENA`: tulis jenis antena dengan rapi (contoh: Antenna Omni, Antenna Planar).

5. STANDAR KALKULASI LOSS PERANGKAT G-WAVE & MARGIN LAPANGAN (END-TO-END PER ANTENA):
   - TITIK AWAL PENELUSURAN (WAJIB): Mulai hitung segmen kabel pertama tepat setelah port output sumber utama (MCM/Combiner/HC atau Repeater) dan splitter percabangan utama pertama yang membagi jalur ke masing-masing lantai. Jangan melewatkan kabel awal maupun splitter pertama setelah output MCM/Combiner/HC.

   A. `FEEDER_LOSS_DB` (Total akumulasi loss kabel feeder + Margin Bending Lapangan 10% - 15%):
      - Hitung dulu loss teoritis seluruh segmen kabel yang dilewati dari output MCM/Combiner/HC hingga antena tersebut (termasuk segmen feeder awal menuju splitter pertama):
        * Feeder 1/2" = Panjang (m) * 0.07 dB/m
        * Feeder 7/8" = Panjang (m) * 0.04 dB/m
        * Feeder 1 1/4" = Panjang (m) * 0.03 dB/m
      - Terapkan Margin Bending & Routing Lapangan sebesar +12% (pengali 1.12; atau 1.10 untuk riser vertikal lurus dan 1.15 untuk jalur banyak belokan):
        FEEDER_LOSS_DB = round(Total_Feeder_Loss_Teoritis * 1.12, 2)

   B. `SPLITTER_LOSS_DB` (Total akumulasi loss seluruh perangkat pasif G-Wave yang dilewati dari output MCM/Combiner/HC hingga antena tersebut):
      - Hitung mulai dari splitter percabangan pertama setelah output MCM/Combiner/HC (jangan masukkan insertion loss internal MCM/Combiner/HC, tetapi wajib hitung splitter pertama yang membagi cabang antar-lantai).
      - Hitung jumlah cabang output pada simbol tiap splitter (JANGAN menyamaratakan semua antena jika jumlah cabangnya berbeda!):
        * G-Wave Power Splitter 2-Way = 3.5 dB
        * G-Wave Power Splitter 3-Way = 5.5 dB
        * G-Wave Power Splitter 4-Way = 6.5 dB
        * G-Wave Directional Coupler (perhatikan cabang port Coupled/Tap vs Through/Main pada gambar!):
          - Coupler 5 dB  -> Port Coupled = 5.5 dB | Port Through = 1.8 dB
          - Coupler 6 dB  -> Port Coupled = 6.5 dB | Port Through = 1.5 dB
          - Coupler 7 dB  -> Port Coupled = 7.5 dB | Port Through = 1.2 dB
          - Coupler 10 dB -> Port Coupled = 10.5 dB | Port Through = 0.7 dB
          - Coupler 15 dB -> Port Coupled = 15.4 dB | Port Through = 0.4 dB
          - Coupler 20 dB -> Port Coupled = 20.3 dB | Port Through = 0.3 dB
          - Coupler 30 dB -> Port Coupled = 30.3 dB | Port Through = 0.2 dB

   C. `TOTAL_LOSS_DB` (Termasuk Dynamic Connector Loss + Jumper):
      - Hitung Connector_Jumper_Loss = (Jumlah perangkat Splitter/Coupler yang dilewati mulai dari splitter pertama * 0.20 dB) + 0.50 dB (untuk konektor output MCM/Combiner/HC + jumper & konektor antena).
      - TOTAL_LOSS_DB = round(FEEDER_LOSS_DB + SPLITTER_LOSS_DB + Connector_Jumper_Loss, 2)
      - JANGAN dikurangi dengan Antenna Gain.

6. ATURAN FORMAT ANGKA & BARIS:
   - Semua nilai loss wajib berupa satu angka desimal matang (contoh: 3.92, 16.5, 21.52). DILARANG menulis rumus atau tanda tambah.
   - Setiap titik antena wajib berada pada baris terpisah (dipisah Enter/newline).
   - DILARANG menambahkan teks penjelasan apa pun sebelum atau sesudah CSV.
"""

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

def clean_csv_output(raw_text):
    text = raw_text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"\s+(?=(?:LT\d+-)?(?:AO|AP)-\d+,)", "\n", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)

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
            f"Telusuri seluruh jalur distribusi mulai dari output MCM menuju setiap titik antena indoor di semua lantai dan hitung total akumulasi Feeder Loss (+margin bending 12%), Splitter/Coupler Loss (standar G-Wave), serta Total Loss (termasuk dynamic connector & jumper loss) ke dalam SATU tabel CSV utuh.\n"
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
                    "thinkingConfig": {"thinkingLevel": "HIGH"}
                }
            }
            payload_basic = {
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0.1}
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
                    elif resp.status_code == 400 and "thinking" in resp.text.lower():
                        current_payload = payload_basic
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

        csv_result = clean_csv_output(raw_text)

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
