from flask import Flask, render_template, request, jsonify
import os
import json
import base64
import requests

app = Flask(__name__)

# Kunci API milik Anda
GEMINI_API_KEY = "AQ.Ab8RN6JCVsxxkb4DAx9NwdgEg_FW5hlHx64MqbMk0bZn5aNG3w"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return jsonify({'error': 'File tidak ditemukan'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'File belum dipilih'}), 400

    try:
        image_bytes = file.read()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        mime_type = file.mimetype or 'image/png'

        prompt = """
        Kamu adalah sistem pakar analisis skematik IBS (In-Building System).
        Analisis gambar skematik yang diberikan dan ekstraksi seluruh data antena distribusi.

        ATURAN EXCLUSION (ABAIKAN):
        - Abaikan Antenna Donor di sebelah kiri.
        - Abaikan Title Block, Legenda REMARKS, Tabel Approval/Revisi di sisi paling kanan.
        
        ALUR PENELUSURAN DARI OUTPUT REPEATER KE ANTENA:
        1. Identifikasi nama lantai tepat untuk setiap antena berdasarkan pembatas garis horizontal dan label nama lantai (misal LANTAI 1, LANTAI 2, dsb).
        2. Analisis tipe antena: Lingkaran Cyan/Biru = Antenna Omni, Segitiga Cyan/Biru = Antenna Planar.
        3. Berikan kode penamaan berurutan dari antena paling atas ke paling bawah (AO-1, AO-2, dst. atau AP-1, AP-2, dst.).
        4. Telusuri jalur kabel dari output Repeater ke setiap antena:
           - Hitung total panjang Feeder 1/2" (dalam Meter) lalu hitung Feeder Loss (asumsi 1/2" = 0.07 dB/m).
           - Identifikasi jumlah & tipe Splitter pada jalur tersebut (Splitter 2-Way = 3.5 dB, 3-Way = 6.0 dB, 4-Way = 7.0 dB).
           - Hitung TOTAL_LOSS_DB = FEEDER_LOSS_DB + SPLITTER_LOSS_DB + 0.5 (jumper/connector).

        Kembalikan respon HANYA dalam format JSON array murni tanpa markdown/penjelasan tambahan dengan struktur persis seperti berikut:
        [
          {
            "TITIK_ANTENA": "AO-7",
            "LANTAI_POSISI": "LANTAI 2",
            "TIPE_ANTENA": "Antenna Omni",
            "FEEDER_LOSS_DB": 3.5,
            "SPLITTER_LOSS_DB": 7.0,
            "TOTAL_LOSS_DB": 11.0
          }
        ]
        """

        # Endpoint REST API Gemini v1beta murni
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64_image
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {
                "response_mime_type": "application/json"
            }
        }

        response = requests.post(url, json=payload, headers={'Content-Type': 'application/json'}, timeout=180)
        res_json = response.json()

        if response.status_code != 200:
            error_msg = res_json.get('error', {}).get('message', 'Terjadi kesalahan pada API')
            return jsonify({'error': f"API Error ({response.status_code}): {error_msg}"}), response.status_code

        raw_text = res_json['candidates'][0]['content']['parts'][0]['text']
        data = json.loads(raw_text)

        return jsonify({
            'success': True,
            'count': len(data),
            'data': data
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
