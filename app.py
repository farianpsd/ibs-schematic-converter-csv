import os
import io
import base64
import requests
from flask import Flask, render_template, request, jsonify
from PIL import Image

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

SYSTEM_INSTRUCTION = """
Kamu adalah Senior RF Engineer IBS (In-Building System) yang bertugas mengekstrak seluruh topologi skematik multi-lantai menjadi 1 tabel CSV Link Budget yang presisi.

ATURAN KELUARAN (STRICT CSV):
1. WAJIB HANYA MENGELUARKAN TEKS CSV MURNI.
2. DILARANG KERAS menggunakan simbol Markdown seperti bold (**), backtick (`), atau pagar (#) di mana pun.
3. BARIS PERTAMA (HEADER) WAJIB EXACT SAMA SEPERTI INI:
Floor,Source_Node,Source_Type,Target_Node,Target_Type,Component_Detail,Length_m,Unit_Loss_dB_per_m,Component_Loss_dB,Antenna_Gain_dBi,Net_Branch_Loss_dB

4. ATURAN PEMBACAAN MULTI-GAMBAR & KONTINUITAS JALUR:
   - Gabungkan semua gambar yang dikirim menjadi SATU rangkaian sistem distribusi yang terhubung.
   - Perhatikan label penghubung antar-gambar (contoh: kabel bertuliskan "TO LT 3" pada Lantai 4 terhubung langsung ke kabel "FROM LT 4" pada Lantai 3 dan Lantai 2).
   - Untuk penamaan titik antena agar tidak bentrok antar-lantai, gunakan format: LT4-AO-1, LT3-AO-1, LT2-AO-1, dst.
   - Ekstrak juga jalur dari Repeater (Telkomsel, Indosat, XL) menuju MCM/Combiner, maupun dari Antenna Donor menuju Repeater.

5. STANDAR KALKULASI LOSS & GAIN RF (ANGKA DESIMAL SAJA TANPA SATUAN):
   - FEEDER 1/2": Unit_Loss_dB_per_m = 0.07, Component_Loss_dB = round(Length_m * 0.07, 2)
   - FEEDER 7/8": Unit_Loss_dB_per_m = 0.04, Component_Loss_dB = round(Length_m * 0.04, 2)
   - FEEDER 1 1/4": Unit_Loss_dB_per_m = 0.03, Component_Loss_dB = round(Length_m * 0.03, 2)
   - SPLITTER 2-WAY: Length_m = 0, Unit_Loss_dB_per_m = 0, Component_Loss_dB = 3.20
   - SPLITTER 3-WAY: Length_m = 0, Unit_Loss_dB_per_m = 0, Component_Loss_dB = 5.00
   - SPLITTER 4-WAY: Length_m = 0, Unit_Loss_dB_per_m = 0, Component_Loss_dB = 6.30
   - COUPLER (6dB / 10dB sesuai label): Length_m = 0, Unit_Loss_dB_per_m = 0, Component_Loss_dB = nilai coupler
   - MCM / COMBINER: Length_m = 0, Unit_Loss_dB_per_m = 0, Component_Loss_dB = 3.50
   - JUMPER + CONNECTOR (jika terpasang): tambahkan 0.50 dB pada segmen terkait.
   - ANTENNA GAIN (Antenna_Gain_dBi):
     * Antenna Omni = 2.0
     * Antenna Planar = 7.0
     * Antenna Donor = 11.0
     * Jika baris bukan merupakan koneksi akhir ke antena, isi Antenna_Gain_dBi = 0.
   - Net_Branch_Loss_dB = round(Component_Loss_dB - Antenna_Gain_dBi, 2)

6. DILARANG MENAMBAHKAN TEKS PENJELASAN APAPUN SEBELUM ATAU SESUDAH CSV.
"""

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

def clean_csv_output(raw_text):
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text

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
            f"Ekstrak seluruh jalur, panjang kabel, splitter, loss, dan gain antena dari semua gambar tersebut menjadi SATU tabel CSV utuh.\n"
            f"Catatan tambahan user: {user_message}"
        )

        parts = [{"inline_data": {"mime_type": "image/jpeg", "data": b64}} for b64 in b64_images]
        parts.append({"text": prompt_text})

        candidate_models = ['gemini-3.8-flash']
        raw_text = None
        last_error = None

        for model_name in candidate_models:
            base_host = "generativelanguage.googleapis.com"
            url = f"https://{base_host}/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": {
                    "temperature": 0.1
                }
            }
            try:
                resp = requests.post(url, json=payload, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    if raw_text:
                        break
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
            except Exception as err:
                last_error = str(err)
                continue

        if not raw_text:
            raise Exception(f"Detail error: {last_error}")

        csv_result = clean_csv_output(raw_text)
        formatted_reply = f"```csv\n{csv_result}\n```" if request.path == '/chat' else csv_result

        return jsonify({
            'status': 'success',
            'image_count': len(b64_images),
            'csv': csv_result,
            'reply': formatted_reply,
            'response': formatted_reply
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
