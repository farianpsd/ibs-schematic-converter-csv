from flask import Flask, render_template, request, jsonify
import os
import json
import base64
import requests

app = Flask(__name__)

# Dapatkan API Key gratis dari https://console.groq.com/keys
GROQ_API_KEY = "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"

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

        url = "https://api.groq.com/openai/v1/chat/completions"
        
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": "qwen-2.5-32b",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"}
        }

        response = requests.post(url, json=payload, headers=headers, timeout=120)
        res_json = response.json()

        if response.status_code != 200:
            error_msg = res_json.get('error', {}).get('message', 'Terjadi kesalahan pada API Groq')
            return jsonify({'error': f"API Error ({response.status_code}): {error_msg}"}), response.status_code

        raw_text = res_json['choices'][0]['message']['content']
        
        # Parse JSON output
        parsed_data = json.loads(raw_text)
        
        # Jika hasil dibungkus objek (misal {"data": [...]}), ambil array di dalamnya
        if isinstance(parsed_data, dict):
            for key in parsed_data:
                if isinstance(parsed_data[key], list):
                    parsed_data = parsed_data[key]
                    break

        return jsonify({
            'success': True,
            'count': len(parsed_data) if isinstance(parsed_data, list) else 1,
            'data': parsed_data
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
