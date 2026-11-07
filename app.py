from flask import Flask, render_template, request, jsonify
import os
import json
import base64
import requests

app = Flask(__name__)

# Masukkan Groq API Key Anda
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
        Analisis gambar skematik yang diberikan dan ekstraksi seluruh data antena distribusi dengan perhitungan presisi tinggi.

        ATURAN EXCLUSION (ABAIKAN):
        - Abaikan Antenna Donor di sebelah kiri dan Feeder 7/8" 70 Meter menuju Antenna Donor.
        - Abaikan Title Block, Legenda REMARKS, Tabel Approval/Revisi di sisi paling kanan.
        
        ALUR PENELUSURAN DARI OUTPUT REPEATER KE ANTENA:
        1. Identifikasi LANTAI_POSISI dari setiap antena berdasarkan garis pembatas horizontal dan label lantai (LANTAI 1, LANTAI 2, dsb).
        2. TIPE_ANTENA: Lingkaran Cyan/Biru = "Antenna Omni", Segitiga Cyan/Biru = "Antenna Planar".
        3. Berikan TITIK_ANTENA berurutan dari paling bawah ke paling atas (misal AO-1 s/d AO-8).
        
        4. PERHITUNGAN FEEDER_LOSS_DB (SANGAT PENTING & KRUSIAL):
           - Telusuri seluruh panjang kabel Feeder 1/2" dari Output Repeater hingga ke titik antena terkait.
           - WAJIB JUMLAHKAN: Panjang Feeder Batang Utama (misal 25 Meter untuk Lantai 1 atau 30 Meter untuk Lantai 2) + Panjang Feeder Cabang Akhir ke Antena (misal 20m, 25m, 30m, 35m).
           - Contoh Lantai 1: Feeder Utama (25m) + Cabang AO-4 (35m) = 60 Meter Total.
           - FEEDER_LOSS_DB = Total Panjang Feeder (Meter) x 0.07 dB/m. Bulatkan ke 2 angka di belakang koma (misal: 60 x 0.07 = 4.2, 45 x 0.07 = 3.15).

        5. PERHITUNGAN SPLITTER_LOSS_DB:
           - Hitung total loss komponen splitter yang dilewati pada jalur tersebut:
             Splitter 2-Way = 3.5 dB, Splitter 3-Way = 6.0 dB, Splitter 4-Way = 7.0 dB.
           - Jika melewati Splitter 2-Way dan Splitter 4-Way, SPLITTER_LOSS_DB = 3.5 + 7.0 = 10.5 dB.

        6. PERHITUNGAN TOTAL_LOSS_DB:
           - TOTAL_LOSS_DB = FEEDER_LOSS_DB + SPLITTER_LOSS_DB + 0.5 (margin fixed jumper & connector).

        Kembalikan respon HANYA dalam format JSON array murni tanpa markdown/penjelasan tambahan dengan struktur seperti ini:
        [
          {
            "TITIK_ANTENA": "AO-1",
            "LANTAI_POSISI": "LANTAI 1",
            "TIPE_ANTENA": "Antenna Omni",
            "FEEDER_LOSS_DB": 3.5,
            "SPLITTER_LOSS_DB": 10.5,
            "TOTAL_LOSS_DB": 14.5
          }
        ]
        """

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }

        # Daftar model Vision aktif di Groq (dengan Fallback)
        VISION_MODELS = [
            "qwen-3.8-27b",
            "llama-3.2-11b-vision-preview",
            "meta-llama/llama-4-scout-17b-16e-instruct"
        ]

        res_json = None
        last_error = ""

        for vision_model in VISION_MODELS:
            payload = {
                "model": vision_model,
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

            if response.status_code == 200:
                break
            else:
                last_error = res_json.get('error', {}).get('message', 'API Error')

        if not res_json or 'choices' not in res_json:
            return jsonify({'error': f"Gagal memproses gambar: {last_error}"}), 400

        raw_text = res_json['choices'][0]['message']['content']
        parsed_data = json.loads(raw_text)
        
        # Ambil array jika JSON terbungkus objek utama
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
